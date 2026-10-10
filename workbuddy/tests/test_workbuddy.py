import contextlib
import base64
import copy
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "workbuddy"), str(ROOT / "server")]
# Importing the server and the staged handshakes must never send production usage events.
os.environ["IPHONE_USE_ANALYTICS"] = "0"
import host_text
import install as installer
import mcp_server
import preview

CODEX_TOOLS = ("functions.", "request_user_input", "view_image", "ALL_TOOLS")


def pristine_server():
    """A private copy of the upstream module, so rewriting it leaves other tests alone."""
    spec = importlib.util.spec_from_file_location("iphone_use_pristine", ROOT / "server/iphone_use.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HostTextTests(unittest.TestCase):
    def test_every_rewrite_still_matches_upstream(self):
        mcp_server.adapt(pristine_server())
        for relative in host_text.SKILLS:
            host_text.skill_text(relative, (ROOT / relative).read_text())
        host_text.screen_html((ROOT / installer.SCREEN_PAGE).read_text())

    def test_server_names_workbuddy_tools_and_keeps_the_tool_contract(self):
        upstream = pristine_server()
        tools = copy.deepcopy(upstream.TOOLS)
        mcp_server.adapt(upstream)
        self.assertIn("AskUserQuestion", upstream.INSTRUCTIONS)
        described = upstream.INSTRUCTIONS + "".join(tool["description"] for tool in upstream.TOOLS)
        for word in CODEX_TOOLS + ("Codex",):
            self.assertNotIn(word, described)
        # Names, schemas and annotations are what the model calls; _meta is for the host.
        for before, after in zip(tools, upstream.TOOLS):
            self.assertEqual({k: v for k, v in before.items() if k not in ("description", "_meta")},
                             {k: v for k, v in after.items() if k not in ("description", "_meta")})

    def test_only_pua_screen_opens_the_screen_and_in_the_side_panel(self):
        upstream = pristine_server()
        mcp_server.adapt(upstream)
        opens = {tool["name"]: tool["_meta"] for tool in upstream.TOOLS if tool.get("_meta", {}).get("ui", {}).get("resourceUri")}
        self.assertEqual(list(opens), ["pua_screen"])
        # The page differs from upstream's, so it must not share upstream's cached URI.
        self.assertEqual(opens["pua_screen"]["ui"]["resourceUri"], upstream.SCREEN_URI)
        self.assertNotEqual(upstream.SCREEN_URI, pristine_server().SCREEN_URI)
        self.assertEqual(opens["pua_screen"]["workbuddy"], {"ui": {"launchSurface": "panel"}})
        # The page may fetch from loopback and nothing more: no new place to load pictures or scripts from.
        self.assertEqual(upstream.SCREEN_META["ui"]["csp"], {"connectDomains": ["http://127.0.0.1:*"], "resourceDomains": []})
        self.assertIn("READY does not open it", upstream.INSTRUCTIONS)

    def test_frame_read_goes_in_as_the_frame_call_and_comes_out_as_the_resource(self):
        written = []

        class Stdout:
            def write(self, text):
                written.append(text)

        channel = mcp_server.FrameResource(iter(()), Stdout())
        uri = host_text.FRAME_URI + "7/3"
        read = {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": uri}}
        self.assertEqual(json.loads(channel.request(json.dumps(read) + "\n")),
                         {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                          "params": {"name": "pua_screen_frame", "arguments": {"after_seq": 7, "last_event_id": 3}}})
        preview = {"frame": {"seq": 8, "data": "x" * 200_000}, "frame_available": True}
        # Another response in between, and the frame response arriving as print() writes it.
        other = json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"tools": []}})
        answer = json.dumps({"jsonrpc": "2.0", "id": 5, "result": {"content": [], "structuredContent": preview, "isError": False}})
        for text in (other, "\n", answer, "\n"):
            channel.write(text)
        self.assertEqual(written[0], other + "\n")
        resource = json.loads(written[1])
        self.assertEqual(resource["result"]["contents"][0]["uri"], uri)
        self.assertEqual(json.loads(resource["result"]["contents"][0]["text"]), preview)
        # A later response with the same id is nobody's frame.
        channel.write(answer + "\n")
        self.assertEqual(written[2], answer + "\n")

    def test_phone_is_asked_for_thirty_half_size_frames_a_second(self):
        upstream = pristine_server()
        mcp_server.adapt(upstream)
        settings = sys.modules[upstream.WDAClient.__module__].SESSION_SETTINGS
        self.assertEqual((settings["mjpegServerFramerate"], settings["mjpegScalingFactor"]), (30, 50))
        self.assertEqual(settings["waitForIdleTimeout"], 0)

    def test_playback_still_fits_upstreams_screen_hub(self):
        sys.path.insert(0, str(ROOT / "server"))
        from wda_screen import ScreenHub
        self.assertEqual(list(inspect.signature(ScreenHub._publish_frame).parameters), ["self", "raw", "width", "height", "stop"])
        with tempfile.TemporaryDirectory() as state:
            hub = preview.playback_hub(state)
            self.addCleanup(hub.close)
            hub._publish_frame(b"\xff\xd8first", 4, 8, hub._stop)
            hub._publish_frame(b"\xff\xd8second", 4, 8, hub._stop)
            hub._publish_frame(b"\xff\xd8second", 4, 8, hub._stop)
            self.assertEqual([(frame["seq"], frame["width"], "at" in frame) for frame in hub.recent], [(1, 4, True), (2, 4, True)])
            self.assertEqual(hub._frame, hub.recent[-1])

    def test_only_a_frame_read_is_rerouted(self):
        channel = mcp_server.FrameResource(iter(()), None)
        page = mcp_server.iphone_use.SCREEN_URI
        for line in ('{"jsonrpc":"2.0","id":1,"method":"resources/read","params":{"uri":"%s"}}\n' % page,
                     '{"jsonrpc":"2.0","id":2,"method":"resources/read","params":{"uri":"%sx/1"}}\n' % host_text.FRAME_URI,
                     '{"jsonrpc":"2.0","id":3,"method":"resources/read","params":{"uri":"ui://iphone-use/action/home"}}\n',
                     '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"pua_tap","arguments":{"x":1,"y":1}}}\n',
                     '{"jsonrpc":"2.0","method":"resources/read","params":{"uri":"%s1/1"}}\n' % host_text.FRAME_URI,
                     "not json\n", "[1]\n"):
            self.assertEqual(channel.request(line), line)
        self.assertEqual(channel.reads, {})

    def test_failed_frame_call_is_a_failed_read(self):
        written = []
        channel = mcp_server.FrameResource(iter(()), type("Stdout", (), {"write": lambda self, text: written.append(text)})())
        channel.request(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "resources/read", "params": {"uri": host_text.FRAME_URI + "0/0"}}))
        channel.write(json.dumps({"jsonrpc": "2.0", "id": "a", "result": {"content": [{"type": "text", "text": "{}"}], "isError": True}}) + "\n")
        self.assertEqual(json.loads(written[0])["error"]["code"], -32603)

    def test_widget_page_reads_frames_as_a_resource_and_drops_the_toolbar(self):
        upstream = (ROOT / installer.SCREEN_PAGE).read_text()
        page = host_text.screen_html(upstream)
        self.assertLess(page.index(host_text.SCREEN_SCRIPT), page.index('<script type="module">'))
        self.assertEqual(page.count('.name==="pua_screen_frame")return globalThis.__workbuddyFrame(this,'), 1)
        self.assertIn(host_text.FRAME_URI, host_text.SCREEN_SCRIPT)
        self.assertIn("#toolbar{display:none", host_text.SCREEN_SCRIPT)
        # Nothing else in the page changes.
        inserted = r'if\(\w+&&\w+\.name==="pua_screen_frame"\)return globalThis\.__workbuddyFrame\(this,\w+,\w+\);'
        self.assertEqual(re.sub(inserted, "", page.replace(host_text.SCREEN_SCRIPT, "", 1)), upstream)
        for broken in ("<html></html>", upstream.replace("async callServerTool(", "async callTool(")):
            with self.assertRaises(host_text.Drift):
                host_text.screen_html(broken)

    def test_skills_drop_codex_only_tool_names(self):
        for path in (ROOT / "skills").rglob("*.md"):
            relative = path.relative_to(ROOT).as_posix()
            text = host_text.skill_text(relative, path.read_text())
            for word in CODEX_TOOLS:
                self.assertNotIn(word, text, relative)

    def test_reworded_upstream_passage_is_reported_or_tolerated(self):
        with self.assertRaises(host_text.Drift):
            host_text.rewrite("new upstream wording", [("old wording", "x")], "INSTRUCTIONS")
        self.assertEqual(host_text.rewrite("new upstream wording", [("old wording", "x")], strict=False), "new upstream wording")

    def test_analytics_stays_on_only_when_asked(self):
        directory = Path("/tmp/wb/mcp-servers/iphone-use")
        self.assertEqual(installer.server_entry(directory, "/usr/bin/python3", analytics=False)["env"]["IPHONE_USE_ANALYTICS"], "0")
        self.assertNotIn("IPHONE_USE_ANALYTICS", installer.server_entry(directory, "/usr/bin/python3", analytics=True)["env"])


