#!/usr/bin/env python3
"""Doubao Work MCP stdio entrypoint: the upstream server, adapted to how Doubao Work hosts it.

Doubao Work takes a local server as a custom connector: a command and its arguments typed
into a form. The form gets the interpreter and this file; what else the server needs from
this Mac is read from host.json beside this file, which the installer writes.

Doubao Work starts one server when the connector is switched on and keeps it, and it
forwards the screen view's tool calls. So the view works the way upstream built it, with
two things taken from the WorkBuddy port: the frames between two polls are kept for the
view to play back, and the worker that keeps the phone service running outlives the server.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent


def apply_host_file(path=HERE / "host.json"):
    """Put the directories of node, npm, git and Xcode's tools on PATH, ahead of what the host gave."""
    try:
        found = json.loads(path.read_text())
        directories = [item for item in found["path"] if isinstance(item, str)]
        defaults = {name: value for name, value in found["env"].items() if isinstance(value, str)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return
    given = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys(filter(None, directories + given)))
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


apply_host_file()
sys.path[:0] = [str(HERE.parent / "server"), str(HERE.parent / "workbuddy")]
import mcp_server as workbuddy
import iphone_use
import preview
import doubao_text


class Trace:
    """What the host asks of the server, by method, tool name and resource URI only.

    How a host treats a server and its view (which process answers, which of a view's
    requests arrive) cannot be seen from outside it. This writes that down, into
    <state>/doubao/host.log, while <state>/doubao/trace exists. Arguments, results and
    frames are never written.
    """
    LIMIT = 1 << 20
    EVERY = 200

    def __init__(self, state_dir, stdin):
        self.stdin = stdin
        self.frames = 0
        self.log = None
        own = Path(state_dir) / "doubao"
        try:
            if (own / "trace").exists():
                path = own / "host.log"
                self.log = open(path, "w" if path.exists() and path.stat().st_size > self.LIMIT else "a", buffering=1)
        except OSError:
            pass
        if self.log:
            self.write("start", {"python": sys.version.split()[0], "parent": os.getppid(), "cwd": os.getcwd(), "state_writable": os.access(state_dir, os.W_OK),
                                 "usb": self.usb(), **{name: shutil.which(name) for name in ("node", "npm", "git", "xcodebuild", "xcrun")}})

    @staticmethod
    def usb():
        """Whether this process may reach the Mac's USB device service; a sandboxed one may not."""
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect("/var/run/usbmuxd")
            return True
        except OSError as error:
            return type(error).__name__

    def write(self, what, detail=None):
        if self.log:
            try:
                self.log.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "what": what, "detail": detail}, ensure_ascii=False) + "\n")
            except (OSError, ValueError):
                self.log = None

    def __iter__(self):
        return self

    def __next__(self):
        try:
            line = next(self.stdin)
        except StopIteration:
            self.write("stdin closed", {"frame_requests": self.frames})
            raise
        if self.log:
            self.note(line)
        return line

    def note(self, line):
        try:
            message = json.loads(line)
            method, params = message["method"], message.get("params") or {}
            if method == "initialize":
                detail = {key: params.get(key) for key in ("protocolVersion", "clientInfo", "capabilities")}
            else:
                detail = params.get("name") if method == "tools/call" else params.get("uri") if method == "resources/read" else None
        except (ValueError, TypeError, KeyError, AttributeError):
            self.write("unreadable request")
            return
        if detail == "pua_screen_frame":
            # Four a second while the view is open: the first, then one in every EVERY.
            self.frames += 1
            if self.frames % self.EVERY != 1:
                return
            detail = [detail, self.frames]
        self.write(method, detail)


def adapt(server, strict=True):
    """Adjust an imported upstream iphone_use module for Doubao Work."""
    doubao_text.adapt_server(server, strict)
    # One tool opens the screen. Upstream puts the view on READY as well, so that Codex reuses
    # one side panel; Doubao Work is told to open it with pua_screen, once.
    upstream_uri, server.SCREEN_URI = server.SCREEN_URI, doubao_text.screen_uri(server.SCREEN_URI)
    for tool in server.TOOLS:
        if tool["name"] == "pua_ready":
            tool.pop("_meta", None)
        elif tool.get("_meta", {}).get("ui", {}).get("resourceUri") == upstream_uri:
            tool["_meta"]["ui"]["resourceUri"] = server.SCREEN_URI
    # The server lives until Doubao Work quits or the connector is switched off; the phone service should not end with it.
    workbuddy.detach_workers(sys.modules[server.SetupManager.__module__])
    # The view asks four times a second. A hub that keeps the frames in between lets it show all of them.
    server.ScreenHub = preview.playback_hub
    # As in WorkBuddy: 30 half-size frames a second instead of upstream's 10 full-size ones.
    sys.modules[server.WDAClient.__module__].SESSION_SETTINGS.update(mjpegServerFramerate=30, mjpegScalingFactor=50)


def main():
    # A started server with one stale sentence beats a server that refuses to start;
    # the installer and the tests are where drift is reported.
    adapt(iphone_use, strict=False)
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--state-dir")
    state_dir = Path(iphone_use.state_directory(options.parse_known_args()[0].state_dir)).expanduser()
    trace = sys.stdin = Trace(state_dir, sys.stdin)
    try:
        return iphone_use.main()
    except BaseException:
        trace.write("stopped", traceback.format_exc(limit=6))
        raise


if __name__ == "__main__":
    sys.exit(main())
