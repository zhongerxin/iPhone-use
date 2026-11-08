#!/usr/bin/env python3
"""Run WDA over a private network (Tailscale) instead of USB.

Run with the pymobiledevice3 venv created by scripts/tailscale_setup.sh.

  serve     Started by the plugin's `pua_setup start` job in network mode (never on its own):
            forwards 127.0.0.1:<local_port> to the phone's WDA, launches the runner over
            the network if needed, and shuts WDA down after idle_minutes without requests.
  pair-usb  One-time, phone on USB: create the RemotePairing record without a prompt.
            iOS refuses new pairings over the network.
  probe     Open the developer tunnel over the network and list the installed runner.
  launch    Launch the runner once in the foreground (debugging).

Launching needs the phone's Wi-Fi to be on (any network). iOS only listens for
RemotePairing (port 49152) while Wi-Fi is up. Control and the screen stream work
over cellular too. lockdownd (62078) resets requests over the network on iOS 27,
so this uses RemotePairing plus pymobiledevice3's no-root userspace tunnel.
"""
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import urllib.request

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
from wda_screen import network_target  # noqa: E402
from wda_setup import state_directory  # noqa: E402

REMOTE_PAIRING_PORT = 49152
CHECK_SECONDS = 15
LAUNCH_GRACE_SECONDS = 120
log = logging.getLogger("iphone-use-tailscale")


def settings():
    state = Path(state_directory()).expanduser()
    config = json.loads((state / "config.json").read_text())
    network = network_target(state)
    if not network:
        raise SystemExit(f"Missing or invalid {state / 'network.json'}; run scripts/tailscale_setup.sh HOST.")
    return {"udid": config["udid"], "runner": config["bundle_id"] + ".xctrunner",
            "local_port": config.get("local_port", 18100), **network}


def remote_tunnel(cfg):
    """A UserspaceRsdTunnel whose provider is RemotePairing at host:49152 rather than USB lockdown."""
    from pymobiledevice3.remote import tunnel_service, userspace_tunnel

    async def provider(serial, autopair, remotepairing_fallback=True):
        service = tunnel_service.RemotePairingTunnelService(cfg["udid"], cfg["host"], REMOTE_PAIRING_PORT)
        await service.connect(autopair=False)  # never prompt; pair-usb creates the record
        return service, None

    userspace_tunnel._create_no_root_tunnel_provider = provider
    return userspace_tunnel.UserspaceRsdTunnel(serial=cfg["udid"])


async def launch(cfg):
    """Open the tunnel and run the WDA runner. Returns only when the test session ends."""
    from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
    async with remote_tunnel(cfg) as rsd:
        test = await TestConfig.create_for(rsd, cfg["runner"], None)
        test.runner_app_env = {"USE_PORT": str(cfg["wda_port"]), "MJPEG_SERVER_PORT": str(cfg["mjpeg_port"])}
        log.info("launching %s over %s", cfg["runner"], cfg["host"])
        await XCUITestService(rsd).run(test)


async def probe(cfg):
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    async with remote_tunnel(cfg) as rsd:
        async with InstallationProxyService(lockdown=rsd) as proxy:
            apps = await proxy.get_apps(application_type="User")
        print(json.dumps({"tunnel": True, "ios": rsd.product_version,
                          "runner_installed": cfg["runner"] in apps}))


async def pair_usb(cfg):
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
    lockdown = await create_using_usbmux(serial=cfg["udid"])
    service = await RemotePairingLockdownService.create(lockdown)
    try:
        await service.connect(autopair=True)
        print("RemotePairing record already valid.")
    except RemotePairingCompletedError:
        print(f"Paired over USB; record saved to {service.pair_record_path}. You can unplug the phone.")
    finally:
        await service.close()
        await lockdown.close()


def wda_ready(cfg):
    try:
        with urllib.request.urlopen(f"http://{cfg['host']}:{cfg['wda_port']}/status", timeout=5) as response:
            return json.load(response).get("value", {}).get("ready") is True
    except Exception:
        return False


class Forward:
    """127.0.0.1:<local_port> -> phone WDA. Loopback only: WDA has no authentication."""

    def __init__(self, cfg):
        self.cfg, self.last_activity = cfg, time.monotonic()

    async def start(self):
        server = await asyncio.start_server(self._handle, "127.0.0.1", self.cfg["local_port"])
        log.info("forwarding 127.0.0.1:%s -> %s:%s", self.cfg["local_port"], self.cfg["host"], self.cfg["wda_port"])
        return server

    async def _pipe(self, reader, writer, request_side):
        try:
            while data := await reader.read(65536):
                if request_side:
                    self.last_activity = time.monotonic()
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _handle(self, client_reader, client_writer):
        self.last_activity = time.monotonic()
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(self.cfg["host"], self.cfg["wda_port"]), timeout=10)
        except (OSError, asyncio.TimeoutError) as error:
            log.warning("forward connect failed: %s", error)
            client_writer.close()
            return
        await asyncio.gather(self._pipe(client_reader, remote_writer, True),
                             self._pipe(remote_reader, client_writer, False))


def shutdown_wda(cfg):
    with contextlib.suppress(Exception):
        urllib.request.urlopen(f"http://{cfg['host']}:{cfg['wda_port']}/wda/shutdown", timeout=5).close()


async def serve(cfg):
    """Owned by the plugin's `pua_setup start` job: forward, launch WDA if needed, stop when idle.

    Exit 0 after the idle timeout (WDA shut down); exit 1 when WDA cannot be launched or dies,
    so the plugin's normal status/start/recover flow takes over.
    """
    forward = Forward(cfg)
    idle = cfg["idle_minutes"] * 60
    task = None
    async with await forward.start():
        if not await asyncio.to_thread(wda_ready, cfg):
            task = asyncio.create_task(launch(cfg))
            deadline = time.monotonic() + LAUNCH_GRACE_SECONDS
            while not await asyncio.to_thread(wda_ready, cfg):
                if task.done() or time.monotonic() > deadline:
                    task.cancel()
                    log.error("WDA did not become ready (phone locked, Wi-Fi off, or UI automation not approved?)")
                    raise SystemExit(1)
                await asyncio.sleep(2)
        else:
            log.info("WDA already running; forwarding only")
        log.info("WDA ready; idle timeout %s min", cfg["idle_minutes"])
        failures = 0
        while True:
            await asyncio.sleep(CHECK_SECONDS)
            if idle and time.monotonic() - forward.last_activity > idle:
                log.info("idle for %s min; shutting WDA down", cfg["idle_minutes"])
                if task:
                    task.cancel()
                await asyncio.to_thread(shutdown_wda, cfg)
                raise SystemExit(0)
            failures = 0 if await asyncio.to_thread(wda_ready, cfg) else failures + 1
            if failures >= 3:
                log.error("WDA stopped responding")
                raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("serve", "pair-usb", "probe", "launch"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("pymobiledevice3", "asyncio", "pytcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.umask(0o077)
    cfg = settings()
    action = {"serve": serve, "pair-usb": pair_usb, "probe": probe, "launch": launch}[args.command]
    asyncio.run(action(cfg))


if __name__ == "__main__":
    main()