def short_state_directory(case):
    """A state directory whose socket path fits in sockaddr_un, with its frame source retired afterwards."""
    state = tempfile.mkdtemp(prefix="pua", dir="/tmp")
    case.addCleanup(shutil.rmtree, state, ignore_errors=True)
    case.addCleanup(retire_preview, state)
    return state


def retire_preview(state):
    """Ask the frame source of this state directory to exit.

    Two reads in quick succession can each start one; the second takes over when the first
    is retired, so keep asking until nothing has answered for a moment.
    """
    quiet = time.monotonic() + 1
    while time.monotonic() < quiet:
        try:
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(str(preview.socket_path(state)))
                client.sendall(b'{"build": "retired", "after_seq": 0, "last_event_id": 0}\n')
                client.makefile("rb").readline()
            quiet = time.monotonic() + 0.5
        except OSError:
            time.sleep(0.05)


FAKE_NODE = """#!%s
# Stands in for the USB screen stream: a new, distinct 440x956 JPEG header every 50 ms.
import sys, time
count = 0
while True:
    count += 1
    comment = bytes([0xFF, 0xFE]) + (6).to_bytes(2, "big") + count.to_bytes(4, "big")
    size = bytes([0xFF, 0xC0]) + (17).to_bytes(2, "big") + bytes([8]) + (956).to_bytes(2, "big") + (440).to_bytes(2, "big") + bytes(10)
    sys.stdout.buffer.write(bytes([0xFF, 0xD8]) + comment + size + bytes([0xFF, 0xD9]))
    sys.stdout.buffer.flush()
    time.sleep(0.05)
""" % sys.executable


