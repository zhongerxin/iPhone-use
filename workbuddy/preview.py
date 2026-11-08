#!/usr/bin/env python3
"""The live preview's frame source under WorkBuddy: one process, shared by every server process.

WorkBuddy answers each resource read from the screen panel with a server process started
for that read alone, so a server cannot hold the USB stream and the latest frame between
reads the way upstream's ScreenHub expects. This process holds them. Frames stay in memory,
capture runs only while frames are being asked for (ScreenHub's own lease), and the process
exits once nobody has asked for a while.

It answers in two places. Server processes ask over a socket in the private state
directory. The panel, once such an answer has told it where, asks this process directly
over loopback HTTP and waits there for each new frame, so a frame costs WorkBuddy nothing
and arrives as it is captured. The HTTP side listens on 127.0.0.1 only, on a port and under
a path token chosen at random for this process; the token reaches the panel through the
MCP answer and nothing else.

A panel that cannot reach the HTTP side keeps reading through WorkBuddy, about four times a
second. Each answer therefore also carries the frames since the previous read, for the
panel to play back at the pace they were captured.
"""
import base64
import collections
import contextlib
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

SERVER = Path(__file__).resolve().parents[1] / "server"
IDLE_SECONDS = 30
REPLY_SECONDS = 2
# What one answer may carry besides the newest frame: enough for a slow read at 30 frames a second.
PLAYBACK_MS = 600
PLAYBACK_FRAMES = 16
# The longest a direct request is held open for a frame that has not been captured yet.
WAIT_MS = 1000
FRAME_PATH = re.compile(r"frame/(\d{1,16})/(\d{1,16})")


def socket_path(state_dir):
    return Path(state_dir) / "preview.sock"


def build():
    """Which copy of this file a process runs, so a reinstall retires the old process."""
    status = os.stat(__file__)
    return f"{status.st_mtime_ns}:{status.st_size}"


def request(state_dir, after_seq=0, last_event_id=0):
    """The preview from the running process, or None when there is none to ask."""
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(REPLY_SECONDS)
            client.connect(str(socket_path(state_dir)))
            asked = {"build": build(), "after_seq": after_seq, "last_event_id": last_event_id}
            client.sendall((json.dumps(asked) + "\n").encode())
            with client.makefile("rb") as reply:
                return json.loads(reply.readline())
    except (OSError, ValueError):
        return None


def playback_hub(state_dir):
    """Upstream's ScreenHub, also remembering the last moments of frames and announcing each new one."""
    sys.path.insert(0, str(SERVER))
    from wda_screen import ScreenHub

    class PlaybackHub(ScreenHub):
        def __init__(self, state_dir):
            super().__init__(state_dir)
            self.recent = collections.deque(maxlen=4 * PLAYBACK_FRAMES)
            self.fresh = threading.Condition(self._lock)

        def _publish_frame(self, raw, width, height, stop):
            # One step under upstream's lock: a reader never meets a frame without its time.
            with self._lock:
                previous = self._frame
                super()._publish_frame(raw, width, height, stop)
                if self._frame is not None and self._frame is not previous:
                    self._frame["at"] = self._now()
                    self.recent.append(self._frame)
                    self.fresh.notify_all()

        def frame(self, after_seq=0, last_event_id=0, wait_ms=0):
            shown = super().frame(after_seq, last_event_id)
            if wait_ms and shown["frame"] is None:
                # Nothing newer yet: hold the caller until there is, which is what paces it.
                with self.fresh:
                    self.fresh.wait_for(lambda: self._frame is not None and self._frame["seq"] != after_seq, min(wait_ms, WAIT_MS) / 1000)
                shown = super().frame(after_seq, last_event_id)
            newest = shown["frame"]
            with self._lock:
                if newest is None:
                    # Paused or stopped: upstream has dropped its frame, and so must this.
                    if not shown["frame_available"]:
                        self.recent.clear()
                elif after_seq:
                    earlier = [frame for frame in self.recent
                               if after_seq < frame["seq"] < newest["seq"] and newest["at"] - frame["at"] <= PLAYBACK_MS][-PLAYBACK_FRAMES:]
                    for frame in earlier:
                        if "data" not in frame:
                            frame["data"] = base64.b64encode(frame["_jpeg"]).decode("ascii")
                    shown["more"] = [{key: value for key, value in frame.items() if key != "_jpeg"} for frame in earlier]
            return shown

    return PlaybackHub(state_dir)


def direct_server(hub, asked):
    """The loopback HTTP side: GET /<token>/frame/<after_seq>/<last_event_id>[?wait=<ms>]."""
    token = secrets.token_urlsafe(24)

    class Direct(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            url = urlsplit(self.path)
            prefix, _, rest = url.path[1:].partition("/")
            frame = FRAME_PATH.fullmatch(rest)
            # The Host check keeps a web page from reaching this through a name it points at 127.0.0.1.
            if self.headers.get("Host") != "127.0.0.1:%d" % self.server.server_port \
                    or not hmac.compare_digest(prefix.encode(), token.encode()) or not frame:
                self.send_error(404)
                return
            asked()
            try:
                wait = int(parse_qs(url.query).get("wait", ["0"])[0])
            except ValueError:
                wait = 0
            body = json.dumps(hub.frame(int(frame.group(1)), int(frame.group(2)), max(0, wait)), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The panel's page is another origin. The token in the path is what admits a caller.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *arguments):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Direct)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="preview-direct", daemon=True).start()
    return server, "http://127.0.0.1:%d/%s" % (server.server_port, token)


def serve(state_dir, idle_seconds=IDLE_SECONDS):
    path = socket_path(state_dir)
    # Two server processes may both find nobody to ask and both start one of these.
    lock = os.open(Path(state_dir) / "preview.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return 0
    hub = playback_hub(state_dir)
    running = build()
    last_asked = [time.monotonic()]

    def asked():
        last_asked[0] = time.monotonic()

    direct, address = direct_server(hub, asked)
    with socket.socket(socket.AF_UNIX) as listener:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        mask = os.umask(0o177)
        try:
            listener.bind(str(path))
        finally:
            os.umask(mask)
        listener.listen(8)
        listener.settimeout(min(1, idle_seconds))
        try:
            while True:
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    if time.monotonic() - last_asked[0] > idle_seconds:
                        return 0
                    continue
                asked()
                with connection, connection.makefile("rwb") as stream:
                    connection.settimeout(REPLY_SECONDS)
                    try:
                        question = json.loads(stream.readline())
                        current = question["build"] == running
                        preview = None
                        if current:
                            preview = hub.frame(int(question["after_seq"]), int(question["last_event_id"]))
                            preview["direct"] = address
                        stream.write((json.dumps(preview, separators=(",", ":")) + "\n").encode())
                        stream.flush()
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
                if not current:
                    return 0
        finally:
            direct.shutdown()
            direct.server_close()
            hub.close()
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1], *map(float, sys.argv[2:3])))
