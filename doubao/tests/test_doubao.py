import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "doubao"), str(ROOT / "workbuddy/tests")]
# Importing the server and the staged handshakes must never send production usage events.
os.environ["IPHONE_USE_ANALYTICS"] = "0"
# The WorkBuddy tests' stand-ins (an upstream module of one's own, a USB stream without a phone) serve here too.
import test_workbuddy as workbuddy_tests
import doubao_text
import mcp_stdio


def doubao_installer():
    spec = importlib.util.spec_from_file_location("doubao_install", ROOT / "doubao/install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = doubao_installer()
# Tools only another host has. The skills may still name the other ports: they share the phone's state.
OTHER_TOOLS = workbuddy_tests.CODEX_TOOLS + ("AskUserQuestion",)
OTHER_HOSTS = OTHER_TOOLS + ("Codex", "WorkBuddy")


class TextTests(unittest.TestCase):
    def test_every_rewrite_still_matches_upstream(self):
        mcp_stdio.adapt(workbuddy_tests.pristine_server())
        for relative in doubao_text.SKILLS:
            doubao_text.skill_text(relative, (ROOT / relative).read_text())
        doubao_text.screen_html((ROOT / installer.shared.SCREEN_PAGE).read_text())

    def test_server_speaks_of_doubao_work_and_keeps_the_tool_contract(self):
        upstream = workbuddy_tests.pristine_server()
        tools = copy.deepcopy(upstream.TOOLS)
        mcp_stdio.adapt(upstream)
        self.assertIn("interaction.ask", upstream.INSTRUCTIONS)
        self.assertIn("Doubao Work side panel", upstream.INSTRUCTIONS + "".join(tool["description"] for tool in upstream.TOOLS))
        for word in OTHER_HOSTS:
            self.assertNotIn(word, upstream.INSTRUCTIONS + "".join(tool["description"] for tool in upstream.TOOLS))
        # Names, schemas and annotations are what the model calls; _meta is for the host.
        for before, after in zip(tools, upstream.TOOLS):
            self.assertEqual({k: v for k, v in before.items() if k not in ("description", "_meta")},
                             {k: v for k, v in after.items() if k not in ("description", "_meta")})

    def test_only_pua_screen_opens_the_view_and_no_loopback_origin_is_asked_for(self):
        upstream = workbuddy_tests.pristine_server()
        mcp_stdio.adapt(upstream)
        views = {tool["name"]: tool["_meta"]["ui"]["resourceUri"] for tool in upstream.TOOLS if tool.get("_meta", {}).get("ui", {}).get("resourceUri")}
        self.assertEqual(views, {"pua_screen": upstream.SCREEN_URI})
        self.assertRegex(upstream.SCREEN_URI, r"^ui://iphone-use/phone-[\d.]+-doubao-[0-9a-f]{8}\.html$")
        self.assertEqual(upstream.SCREEN_META["ui"]["csp"]["connectDomains"], [])

    def test_phone_is_asked_for_thirty_half_size_frames_and_its_service_outlives_the_server(self):
        upstream = workbuddy_tests.pristine_server()
        mcp_stdio.adapt(upstream)
        settings = sys.modules[upstream.WDAClient.__module__].SESSION_SETTINGS
        self.assertEqual((settings["mjpegServerFramerate"], settings["mjpegScalingFactor"]), (30, 50))
        self.assertIsInstance(sys.modules[upstream.SetupManager.__module__].subprocess, mcp_stdio.workbuddy.WorkerSubprocess)

    def test_page_asks_frames_with_the_widgets_own_call_and_keeps_the_toolbar(self):
        upstream = (ROOT / installer.shared.SCREEN_PAGE).read_text()
        page = doubao_text.screen_html(upstream)
        self.assertLess(page.index(doubao_text.SCREEN_SCRIPT), page.index('<script type="module">'))
        self.assertIn("app.callServerTool(call, options)", doubao_text.SCREEN_SCRIPT)
        for gone in ("readServerResource", "#toolbar"):
            self.assertNotIn(gone, doubao_text.SCREEN_SCRIPT)
        # Nothing else in the page changes.
        inserted = r'if\(\w+&&\w+\.name==="pua_screen_frame"&&!globalThis\.__hostCall\)return globalThis\.__workbuddyFrame\(this,\w+,\w+\);'
        self.assertEqual(len(re.findall(inserted, page)), 1)
        self.assertEqual(re.sub(inserted, "", page.replace(doubao_text.SCREEN_SCRIPT, "", 1)), upstream)
        with self.assertRaises(doubao_text.Drift):
            doubao_text.screen_html("<html></html>")

    def test_skills_name_no_other_host(self):
        for path in (ROOT / "skills").rglob("*.md"):
            relative = path.relative_to(ROOT).as_posix()
            text = doubao_text.skill_text(relative, path.read_text())
            for word in OTHER_TOOLS:
                self.assertNotIn(word, text, relative)
        skill = doubao_text.skill_text("skills/iphone-use/SKILL.md", (ROOT / "skills/iphone-use/SKILL.md").read_text())
        self.assertRegex(skill, r"\A---\nname: iphone-use\ndescription: 用户要在手机 / iPhone 上")
        self.assertIn("mcp__iphone_use__pua_", skill)

    def test_a_list_out_of_step_with_workbuddys_is_reported(self):
        with self.assertRaises(doubao_text.Drift):
            doubao_text.retell(doubao_text.base.INSTRUCTIONS, ["one text for three passages"], "INSTRUCTIONS")

    def test_host_file_supplies_the_tools_a_bare_path_lacks(self):
        with tempfile.TemporaryDirectory() as directory:
            found = Path(directory) / "host.json"
            found.write_text(json.dumps({"path": ["/opt/tools/bin", "/usr/bin"], "env": {"NO_PROXY": "127.0.0.1", "IPHONE_USE_ANALYTICS": "1"}}))
            with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin", "IPHONE_USE_ANALYTICS": "0"}):
                os.environ.pop("NO_PROXY", None)
                mcp_stdio.apply_host_file(found)
                # The installer's directories come first; what the host or the user already set stays.
                self.assertEqual(os.environ["PATH"], "/opt/tools/bin:/usr/bin:/bin")
                self.assertEqual((os.environ["NO_PROXY"], os.environ["IPHONE_USE_ANALYTICS"]), ("127.0.0.1", "0"))
                for broken in ("not json", json.dumps({"path": "x"})):
                    found.write_text(broken)
                    mcp_stdio.apply_host_file(found)
                    self.assertEqual(os.environ["PATH"], "/opt/tools/bin:/usr/bin:/bin")


class Host:
    """One server kept for the whole test, as Doubao Work keeps one for a connector."""

    def __init__(self, case, env):
        self.child = subprocess.Popen([sys.executable, str(ROOT / "doubao/mcp_stdio.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, env=env)
        case.addCleanup(self.close)
        self.ident = 0
        self.ask("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "rmcp", "version": "0"}})

    def ask(self, method, params=None):
        self.ident += 1
        self.child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.ident, "method": method, "params": params or {}}) + "\n")
        self.child.stdin.flush()
        return json.loads(self.child.stdout.readline())

    def frame(self, after_seq=0, **more):
        return self.ask("tools/call", {"name": "pua_screen_frame", "arguments": {"after_seq": after_seq, "last_event_id": 0, **more}})

    def close(self):
        if self.child.poll() is None:
            self.child.stdin.close()
            self.child.wait(timeout=10)
        self.child.stdout.close()


class HostTests(unittest.TestCase):
    """The server as Doubao Work runs it, with a stand-in for the phone's USB screen stream."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="pua", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        (self.state / "config.json").write_text(json.dumps({"udid": "00000000-TESTPHONE0000001"}))
        (self.state / "runtime/forward/node_modules/appium-ios-device").mkdir(parents=True)
        tools = self.state / "bin"
        tools.mkdir()
        (tools / "node").write_text(workbuddy_tests.FAKE_NODE)
        (tools / "node").chmod(0o755)
        self.env = {"PATH": os.pathsep.join([str(tools), *installer.shared.SYSTEM_PATH]), "HOME": str(Path.home()),
                    "IPHONE_USE_STATE_DIR": str(self.state), "IPHONE_USE_ANALYTICS": "0"}

    def first_frame(self, host, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            shown = host.frame()["result"]["structuredContent"]
            if shown["frame"]:
                return shown["frame"]
            time.sleep(0.2)
        self.fail("no frame arrived")

    def test_view_is_handed_the_frames_between_two_polls_by_the_one_server(self):
        host = Host(self, self.env)
        frame = self.first_frame(host)
        self.assertEqual((frame["width"], frame["height"], frame["mimeType"]), (440, 956, "image/jpeg"))
        time.sleep(0.4)
        shown = host.frame(frame["seq"])["result"]["structuredContent"]
        played = shown["more"] + [shown["frame"]]
        # The stand-in stream is 20 frames a second: several frames, in order, none missing.
        self.assertGreater(len(played), 4)
        self.assertEqual([each["seq"] for each in played], list(range(played[0]["seq"], played[-1]["seq"] + 1)))
        self.assertEqual(sorted(each["at"] for each in played), [each["at"] for each in played])
        # No frame source beside the server and no listener for the page: neither is of use in Doubao Work.
        self.assertNotIn("direct", shown)
        self.assertFalse(list(self.state.glob("**/preview.sock")))

    def test_requests_are_traced_by_name_only_and_only_when_switched_on(self):
        Host(self, self.env).frame()
        self.assertFalse((self.state / "doubao").exists())
        (self.state / "doubao").mkdir()
        (self.state / "doubao/trace").touch()
        host = Host(self, self.env)
        for _ in range(3):
            host.frame(note="SECRET-ARGUMENT")
        host.ask("tools/call", {"name": "pua_screen", "arguments": {"action": "SECRET-ARGUMENT"}})
        host.close()
        log = (self.state / "doubao/host.log").read_text()
        noted = [(line["what"], line["detail"]) for line in map(json.loads, log.splitlines())]
        self.assertEqual([what for what, _ in noted], ["start", "initialize", "tools/call", "tools/call", "stdin closed"])
        self.assertEqual(noted[1][1]["clientInfo"], {"name": "rmcp", "version": "0"})
        # Frame polls are four a second: the first is noted, the rest only counted.
        self.assertEqual((noted[2][1], noted[3][1], noted[4][1]), (["pua_screen_frame", 1], "pua_screen", {"frame_requests": 3}))
        self.assertNotIn("SECRET-ARGUMENT", log)


class InstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="doubao install ")
        self.addCleanup(directory.cleanup)
        self.install_dir = Path(directory.name).resolve() / "iphone-use-doubao"

    def test_install_prepares_a_server_that_starts_under_a_bare_path(self):
        result = installer.install(self.install_dir, sys.executable)
        manifest = json.loads((ROOT / "plugin.json").read_text())
        self.assertEqual((result["version"], result["tools"]), (manifest["version"], 19))
        entry = self.install_dir / "doubao/mcp_stdio.py"
        self.assertEqual(result["connector"], {"服务器名称": "iphone_use", "传输类型": "STDIO", "命令": sys.executable, "参数": [str(entry)]})
        found = json.loads((self.install_dir / "doubao/host.json").read_text())
        self.assertTrue(set(installer.shared.SYSTEM_PATH) <= set(found["path"]))
        self.assertEqual(found["env"], {"NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1", "IPHONE_USE_ANALYTICS": "0"})
        # The connector's command itself, with only system directories on PATH.
        bare = {"command": sys.executable, "args": [str(entry)], "env": {"PATH": os.pathsep.join(installer.shared.SYSTEM_PATH)}}
        self.assertEqual(installer.shared.handshake(bare), (manifest["version"], 19))

    def test_analytics_stays_on_only_when_asked(self):
        self.assertNotIn("IPHONE_USE_ANALYTICS", installer.host_file(analytics=True)["env"])

    def test_install_ships_both_ports_runtime_and_doubao_worded_skills(self):
        result = installer.install(self.install_dir, sys.executable)
        for name in ("server/iphone_use.py", "tooling/forward.mjs", "assets/phone-screen.html", "scripts/phone.py", "workbuddy/mcp_server.py",
                     "workbuddy/preview.py", "workbuddy/host_text.py", "doubao/doubao_text.py", installer.MARKER):
            self.assertTrue((self.install_dir / name).is_file(), name)
        for name in installer.shared.CODEX_ONLY + ("doubao/install.py", "workbuddy/install.py", "tests"):
            self.assertFalse((self.install_dir / name).exists(), name)
        self.assertIn(doubao_text.SCREEN_SCRIPT, (self.install_dir / installer.shared.SCREEN_PAGE).read_text())
        self.assertEqual(result["skills"], [str(self.install_dir / "skills/iphone-use"), str(self.install_dir / "skills/iphone-use-setup")])
        for directory in map(Path, result["skills"]):
            skill = (directory / "SKILL.md").read_text()
            # What Doubao Work's upload asks of a skill folder, and this host's wording inside it.
            self.assertRegex(skill, r"\A---\nname: %s\ndescription: \S" % directory.name)
            self.assertIn("## 在豆包工作中使用", skill)
            for word in OTHER_TOOLS:
                self.assertNotIn(word, skill)

    def test_reinstall_replaces_its_own_directory_and_no_one_elses(self):
        installer.install(self.install_dir, sys.executable)
        (self.install_dir / "stale").touch()
        installer.install(self.install_dir, sys.executable)
        self.assertFalse((self.install_dir / "stale").exists())
        (self.install_dir / installer.MARKER).unlink()
        for refused in (lambda: installer.install(self.install_dir, sys.executable), lambda: installer.uninstall(self.install_dir)):
            with self.assertRaises(installer.InstallError):
                refused()
        self.assertTrue((self.install_dir / "doubao/mcp_stdio.py").is_file())

    def test_uninstall_removes_the_installed_server(self):
        self.assertFalse(installer.uninstall(self.install_dir))
        installer.install(self.install_dir, sys.executable)
        self.assertTrue(installer.uninstall(self.install_dir))
        self.assertFalse(self.install_dir.exists())


if __name__ == "__main__":
    unittest.main()