class PreviewTests(unittest.TestCase):
    """The panel's frame reads, each answered by a server process of its own, as WorkBuddy does."""

    def setUp(self):
        self.state = Path(short_state_directory(self))
        (self.state / "config.json").write_text(json.dumps({"udid": "00000000-TESTPHONE0000001"}))
        (self.state / "device.json").write_text(json.dumps({"udid": "00000000-TESTPHONE0000001", "model": "Test Phone"}))
        (self.state / "runtime/forward/node_modules/appium-ios-device").mkdir(parents=True)
        tools = self.state / "bin"
        tools.mkdir()
        (tools / "node").write_text(FAKE_NODE)
        (tools / "node").chmod(0o755)
        self.env = {"PATH": os.pathsep.join([str(tools), *installer.SYSTEM_PATH]), "HOME": str(Path.home()),
                    "IPHONE_USE_STATE_DIR": str(self.state), "IPHONE_USE_ANALYTICS": "0"}

    def read(self, after_seq):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": f"{host_text.FRAME_URI}{after_seq}/0"}}]
        done = subprocess.run([sys.executable, str(ROOT / "workbuddy/mcp_server.py")], input="".join(json.dumps(r) + "\n" for r in requests),
                              capture_output=True, text=True, timeout=30, env=self.env)
        return json.loads(json.loads(done.stdout.splitlines()[1])["result"]["contents"][0]["text"])

    def frame_after(self, after_seq, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            shown = self.read(after_seq)
            if shown["frame"]:
                return shown
            time.sleep(0.2)
        self.fail("no frame arrived")

    def source(self, *idle):
        process = subprocess.Popen([sys.executable, str(ROOT / "workbuddy/preview.py"), str(self.state), *idle], env=self.env)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    def test_a_frame_started_by_one_server_process_is_read_by_the_next(self):
        first = self.read(0)
        self.assertEqual((first["frame"], first["frame_available"]), (None, False))
        shown = self.frame_after(0)
        frame = shown["frame"]
        self.assertEqual((frame["width"], frame["height"], frame["mimeType"]), (440, 956, "image/jpeg"))
        self.assertEqual((shown["frame_available"], shown["device"]), (True, {"model": "Test Phone"}))
        later = self.frame_after(frame["seq"])
        self.assertGreater(later["frame"]["seq"], frame["seq"])
        self.assertEqual(later["stream_id"], shown["stream_id"])

    def test_a_read_carries_the_frames_since_the_last_one_for_playback(self):
        frame = self.frame_after(0)["frame"]
        self.assertNotIn("more", self.read(0))
        time.sleep(0.4)
        shown = self.read(frame["seq"])
        played = shown["more"] + [shown["frame"]]
        # The stand-in stream is 20 frames a second: several frames, in order, none missing, none stale.
        self.assertGreater(len(played), 4)
        self.assertEqual([each["seq"] for each in played], list(range(played[0]["seq"], played[-1]["seq"] + 1)))
        self.assertEqual(sorted(each["at"] for each in played), [each["at"] for each in played])
        self.assertLessEqual(played[-1]["at"] - played[0]["at"], preview.PLAYBACK_MS)
        self.assertLessEqual(len(shown["more"]), preview.PLAYBACK_FRAMES)
        self.assertTrue(all(base64.b64decode(each["data"]).startswith(b"\xff\xd8") for each in played))
        time.sleep(1)
        self.assertLessEqual(len(self.read(frame["seq"])["more"]), preview.PLAYBACK_FRAMES)

    def direct(self, path, host=None, timeout=5):
        """GET from the frame source's loopback side, as the panel's page does."""
        base = self.frame_after(0)["direct"] if not hasattr(self, "base") else self.base
        self.base = base
        origin, token = base.rsplit("/", 1)
        request = urllib.request.Request(origin + path.replace("TOKEN", token), headers={"Host": host} if host else {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as reply:
            return json.loads(reply.read()), reply.headers

    def test_panel_gets_each_new_frame_straight_from_the_source(self):
        shown, headers = self.direct("/TOKEN/frame/0/0")
        self.assertRegex(self.base, r"^http://127\.0\.0\.1:\d+/[\w-]{32}$")
        self.assertEqual((headers["Access-Control-Allow-Origin"], headers["Cache-Control"]), ("*", "no-store"))
        self.assertNotIn("direct", shown)
        seq = shown["frame"]["seq"]
        # Held open until the next frame exists, then answered at once: one request per frame.
        started = time.monotonic()
        for _ in range(10):
            shown, _ = self.direct(f"/TOKEN/frame/{seq}/0?wait=1000")
            self.assertGreater(shown["frame"]["seq"], seq)
            seq = shown["frame"]["seq"]
        self.assertLess(time.monotonic() - started, 1.5)

    def test_direct_side_admits_only_the_token_and_its_own_address(self):
        self.direct("/TOKEN/frame/0/0")
        for path, host in (("/wrong-token/frame/0/0", None), ("/frame/0/0", None), ("/TOKEN/", None), ("/TOKEN/frame/0", None),
                           ("/TOKEN/frame/0/0/extra", None), ("/TOKEN/frame/0/0", "evil.example:80"), ("/TOKEN/frame/0/0", "localhost")):
            with self.assertRaises(urllib.error.HTTPError, msg=(path, host)) as refused:
                self.direct(path, host)
            self.assertEqual(refused.exception.code, 404)
            self.assertIsNone(refused.exception.headers.get("Access-Control-Allow-Origin"))

    def test_direct_request_waits_out_a_paused_preview_without_a_frame(self):
        self.direct("/TOKEN/frame/0/0")
        (self.state / "screen-state.json").write_text(json.dumps({"paused": True, "pause_reason": "authentication", "pause_id": "0" * 32}))
        started = time.monotonic()
        shown, _ = self.direct("/TOKEN/frame/0/0?wait=300")
        self.assertEqual((shown["paused"], shown["frame"], shown["frame_available"]), (True, None, False))
        self.assertGreaterEqual(time.monotonic() - started, 0.25)

    def test_source_stays_while_the_panel_asks_it_directly(self):
        source = self.source("0.6")
        deadline = time.monotonic() + 5
        while preview.request(self.state) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.base = preview.request(self.state)["direct"]
        for _ in range(8):
            self.direct("/TOKEN/frame/0/0")
            time.sleep(0.2)
        self.assertIsNone(source.poll())
        self.assertEqual(source.wait(timeout=10), 0)

    def test_paused_preview_gives_no_frame(self):
        self.frame_after(0)
        (self.state / "screen-state.json").write_text(json.dumps({"paused": True, "pause_reason": "authentication", "pause_id": "0" * 32}))
        shown = self.read(0)
        self.assertEqual((shown["paused"], shown["pause_reason"], shown["frame"]), (True, "authentication", None))
        self.assertNotIn("more", self.read(1))

    def test_source_leaves_when_nobody_asks(self):
        source = self.source("0.4")
        self.assertEqual(source.wait(timeout=10), 0)
        self.assertFalse(preview.socket_path(self.state).exists())

    def test_second_source_for_the_same_phone_steps_aside(self):
        first = self.source()
        deadline = time.monotonic() + 5
        while preview.request(self.state) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.source().wait(timeout=10), 0)
        self.assertIsNotNone(preview.request(self.state))
        self.assertIsNone(first.poll())

    def test_source_from_an_earlier_install_retires_itself(self):
        source = self.source()
        deadline = time.monotonic() + 5
        while preview.request(self.state) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        with mock.patch.object(preview, "build", return_value="a newer install"):
            self.assertIsNone(preview.request(self.state))
        self.assertEqual(source.wait(timeout=10), 0)


class InstallTests(unittest.TestCase):
    OTHER = {"command": "/usr/bin/true", "args": ["--flag"], "env": {"KEEP": "1"}}

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="workbuddy home ")
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        self.config = self.home / "mcp.json"
        self.config.write_text(json.dumps({"mcpServers": {"other": self.OTHER}}))
        self.install_dir = self.home / "mcp-servers/iphone-use"

    def install(self, **options):
        return installer.install(self.home, sys.executable, **options)

    def backups(self):
        return sorted(self.home.glob("mcp.json.bak-*"))

    def test_install_registers_a_server_that_starts_under_a_gui_path(self):
        result = self.install()
        manifest = json.loads((ROOT / "plugin.json").read_text())
        self.assertEqual((result["version"], result["tools"]), (manifest["version"], 19))
        servers = json.loads(self.config.read_text())["mcpServers"]
        self.assertEqual(servers["other"], self.OTHER)
        entry = servers["iphone_use"]
        self.assertEqual(entry["args"], [str(self.install_dir / "workbuddy/mcp_server.py")])
        self.assertTrue(Path(entry["command"]).is_absolute())
        self.assertTrue(set(installer.SYSTEM_PATH) <= set(entry["env"]["PATH"].split(os.pathsep)))
        self.assertEqual(entry["env"]["NO_PROXY"], "127.0.0.1,localhost,::1")
        # The registered command itself, with nothing inherited from this shell.
        self.assertEqual(installer.handshake(entry), (manifest["version"], 19))

    def test_installed_server_answers_a_frame_read(self):
        entry = self.install()["entry"]
        uri = host_text.FRAME_URI + "0/0"
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "resources/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": uri}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "pua_screen_frame", "arguments": {}}}]
        state = short_state_directory(self)
        done = subprocess.run([entry["command"], *entry["args"]], input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True,
                              text=True, timeout=60, env={**entry["env"], "HOME": str(Path.home()), "IPHONE_USE_STATE_DIR": state})
        self.assertTrue((self.install_dir / "workbuddy/preview.py").is_file())
        replies = {reply["id"]: reply for reply in map(json.loads, done.stdout.splitlines())}
        frame = replies[3]["result"]["contents"][0]
        self.assertEqual((frame["uri"], frame["mimeType"]), (uri, "application/json"))
        # No phone is configured in this state directory: a real preview answer, without a picture.
        self.assertEqual(json.loads(frame["text"])["frame_available"], False)
        self.assertEqual(json.loads(frame["text"]).keys(), replies[4]["result"]["structuredContent"].keys())
        self.assertIn("-workbuddy-", replies[2]["result"]["resources"][0]["uri"])

    def test_install_ships_the_runtime_without_codex_manifests(self):
        self.install()
        for name in ("server/iphone_use.py", "server/posthog.json", "tooling/forward.mjs", "assets/phone-screen.html",
                     "skills/iphone-use/references/apps.json", "scripts/phone.py", "workbuddy/host_text.py", installer.MARKER):
            self.assertTrue((self.install_dir / name).is_file(), name)
        for name in installer.CODEX_ONLY + ("workbuddy/install.py", "tests"):
            self.assertFalse((self.install_dir / name).exists(), name)
        self.assertIn(host_text.SCREEN_SCRIPT, (self.install_dir / installer.SCREEN_PAGE).read_text())

    def test_install_adds_both_skills_in_workbuddy_wording(self):
        self.install()
        for name in installer.SKILLS:
            skill = (self.home / "skills" / name / "SKILL.md").read_text()
            self.assertRegex(skill, rf"(?m)^name: {name}$")
            self.assertIn("## 在 WorkBuddy 中使用", skill)
            self.assertIn("AskUserQuestion", skill)
        references = self.home / "skills/iphone-use/references"
        self.assertIn("AskUserQuestion", (references / "authentication.md").read_text())
        self.assertIn("~/.workbuddy/mcp-servers/iphone-use", (references / "tool-fallback.md").read_text())
        # The setup skill links into the other skill's references.
        self.assertTrue((self.home / "skills/iphone-use-setup/../iphone-use/references/authentication.md").is_file())

    def test_reinstall_changes_nothing_and_keeps_one_backup(self):
        self.assertTrue(self.install()["config_changed"])
        before = self.config.read_text()
        self.assertFalse(self.install()["config_changed"])
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(json.loads(self.backups()[0].read_text()), {"mcpServers": {"other": self.OTHER}})
        self.assertEqual([p.name for p in (self.home / "mcp-servers").iterdir()], ["iphone-use"])

    def test_someone_elses_server_entry_is_kept_unless_forced(self):
        foreign = {"mcpServers": {"iphone_use": {"command": "python3", "args": ["/elsewhere/server/iphone_use.py"]}}}
        self.config.write_text(json.dumps(foreign))
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(json.loads(self.config.read_text()), foreign)
        self.assertFalse(self.install_dir.exists())
        self.install(force=True)
        self.assertTrue(installer.ours(json.loads(self.config.read_text())["mcpServers"]["iphone_use"], self.install_dir))

    def test_someone_elses_skill_directory_is_kept(self):
        skill = self.home / "skills/iphone-use"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: something-else\n---\n")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual((skill / "SKILL.md").read_text(), "---\nname: something-else\n---\n")
        self.assertFalse(self.install_dir.exists())

    def test_broken_config_is_left_as_found(self):
        self.config.write_text("{")
        with self.assertRaises(installer.InstallError):
            self.install()
        self.assertEqual(self.config.read_text(), "{")
        self.assertFalse(self.install_dir.exists())

    def test_missing_config_is_created(self):
        self.config.unlink()
        self.install()
        self.assertEqual(list(json.loads(self.config.read_text())["mcpServers"]), ["iphone_use"])
        self.assertEqual(self.backups(), [])

    def test_uninstall_removes_only_this_install(self):
        self.install()
        unrelated = self.home / "skills/unrelated/SKILL.md"
        unrelated.parent.mkdir()
        unrelated.write_text("---\nname: unrelated\n---\n")
        removed = installer.uninstall(self.home)
        self.assertEqual(len(removed), 4)
        self.assertEqual(json.loads(self.config.read_text()), {"mcpServers": {"other": self.OTHER}})
        self.assertFalse(self.install_dir.exists())
        self.assertEqual([p.name for p in (self.home / "skills").iterdir()], ["unrelated"])
        self.assertEqual(installer.uninstall(self.home), [])


