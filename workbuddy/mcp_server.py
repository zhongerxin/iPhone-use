#!/usr/bin/env python3
"""WorkBuddy MCP stdio entrypoint: the upstream server, adapted to how WorkBuddy hosts it."""
import contextlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import host_text
import iphone_use
import preview

# Upstream starts the worker that keeps the phone service running as a child of the server,
# in a session of its own. Codex keeps one server for hours. WorkBuddy starts one per chat,
# and when that server went away the worker was terminated with it, new session or not, so
# the next chat waited for the service to start again. A worker started by a launcher that
# exits at once belongs to launchd, and nothing below the server leads to it.
# The launcher's stderr is the job log; the worker writes there.
LAUNCHER = """import subprocess, sys
print(subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, stdout=2, stderr=2, start_new_session=True).pid)
"""
LAUNCH_TIMEOUT = 10
WORKER_OPTIONS = {"stdin", "stdout", "stderr", "env", "start_new_session", "close_fds"}


class DetachedWorker:
    """What SetupManager keeps per job in place of a Popen: a worker that is not this server's child."""

    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        if self.returncode is None:
            try:
                os.kill(self.pid, 0)
            except OSError:
                # launchd collected the exit status; the job file says how the job ended.
                self.returncode = 0
        return self.returncode


class WorkerSubprocess:
    """The subprocess module as upstream wda_setup sees it, with the worker launch detached."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def Popen(self, *args, **options):
        # Only the call upstream makes today is rerouted. Written any other way it runs as
        # upstream wrote it, and the tests report that the worker is a child again.
        if not (len(args) == 1 and isinstance(args[0], list) and args[0][2:3] == ["--worker"] and set(options) == WORKER_OPTIONS
                and options["stdin"] == subprocess.DEVNULL and hasattr(options["stdout"], "fileno")
                and options["stderr"] is options["stdout"] and options["start_new_session"] is True
                and options["close_fds"] is True):
            return subprocess.Popen(*args, **options)
        launcher = subprocess.Popen([sys.executable, "-I", "-c", LAUNCHER, *args[0]], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=options["stderr"], env=options["env"], close_fds=True)
        try:
            reported, _ = launcher.communicate(timeout=LAUNCH_TIMEOUT)
            return DetachedWorker(int(reported))
        except (subprocess.TimeoutExpired, ValueError) as error:
            launcher.kill()
            launcher.wait()
            # Upstream records an OSError as a failed job; the launcher's own error is in the job log.
            raise OSError("The setup worker could not be started detached from the MCP server.") from error


def detach_workers(setup):
    """Have an imported upstream wda_setup module start its job workers through LAUNCHER."""
    if getattr(setup, "subprocess", None) is subprocess:
        setup.subprocess = WorkerSubprocess()


class FrameResource:
    """Serve the preview frame as a readable resource, on top of upstream's serve() loop.

    WorkBuddy forwards a view's resources/read to the server but not its tools/call (see
    host_text.py). A read of FRAME_URI/<after_seq>/<last_event_id> goes into upstream as the
    pua_screen_frame call it stands for, and that call's answer comes out as the resource.
    Only this read-only tool is reachable this way.
    """
    URI = re.compile(re.escape(host_text.FRAME_URI) + r"(\d{1,16})/(\d{1,16})")

    def __init__(self, stdin, stdout):
        self.stdin, self.stdout = stdin, stdout
        self.reads = {}
        self.lock = threading.Lock()
        self.partial = ""

    def __iter__(self):
        return self

    def __next__(self):
        return self.request(next(self.stdin))

    def request(self, line):
        try:
            message = json.loads(line)
            frame = message["method"] == "resources/read" and self.URI.fullmatch(message["params"]["uri"])
        except (ValueError, TypeError, KeyError):
            return line
        if not frame or "id" not in message:
            return line
        with self.lock:
            self.reads[json.dumps(message["id"])] = frame.group(0)
        call = {"name": "pua_screen_frame", "arguments": {"after_seq": int(frame.group(1)), "last_event_id": int(frame.group(2))}}
        return json.dumps({"jsonrpc": "2.0", "id": message["id"], "method": "tools/call", "params": call}) + "\n"

    def write(self, text):
        # Upstream prints one whole response at a time under its own lock.
        with self.lock:
            self.partial += text
            while "\n" in self.partial:
                line, self.partial = self.partial.split("\n", 1)
                self.stdout.write(self.response(line) + "\n")
        return len(text)

    def response(self, line):
        if not self.reads:
            return line
        try:
            message = json.loads(line)
            uri = self.reads.pop(json.dumps(message["id"]))
        except (ValueError, TypeError, KeyError):
            return line
        preview = message.get("result", {}).get("structuredContent")
        if preview is None:
            answer = {"error": {"code": -32603, "message": "The preview frame is unavailable."}}
        else:
            answer = {"result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(preview, separators=(",", ":"))}]}}
        return json.dumps({"jsonrpc": "2.0", "id": message["id"], **answer}, ensure_ascii=False)

    def flush(self):
        self.stdout.flush()


def start_preview(state_dir):
    """Start the shared frame source, detached like the worker so it outlives this server."""
    launcher = subprocess.Popen([sys.executable, "-I", "-c", LAUNCHER, sys.executable, str(Path(preview.__file__).resolve()), str(state_dir)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    with contextlib.suppress(subprocess.TimeoutExpired):
        launcher.wait(timeout=LAUNCH_TIMEOUT)


def shared_frame(hub, after_seq=0, last_event_id=0):
    """ScreenHub.frame for a server process that may exist for this one request (see preview.py)."""
    shown = preview.request(hub.state_dir, after_seq, last_event_id)
    if shown is None:
        # Nothing to show yet; the panel asks again in a second and finds the source running.
        start_preview(hub.state_dir)
        shown = hub.start()
    if hub.device:
        shown["device"] = hub.device
    return shown


def adapt(server, strict=True):
    """Adjust an imported upstream iphone_use module for WorkBuddy."""
    host_text.adapt_server(server, strict)
    # One tool opens the screen, in the side panel. Upstream puts the screen on READY as well;
    # WorkBuddy treats each tool as an app of its own, so that would be two panels.
    upstream_uri, server.SCREEN_URI = server.SCREEN_URI, host_text.screen_uri(server.SCREEN_URI)
    server.SCREEN_META["ui"]["csp"]["connectDomains"] = list(host_text.SCREEN_CONNECT)
    for tool in server.TOOLS:
        if tool["name"] == "pua_ready":
            tool.pop("_meta", None)
        elif tool.get("_meta", {}).get("ui", {}).get("resourceUri") == upstream_uri:
            tool["_meta"]["ui"]["resourceUri"] = server.SCREEN_URI
            # Without this WorkBuddy mounts the view inline in the chat, one more per call.
            tool["_meta"]["workbuddy"] = {"ui": {"launchSurface": "panel"}}
    detach_workers(sys.modules[server.SetupManager.__module__])
    server.ScreenHub.frame = shared_frame
    # The phone streams 10 full-size frames a second unless told otherwise. The panel draws the
    # phone about 300 points wide, so half size loses nothing there and costs a quarter of the bytes.
    sys.modules[server.WDAClient.__module__].SESSION_SETTINGS.update(mjpegServerFramerate=30, mjpegScalingFactor=50)


if __name__ == "__main__":
    # A started server with one stale sentence beats a server that refuses to start;
    # the installer and the tests are where drift is reported.
    adapt(iphone_use, strict=False)
    sys.stdin = sys.stdout = FrameResource(sys.stdin, sys.stdout)
    sys.exit(iphone_use.main())
