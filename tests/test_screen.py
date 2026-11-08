import base64
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from wda_screen import MJPEGParser, ScreenHub, jpeg_dimensions

ROOT = Path(__file__).resolve().parents[1]


def jpeg(width=440, height=956, payload=b"pixels"):
    # Synthetic SOF header; the preview parser deliberately does not decode pixels.
    header = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8\xff\xe0\x00\x04AB\xff\xc0\x00\x0b" + header + b"\xff\xda\x00\x02" + payload + b"\xff\xd9"


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for owned test stream.")


class MJPEGTests(unittest.TestCase):
    def test_dimensions_reject_malformed_and_read_nonbaseline_sof(self):
        raw = jpeg()
        self.assertEqual(jpeg_dimensions(raw), (440, 956))
        self.assertEqual(jpeg_dimensions(raw.replace(b"\xff\xc0", b"\xff\xc2")), (440, 956))
        for malformed in (b"", b"not JPEG", raw[:20], raw.replace(b"\x00\x0b", b"\xff\xff"), jpeg(width=0)):
            self.assertIsNone(jpeg_dimensions(malformed))

    def test_byte_split_markers_and_multipart_yield_complete_frames(self):
        first, second = jpeg(), jpeg(1170, 2532)
        stream = b"HTTP/1.0 200 OK\r\n\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n" + first + b"\r\n--frame\r\n\r\n" + second
        parser, frames = MJPEGParser(), []
        for byte in stream:
            frames.extend(parser.feed(bytes([byte])))
        self.assertEqual(frames, [(first, (440, 956)), (second, (1170, 2532))])
        self.assertLessEqual(len(parser.buffer), 1)

    def test_oversize_and_malformed_frames_are_bounded_then_recover(self):
        parser = MJPEGParser(max_bytes=1024)
        self.assertEqual(parser.feed(b"\xff\xd8" + b"A" * 8000), [])
        self.assertLessEqual(len(parser.buffer), 1024)
        raw = jpeg()
        self.assertEqual(parser.feed(b"\xff\xd9--boundary\xff\xd8broken\xff\xd9" + raw), [(raw, (440, 956))])
        self.assertLessEqual(len(parser.buffer), 1024)


class ScreenHubTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.hub = ScreenHub(self.directory)

    def tearDown(self):
        self.hub.close()
        if self.hub._thread:
            self.hub._thread.join(timeout=2)
        self.temporary.cleanup()

    @staticmethod
    def pipe_command(raw, delay=0, repeat=True):
        code = ("import base64,sys,time; "
                f"time.sleep({delay}); raw=base64.b64decode({base64.b64encode(raw).decode()!r}); "
                "sys.stdout.buffer.write(raw); sys.stdout.buffer.flush(); "
                + ("time.sleep(30)" if repeat else "time.sleep(.1)"))
        return [sys.executable, "-u", "-c", code]

    def test_start_never_creates_capture_or_returns_cached_base64(self):
        with patch.object(self.hub, "_command") as command:
            self.hub._frame = {"seq": 1, "data": "private pixels"}
            self.assertIsNone(self.hub.start()["frame"])
            command.assert_not_called()
            self.assertIsNone(self.hub._thread)
        other = ScreenHub(self.directory)
        try:
            other.set_paused(True)
            self.assertTrue(self.hub.start()["paused"])
        finally:
            other.close()

    def test_real_pipe_frames_are_cached_and_poll_does_not_wait(self):
        raw = jpeg(1170, 2532)
        with patch.object(self.hub, "_command", return_value=self.pipe_command(raw, delay=.25)):
            started = time.monotonic()
            initial = self.hub.frame()
            self.assertLess(time.monotonic() - started, .15)
            self.assertIsNone(initial["frame"])
            eventually(lambda: self.hub._frame is not None)
            value = self.hub.frame()
            self.assertEqual(value["frame"]["mimeType"], "image/jpeg")
            self.assertEqual((value["frame"]["width"], value["frame"]["height"]), (1170, 2532))
            self.assertEqual(base64.b64decode(value["frame"]["data"]), raw)
            self.assertIsNone(self.hub.frame(after_seq=value["frame"]["seq"])["frame"])
            self.assertFalse(any(self.directory.glob("*.jpg")))

    def test_restart_returns_lower_sequence_with_a_new_stream_identity(self):
        self.hub._frame={"seq":1,"data":"private pixels","mimeType":"image/jpeg","width":440,"height":956}
        result=self.hub._wire(self.hub._read_state(),after_seq=800)
        self.assertEqual(result["frame"]["seq"],1)
        self.assertTrue(result["frame_available"])
        other=ScreenHub(self.directory)
        try:self.assertNotEqual(other.start()["stream_id"],result["stream_id"])
        finally:other.close()
        same=self.hub._wire(self.hub._read_state(),after_seq=1)
        self.assertIsNone(same["frame"])
        self.assertTrue(same["frame_available"])
        self.hub.close()
        self.assertFalse(self.hub.start()["frame_available"])

    def test_hidden_widget_lease_stops_only_its_owned_child_and_preserves_last_frame(self):
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            with patch("wda_screen.FRAME_LEASE_SECONDS", .2), patch.object(self.hub, "_command", return_value=self.pipe_command(jpeg())):
                self.hub.frame()
                eventually(lambda: self.hub._child is not None and self.hub._frame is not None)
                child = self.hub._child
                cached = self.hub._wire(self.hub._read_state())["frame"]
                eventually(lambda: child.poll() is not None)
                eventually(lambda: not self.hub._thread.is_alive())
                self.assertEqual(self.hub._wire(self.hub._read_state())["frame"], cached)
                self.hub.frame(after_seq=cached["seq"])
                eventually(lambda: self.hub._child is not None and self.hub._child is not child)
                self.assertEqual(self.hub._wire(self.hub._read_state())["frame"], cached)
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_transient_stream_exit_keeps_cached_frame_until_explicit_close(self):
        with patch.object(self.hub, "_command", return_value=self.pipe_command(jpeg(), repeat=False)):
            self.hub.frame()
            eventually(lambda: self.hub._child is not None and self.hub._frame is not None)
            child = self.hub._child
            cached = self.hub._wire(self.hub._read_state())["frame"]
            eventually(lambda: child.poll() is not None and self.hub._child is None)
            self.assertEqual(self.hub._wire(self.hub._read_state())["frame"], cached)
            self.assertTrue(self.hub.start()["frame_available"])
            self.hub.close()
            self.assertIsNone(self.hub._wire(self.hub._read_state())["frame"])
            self.assertFalse(self.hub.start()["frame_available"])

    def test_duplicate_frames_refresh_liveness_without_resending_pixels(self):
        stop = threading.Event()
        state = self.hub._read_state()
        with patch("wda_screen.time.monotonic", return_value=10):
            self.hub._publish_frame(jpeg(), 440, 956, stop)
        with patch("wda_screen.time.monotonic", return_value=13):
            self.assertEqual(self.hub._wire(state)["frame_age_ms"], 3000)
            self.hub._publish_frame(jpeg(), 440, 956, stop)
            wire = self.hub._wire(state, after_seq=1)
            self.assertEqual(wire["frame_age_ms"], 0)
            self.assertIsNone(wire["frame"])
            self.assertEqual(self.hub._seq, 1)
        stop.set()
        with patch("wda_screen.time.monotonic", return_value=16):
            self.hub._publish_frame(jpeg(), 440, 956, stop)
            self.assertEqual(self.hub._wire(state)["frame_age_ms"], 3000)

    def test_latest_jpeg_is_encoded_only_when_requested_and_identical_frames_are_skipped(self):
        stop = threading.Event()
        state = self.hub._read_state()
        with patch("wda_screen.base64.b64encode", wraps=base64.b64encode) as encode:
            for index in range(30):
                self.hub._publish_frame(jpeg(payload=str(index).encode()), 440, 956, stop)
            self.assertEqual(self.hub._seq, 30)
            self.assertEqual(encode.call_count, 0)
            self.assertIsNone(self.hub.start()["frame"])
            self.assertEqual(encode.call_count, 0)
            latest = self.hub._wire(state)["frame"]
            self.assertEqual(base64.b64decode(latest["data"]), jpeg(payload=b"29"))
            self.assertNotIn("_jpeg", latest)
            self.assertEqual(encode.call_count, 1)
            for _ in range(30):
                self.hub._publish_frame(jpeg(payload=b"29"), 440, 956, stop)
            self.assertEqual(self.hub._seq, 30)
            self.assertIsNone(self.hub._wire(state, after_seq=30)["frame"])
            self.assertEqual(self.hub._wire(state)["frame"], latest)
            self.assertEqual(encode.call_count, 1)
            self.hub._publish_frame(jpeg(payload=b"next"), 440, 956, stop)
            self.assertEqual(encode.call_count, 1)
            self.assertEqual(self.hub._wire(state, after_seq=30)["frame"]["seq"], 31)
            self.assertEqual(encode.call_count, 2)

    def test_pause_clears_last_frame_and_a_late_capture_cannot_repopulate_it(self):
        self.hub._publish_frame(jpeg(), 440, 956, self.hub._stop)
        self.assertTrue(self.hub.start()["frame_available"])
        stop = self.hub._stop
        self.hub.set_paused(True)
        self.hub._publish_frame(jpeg(payload=b"late"), 440, 956, stop)
        self.assertIsNone(self.hub._frame)
        self.assertIsNone(self.hub.frame()["frame"])
        self.assertFalse(self.hub.start()["frame_available"])

    def test_pause_from_another_stdio_process_stops_stream_and_requires_resume(self):
        other = ScreenHub(self.directory)
        try:
            with patch.object(self.hub, "_command", return_value=self.pipe_command(jpeg())) as command:
                self.hub.frame()
                eventually(lambda: self.hub._child is not None and self.hub._frame is not None)
                child = self.hub._child
                other.set_paused(True)
                eventually(lambda: child.poll() is not None)
                eventually(lambda: self.hub._frame is None)
                calls = command.call_count
                self.assertTrue(self.hub.start()["paused"])
                self.assertIsNone(self.hub.frame()["frame"])
                time.sleep(.15)
                self.assertEqual(command.call_count, calls)
                other.set_paused(False)
                self.assertFalse(self.hub.frame()["paused"])
                eventually(lambda: command.call_count > calls)
        finally:
            other.close()

    def test_shared_activity_events_grace_expiry_and_private_geometry_only(self):
        other = ScreenHub(self.directory)
        try:
            with patch.object(ScreenHub, "_now", return_value=100_000):
                self.hub.set_viewport({"width": 440, "height": 956, "text": "secret"})
                token = self.hub.begin("secret app, input text and selector")
                self.hub.gesture("tap", point={"x": 20, "y": 30, "text": "secret"})
                self.hub.gesture("drag", from_point={"x": 20, "y": 80}, to_point={"x": 20, "y": 40}, duration_ms=300)
                wire = other.start()
                self.assertTrue(wire["busy"])
                self.assertTrue(wire["input_busy"])
                self.assertEqual(wire["viewport"], {"width": 440, "height": 956})
                self.assertEqual([event["kind"] for event in wire["events"]], ["tap", "drag"])
                self.assertEqual(len(other._wire(other._read_state(), last_event_id=1)["events"]), 1)
                self.hub.end(token)
                self.assertTrue(other.start()["busy"])
                self.assertFalse(other.start()["input_busy"])
            with patch.object(ScreenHub, "_now", return_value=100_651):
                self.assertFalse(other.start()["busy"])
            encoded = (self.directory / "screen-state.json").read_text()
            self.assertNotIn("secret", encoded)
            self.assertNotIn("data", encoded)
            self.assertEqual(os.stat(self.directory / "screen-state.json").st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(self.directory / ".screen-state.lock").st_mode & 0o777, 0o600)
            with patch.object(ScreenHub, "_now", return_value=111_000):
                self.assertEqual(other.start()["events"], [])
                self.hub.begin("operation")
            with patch.object(ScreenHub, "_now", return_value=126_000):
                self.assertFalse(other.start()["busy"])
        finally:
            other.close()

    def test_bounded_events_invalid_geometry_and_concurrent_actor_completion(self):
        for index in range(45):
            self.hub.gesture("tap", point={"x": index, "y": 10})
        before = self.hub.start()["events"]
        self.assertEqual(len(before), 32)
        for kind, arguments in (("tap", {"point": {"x": float("nan"), "y": 1}}),
                                ("drag", {"from_point": {"x": 1, "y": 1}, "to_point": {"x": 2, "y": 2}, "duration_ms": 9999}),
                                ("type", {"point": {"x": 1, "y": 1}})):
            self.hub.gesture(kind, **arguments)
        self.assertEqual(self.hub.start()["events"], before)
        with patch.object(ScreenHub, "_now", return_value=self.hub._now()):
            first, second = self.hub.begin("first"), self.hub.begin("second")
            self.hub.end(first)
            self.assertIn(second, self.hub._read_state()["actors"])
            self.hub.set_paused(True)
            self.assertFalse(self.hub.start()["busy"])
            self.assertEqual(self.hub.start()["events"], [])

    def test_state_file_is_parsed_once_per_change_not_once_per_stream_chunk(self):
        self.hub.set_paused(False)
        with patch("wda_screen.json.loads", wraps=json.loads) as parse:
            for _ in range(200):
                self.assertFalse(self.hub._paused())
                self.hub._read_state()
            self.assertLessEqual(parse.call_count, 1)
            # Another process pausing for authentication replaces the file and is seen at once.
            other = ScreenHub(self.directory)
            self.addCleanup(other.close)
            other.set_paused(True)
            self.assertTrue(self.hub._paused())
            self.assertTrue(self.hub._read_state()["paused"])
        (self.directory / "screen-state.json").unlink()
        self.assertFalse(self.hub._paused())

    def test_restart_drops_the_frame_and_announces_a_new_stream(self):
        self.hub._frame = {"seq": 9, "_jpeg": jpeg(), "mimeType": "image/jpeg", "width": 440, "height": 956}
        self.hub._seq = 9
        before = self.hub.start()["stream_id"]
        self.hub.device = {"model": "iPhone 17 Pro Max"}
        state = self.hub.restart()
        self.assertNotEqual(state["stream_id"], before)
        self.assertEqual((state["frame"], state["frame_available"], self.hub._seq), (None, False, 0))
        self.assertEqual(state["device"], {"model": "iPhone 17 Pro Max"})
        self.assertFalse(self.hub.paused())

    def test_stream_command_uses_existing_usb_dependencies_and_no_new_install(self):
        (self.directory / "config.json").write_text(json.dumps({"udid": "TEST-DEVICE-1234", "password": "secret"}))
        with patch("wda_screen.shutil.which", return_value="/usr/bin/node"):
            self.assertIsNone(self.hub._command())
            dependency = self.directory / "runtime/forward/node_modules/appium-ios-device"
            dependency.mkdir(parents=True)
            command = self.hub._command()
            self.assertEqual(command[-2:], ["TEST-DEVICE-1234", "9100"])
            self.assertEqual(Path(command[1]).read_bytes(), (ROOT / "tooling/screen-stream.mjs").read_bytes())
            self.assertNotIn("secret", Path(command[1]).read_text())
            self.assertEqual(os.stat(command[1]).st_mode & 0o777, 0o600)
        (self.directory / "config.json").write_text('{"udid":"TEST-DEVICE-1234", "mjpeg_device_port":"http://remote"}')
        self.assertIsNone(self.hub._command())


class NodeStreamTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is not installed.")
    def test_paused_usb_socket_dechunks_binary_body_and_requests_only_stream_root(self):
        body = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg() + b"\r\n--frame--\r\n"
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *arguments):
                pass

            def do_GET(self):
                requests.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for offset in range(0, len(body), 7):
                    chunk = body[offset:offset + 7]
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                runtime = Path(temporary)
                script = runtime / "screen-stream.mjs"
                script.write_bytes((ROOT / "tooling/screen-stream.mjs").read_bytes())
                stub = runtime / "node_modules/appium-ios-device"
                stub.mkdir(parents=True)
                (stub / "package.json").write_text('{"type":"module","main":"index.js"}')
                # appium-ios-device returns the socket paused after unpiping its
                # usbmux plist reader. A fresh, flowing TCP fixture hides this.
                (stub / "index.js").write_text("import net from 'node:net'; export default {utilities:{connectPort: async () => "
                                               f"net.connect({server.server_port}, '127.0.0.1').pause()" + "}};")
                result = subprocess.run([shutil.which("node"), str(script), "TEST-DEVICE-1234", "9100"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                self.assertEqual(result.stdout, body)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(requests, ["/"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
