import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
UDID = "TEST-DEVICE-1234"

# Stands in for appium-ios-device's usbmux client. Records are re-read on every
# listing, so a test can unplug the cable between connections; connect maps a
# DeviceID to a local TCP "device".
STUB = """
import fs from 'node:fs';
import net from 'node:net';
export async function getDefaultSocket() { return null; }
export class Usbmux {
  async listDevices() { return JSON.parse(fs.readFileSync(process.env.STUB_RECORDS, 'utf8')); }
  async connect(deviceID) { return net.connect(JSON.parse(process.env.STUB_TARGETS)[deviceID], '127.0.0.1').pause(); }
  close() {}
}
"""

# Stands in for `xcrun devicectl list devices ... --json-output -`: prints the
# CoreDevice listing the test wrote last, or fails when there is none.
XCRUN = '#!/bin/sh\nexec cat "$STUB_DEVICECTL"\n'


def record(device_id, kind, serial=UDID):
    return {"DeviceID": device_id, "Properties": {"SerialNumber": serial, "ConnectionType": kind, "DeviceID": device_id}}


def core_device(state="connected", address="::1", link="localNetwork", udid=UDID):
    return {"result": {"devices": [{"hardwareProperties": {"udid": udid, "reality": "physical"},
                                    "connectionProperties": {"tunnelState": state, "tunnelIPAddress": address,
                                                             "transportType": link}}]}}


@unittest.skipUnless(shutil.which("node"), "Node is not installed.")
class ForwardTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        for name in ("forward.mjs", "device-path.mjs"):
            shutil.copyfile(ROOT / "tooling" / name, self.runtime / name)
        stub = self.runtime / "node_modules/appium-ios-device"
        (stub / "build/lib/usbmux").mkdir(parents=True)
        (stub / "package.json").write_text('{"type":"module"}')
        (stub / "build/lib/usbmux/index.js").write_text(STUB)
        bin_dir = self.runtime / "bin"
        bin_dir.mkdir()
        (bin_dir / "xcrun").write_text(XCRUN)
        (bin_dir / "xcrun").chmod(0o700)
        self.path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        self.records = self.runtime / "records.json"
        self.devicectl = self.runtime / "devicectl.json"
        # The cable "device" is reached through usbmux; the tunnel "device" listens
        # on IPv6 loopback, the address the fake CoreDevice listing reports.
        self.targets = {1: self.device(b"USB1", "127.0.0.1")}
        try:
            self.tunnel_port = self.device(b"TUNL", "::1")
        except OSError:
            self.skipTest("IPv6 loopback is unavailable.")

    def device(self, name, host):
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        listener = socket.create_server((host, 0), family=family)
        self.addCleanup(listener.close)

        def serve():
            while True:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                with client:
                    client.sendall(name)
                    # Hold the tunnel open until the forward closes it.
                    client.recv(1)

        threading.Thread(target=serve, daemon=True).start()
        return listener.getsockname()[1]

    def set_paths(self, records=(), core=None):
        self.records.write_text(json.dumps(list(records)))
        if core is None:
            self.devicectl.unlink(missing_ok=True)
        else:
            self.devicectl.write_text(json.dumps(core))

    def forward(self, transport, device_port=None):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, PATH=self.path, STUB_RECORDS=str(self.records), STUB_TARGETS=json.dumps(self.targets),
                   STUB_DEVICECTL=str(self.devicectl))
        process = subprocess.Popen([shutil.which("node"), str(self.runtime / "forward.mjs"), UDID, str(port),
                                    str(device_port or self.tunnel_port), transport],
                                   env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.kill, process)
        self.assertTrue(select.select([process.stdout], [], [], 5)[0], "forward did not start")
        self.assertIn(f"listening at 127.0.0.1:{port} (transport={transport})", process.stdout.readline())
        return process, port

    @staticmethod
    def kill(process):
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    @staticmethod
    def finish(process):
        process.terminate()
        return process.communicate(timeout=5)

    @staticmethod
    def through(port):
        """The 4 bytes the device behind the forward sends; empty when the forward refuses."""
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            data = b""
            while len(data) < 4:
                chunk = client.recv(4 - len(data))
                if not chunk:
                    break
                data += chunk
            return data

    def test_auto_prefers_the_cable_and_falls_back_to_the_tunnel(self):
        self.set_paths([record(2, "Network"), record(1, "USB")], core_device())
        process, port = self.forward("auto")
        self.assertEqual(self.through(port), b"USB1")
        # Unplugged: a usbmux Network record is never used, the tunnel is.
        self.set_paths([record(2, "Network")], core_device())
        self.assertEqual(self.through(port), b"TUNL")
        self.set_paths([record(1, "USB")], core_device())
        self.assertEqual(self.through(port), b"USB1")
        out, err = self.finish(process)
        self.assertEqual([line for line in out.splitlines() if line.startswith("Device path:")],
                         ["Device path: USB", "Device path: CoreDevice tunnel (localNetwork)", "Device path: USB"])
        self.assertEqual(err, "")

    def test_network_uses_only_the_tunnel(self):
        self.set_paths([record(1, "USB"), record(2, "Network")], core_device())
        process, port = self.forward("network")
        self.assertEqual(self.through(port), b"TUNL")
        self.set_paths([record(1, "USB")], core_device(state="disconnected", address=None))
        self.assertEqual(self.through(port), b"")
        _, err = self.finish(process)
        self.assertIn("Device connect failed: No CoreDevice tunnel to the configured iPhone (transport=network): "
                      "tunnelState=disconnected.", err)

    def test_usb_never_uses_the_tunnel(self):
        # Another phone's cable does not count as this phone's USB record.
        self.set_paths([record(2, "Network"), record(1, "USB", serial="OTHER-PHONE-0001")], core_device())
        process, port = self.forward("usb")
        self.assertEqual(self.through(port), b"")
        _, err = self.finish(process)
        self.assertIn("Device connect failed: No USB record for the configured iPhone (transport=usb).", err)

    def test_tunnel_problems_are_reported_readably(self):
        process, port = self.forward("auto")
        cases = [(core_device(state="disconnected", address=None), "No USB record or CoreDevice tunnel for the configured "
                                                                     "iPhone (transport=auto): tunnelState=disconnected."),
                 (core_device(udid="OTHER-PHONE-0001"), "CoreDevice does not list it."),
                 # A host name is refused, so the address can never trigger a lookup.
                 (core_device(address="tunnel.example"), "no IPv6 tunnel address."),
                 (None, "devicectl failed:")]
        for core, expected in cases:
            self.set_paths([], core)
            self.assertEqual(self.through(port), b"", expected)
        _, err = self.finish(process)
        for _, expected in cases:
            self.assertIn(expected, err)

    def test_tunnel_port_that_is_not_listening_is_reported_readably(self):
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
            closed = probe.getsockname()[1]
        self.set_paths([], core_device())
        process, port = self.forward("network", device_port=closed)
        self.assertEqual(self.through(port), b"")
        _, err = self.finish(process)
        self.assertIn(f"CoreDevice tunnel connection to device port {closed} failed: the port is not listening yet", err)

    def test_unknown_transport_is_rejected_before_listening(self):
        result = subprocess.run([shutil.which("node"), str(self.runtime / "forward.mjs"), UDID, "18100", "8100", "wifi"],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Transport must be one of: auto, usb, network.", result.stderr)
        self.assertNotIn("listening", result.stdout)


if __name__ == "__main__":
    unittest.main()
