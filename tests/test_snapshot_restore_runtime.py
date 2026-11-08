"""A failed bounded snapshot cannot silently cap another shared Runtime."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime
from wda_client import SESSION_SETTINGS, WDAError
from test_client import fake_server


XML = ('<XCUIElementTypeApplication type="XCUIElementTypeApplication" '
       'bundleId="com.example.phone" width="390" height="844">'
       '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Target" '
       'x="20" y="100" width="100" height="40" enabled="true"/>'
       '</XCUIElementTypeApplication>')


class SnapshotRestoreRuntimeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        environment = patch.dict(os.environ, {"IPHONE_USE_ANALYTICS": "0"})
        environment.start()
        self.addCleanup(environment.stop)
        self.native_settings = dict(SESSION_SETTINGS)
        self.source_settings = []
        self.journal_events = []
        server = fake_server(self.respond)
        self.url, self.requests, _ = server.__enter__()
        self.addCleanup(server.__exit__, None, None, None)
        self.first = self.runtime()
        self.peer = self.runtime()
        self.restore = {"snapshotMaxDepth": SESSION_SETTINGS["snapshotMaxDepth"],
                        "snapshotMaxChildren": SESSION_SETTINGS["snapshotMaxChildren"]}

    def runtime(self):
        runtime = Runtime(self.directory, self.url)
        runtime.screen = Mock()
        runtime.phone.screen = runtime.screen
        runtime.client.session_id = runtime.client._settings_session_id = "shared-session"
        runtime.phone._viewport_cache = (time.monotonic(), {
            "width": 390, "height": 844, "units": "iPhone points"})
        self.addCleanup(runtime.close)
        return runtime

    def respond(self, method, path, body, requests):
        if path.endswith("/appium/settings"):
            if method == "POST":
                self.journal_events.append(("settings", dict(body["settings"]), self.cache()))
                self.native_settings.update(body["settings"])
            value = dict(self.native_settings)
        elif "/source?" in path:
            self.journal_events.append(("source", None, self.cache()))
            self.source_settings.append(dict(self.native_settings))
            value = XML
        elif path == "/status":
            value = {"ready": True}
        elif path == "/wda/locked":
            value = False
        elif path.endswith("/window/size"):
            value = {"width": 390, "height": 844}
        elif path.endswith("/actions"):
            self.journal_events.append(("pointer", dict(self.native_settings), self.cache()))
            value = None
        else:
            raise AssertionError("Unexpected fake WDA request: " + path)
        return 200, {"value": value}, 0, False

    def cache(self):
        return json.loads((self.directory / "session.json").read_text())

    def fail_bounded_snapshot(self):
        fault = WDAError("pua_timeout", "Native snapshot exceeded its read deadline")
        with patch.object(self.first.phone, "tree", side_effect=fault):
            with self.assertRaises(WDAError) as caught:
                self.first.call("pua_observe", {"snapshot_max_depth": 20,
                                                 "snapshot_max_children": 40})
        self.assertIs(caught.exception, fault)
        self.assertEqual(self.native_settings["snapshotMaxDepth"], 20)
        self.assertEqual(self.native_settings["snapshotMaxChildren"], 40)

    def test_failed_bounded_snapshot_persists_exact_pending_restore(self):
        self.fail_bounded_snapshot()
        self.assertEqual(self.cache(), {"url": self.url, "session_id": "shared-session",
                                       "settings_restore": self.restore})
        self.assertEqual((self.directory / "session.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual([method for method, _, _ in self.requests], ["GET", "POST"])
        self.assertEqual(self.source_settings, [])

    def test_already_warmed_peer_restores_before_ordinary_tree(self):
        self.fail_bounded_snapshot()
        self.requests.clear()
        self.assertEqual(self.peer.client._settings_session_id, "shared-session")
        self.assertEqual(self.peer.client._settings_restore, {})
        observed = self.peer.call("pua_observe", {})
        self.assertEqual(self.requests[0], ("POST", "/session/shared-session/appium/settings",
            {"settings": {**SESSION_SETTINGS, **self.restore}}))
        self.assertIn("/source?", self.requests[1][1])
        self.assertEqual(self.source_settings, [dict(SESSION_SETTINGS)])
        self.assertEqual(observed["total_nodes"], 1)
        self.assertFalse(observed["truncated"])
        self.assertNotIn("snapshot_limits", observed)
        self.assertEqual(self.peer.client._settings_restore, {})
        self.assertEqual(self.cache(), {"url": self.url, "session_id": "shared-session"})

    def test_health_only_call_retains_pending_restore_for_later_peer_read(self):
        self.fail_bounded_snapshot()
        self.requests.clear()
        with patch.object(self.peer, "_call", side_effect=lambda name, args:
                          self.peer.client.request("GET", "/status").get("value")):
            self.assertTrue(self.peer.call("pua_metrics", {})["ready"])
        self.assertEqual(self.requests, [("GET", "/status", None)])
        self.assertEqual(self.cache()["settings_restore"], self.restore)
        self.assertEqual(self.peer.client._settings_restore, self.restore)
        self.assertEqual(self.native_settings["snapshotMaxChildren"], 40)
        self.requests.clear()
        self.peer.call("pua_observe", {})
        self.assertTrue(self.requests[0][1].endswith("/appium/settings"))
        self.assertEqual(self.source_settings, [dict(SESSION_SETTINGS)])
        self.assertNotIn("settings_restore", self.cache())

    def test_preview_tap_restores_failed_snapshot_before_pointer_action(self):
        self.fail_bounded_snapshot()
        self.requests.clear()
        self.peer.screen.paused.return_value = False
        result = self.peer.call("pua_screen_action", {
            "action": "tap", "x": 50, "y": 120, "width": 390, "height": 844})
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.requests[0], ("POST", "/session/shared-session/appium/settings",
            {"settings": {**SESSION_SETTINGS, **self.restore}}))
        self.assertEqual(self.requests[1], ("GET", "/wda/locked", None))
        pointers = [event for event in self.journal_events if event[0] == "pointer"]
        self.assertEqual(len(pointers), 1)
        self.assertEqual(pointers[0][1], dict(SESSION_SETTINGS))
        self.assertNotIn("settings_restore", self.cache())

    def test_preview_tap_does_not_execute_when_snapshot_restore_fails(self):
        self.fail_bounded_snapshot()
        self.requests.clear()
        self.peer.screen.paused.return_value = False
        request = self.peer.client.request
        def fail_restore(method, path, *args, **kwargs):
            if path.endswith("/appium/settings"):
                raise WDAError("pua_timeout", "Settings restoration timed out")
            return request(method, path, *args, **kwargs)
        with patch.object(self.peer.client, "request", side_effect=fail_restore):
            with self.assertRaises(WDAError):
                self.peer.call("pua_screen_action", {
                    "action": "tap", "x": 50, "y": 120, "width": 390, "height": 844})
        self.assertFalse(any(path.endswith("/actions") for _, path, _ in self.requests))
        self.assertEqual(self.cache()["settings_restore"], self.restore)

    def test_journal_precedes_native_apply_and_survives_until_restore_response(self):
        limits = {"snapshotMaxDepth": 20, "snapshotMaxChildren": 40}
        observed = self.first.call("pua_observe", {"snapshot_max_depth": 20,
                                                   "snapshot_max_children": 40})
        pending = {"url": self.url, "session_id": "shared-session",
                   "settings_restore": self.restore}
        self.assertEqual(self.journal_events, [
            ("settings", limits, pending),
            ("source", None, pending),
            ("settings", self.restore, pending),
        ])
        self.assertEqual(self.source_settings, [{**SESSION_SETTINGS, **limits}])
        self.assertEqual(self.native_settings, dict(SESSION_SETTINGS))
        self.assertEqual(observed["snapshot_limits"], limits)
        self.assertEqual(self.cache(), {"url": self.url, "session_id": "shared-session"})

    def test_keyboard_interrupt_leaves_journal_and_warmed_peer_restores(self):
        with patch.object(self.first.phone, "tree", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.first.call("pua_observe", {"snapshot_max_children": 40})
        restore = {"snapshotMaxChildren": SESSION_SETTINGS["snapshotMaxChildren"]}
        self.assertEqual(self.cache()["settings_restore"], restore)
        self.assertEqual(self.first.client._settings_restore, restore)
        self.assertEqual(self.native_settings["snapshotMaxChildren"], 40)
        self.requests.clear()
        self.peer.call("pua_observe", {})
        self.assertEqual(self.requests[0], ("POST", "/session/shared-session/appium/settings",
            {"settings": {**SESSION_SETTINGS, **restore}}))
        self.assertEqual(self.source_settings, [dict(SESSION_SETTINGS)])
        self.assertNotIn("settings_restore", self.cache())

    def test_hard_process_exit_preserves_prewrite_journal_for_warmed_peer(self):
        script = "\n".join([
            "import os, sys",
            "from unittest.mock import Mock",
            "sys.path.insert(0, sys.argv[1])",
            "from iphone_use import Runtime",
            "r = Runtime(sys.argv[2], sys.argv[3])",
            "r.screen = Mock()",
            "r.phone.screen = r.screen",
            "r.client.session_id = r.client._settings_session_id = 'shared-session'",
            "r.phone.tree = lambda *args: os._exit(73)",
            "r.call('pua_observe', {'snapshot_max_children': 40})",
        ])
        process = subprocess.run([sys.executable, "-c", script, str(ROOT / "server"),
                                  str(self.directory), self.url], capture_output=True,
                                 text=True, timeout=10)
        self.assertEqual(process.returncode, 73, process.stderr)
        restore = {"snapshotMaxChildren": SESSION_SETTINGS["snapshotMaxChildren"]}
        self.assertEqual(self.cache()["settings_restore"], restore)
        self.assertEqual(self.native_settings["snapshotMaxChildren"], 40)
        self.assertEqual(self.peer.client._settings_restore, {})
        self.requests.clear()
        self.peer.call("pua_observe", {})
        self.assertEqual(self.requests[0], ("POST", "/session/shared-session/appium/settings",
            {"settings": {**SESSION_SETTINGS, **restore}}))
        self.assertEqual(self.source_settings, [dict(SESSION_SETTINGS)])
        self.assertNotIn("settings_restore", self.cache())


if __name__ == "__main__":
    unittest.main()