# Stands in for upstream's worker: no Xcode and no phone, it only has to be there.
FAKE_WORKER = """import sys, time
print("worker started", *sys.argv[1:], flush=True)
print("worker stderr", file=sys.stderr, flush=True)
time.sleep(60)
"""


def parent_of(pid):
    return int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=True).stdout)


def descendants(root):
    """Every process below root by parent pid: what a host finds when it clears a server's process tree."""
    listing = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, check=True).stdout
    children = {}
    for line in listing.splitlines():
        pid, parent = map(int, line.split())
        children.setdefault(parent, []).append(pid)
    found, pending = [], [root]
    while pending:
        below = children.get(pending.pop(), [])
        found += below
        pending += below
    return found


class DetachedWorkerTests(unittest.TestCase):
    """Upstream's own job code, adapted, with this process in the place of the MCP server."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="iphone-use-worker-")
        self.addCleanup(directory.cleanup)
        home = Path(directory.name).resolve()
        # Upstream only accepts a worker whose entrypoint carries its own file name.
        self.entrypoint = home / "wda_setup.py"
        self.entrypoint.write_text(FAKE_WORKER)
        upstream = pristine_server()
        mcp_server.adapt(upstream)
        self.setup = sys.modules[upstream.SetupManager.__module__]
        # Upstream starts Path(__file__) as the worker.
        entrypoint = mock.patch.object(self.setup, "__file__", str(self.entrypoint))
        entrypoint.start()
        self.addCleanup(entrypoint.stop)
        self.manager = upstream.SetupManager(home / "state")

    def start_job(self):
        result = self.manager.setup("fetch")
        self.assertTrue(result["ok"], result)
        job = json.loads(self.manager._job_path(result["job_id"]).read_text())
        self.addCleanup(self.kill, job["pid"])
        return job

    @staticmethod
    def kill(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    def wait_for(self, condition, seconds=5):
        deadline = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), deadline, "timed out waiting for the worker")
            time.sleep(0.05)

    def test_setup_job_starts_a_worker_that_belongs_to_launchd(self):
        job = self.start_job()
        worker = self.manager._workers[job["id"]]
        self.assertIsInstance(worker, mcp_server.DetachedWorker,
                              "upstream changed how it starts the setup worker; mcp_server.WorkerSubprocess no longer detaches it")
        self.assertEqual(worker.pid, job["pid"])
        self.assertIsNone(worker.poll())
        self.assertEqual(parent_of(worker.pid), 1)
        self.assertEqual(os.getpgid(worker.pid), worker.pid)
        # Upstream finds the worker again by its exact argv, with and without the endpoint.
        self.assertTrue(self.manager._owned(job))
        self.assertTrue(self.manager._owned(job, base_url=self.manager.base_url))
        self.assertEqual(self.manager._job_status(job)["state"], "queued")
        log = self.manager.state_dir / "logs" / (job["id"] + ".log")
        self.wait_for(lambda: "worker stderr" in log.read_text())
        self.assertIn(f"worker started --worker {self.manager.state_dir} {job['id']} {job['owner_token']}", log.read_text())

    def test_worker_survives_the_end_of_everything_below_the_server(self):
        job = self.start_job()
        # What upstream does on its own: a new session, still a child.
        child = subprocess.Popen([sys.executable, str(self.entrypoint)], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(child.kill)
        below = descendants(os.getpid())
        self.assertIn(child.pid, below)
        self.assertNotIn(job["pid"], below)
        for pid in below:
            self.kill(pid)
        self.assertEqual(child.wait(timeout=5), -signal.SIGKILL)
        self.assertIsNone(self.manager._workers[job["id"]].poll())
        self.assertTrue(self.manager._owned(job))

    def test_stop_ends_the_detached_worker(self):
        job = self.start_job()
        worker = self.manager._workers[job["id"]]
        self.assertEqual(self.manager.setup("stop", job_id=job["id"])["stopped_jobs"], [job["id"]])
        self.wait_for(lambda: worker.poll() is not None)
        self.assertFalse(self.manager._owned(job))
        self.manager._jobs()
        self.assertEqual(self.manager._workers, {})

    def test_a_launch_upstream_writes_differently_runs_as_written(self):
        with open(os.devnull, "wb") as log:
            child = self.setup.subprocess.Popen([sys.executable, str(self.entrypoint), "--worker"], stdin=subprocess.DEVNULL,
                                                stdout=log, stderr=log, env=dict(os.environ), start_new_session=True,
                                                close_fds=True, cwd=str(self.entrypoint.parent))
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertIsInstance(child, subprocess.Popen)
        self.assertEqual(parent_of(child.pid), os.getpid())


if __name__ == "__main__":
    unittest.main()
