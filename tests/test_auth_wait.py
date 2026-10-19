"""Automatic handoff checks must keep credentials and stale work inaccessible."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime, tool_result, VERSION
from wda_auth import AuthWaiter
from wda_client import WDAError
from test_controller import FakeWDA


class ProbeClient(FakeWDA):
    def __init__(self):
        super().__init__()
        self.compact = False
        self.blocker = False
        self.page = True
        self.empty = False
        self.rich = False
        self.fail_restore = False
        self.bulk_query_stalls = False
        self.visibility_first_stalls = False
        self.depth = 50
        self.on_query = lambda: None

    def close(self):
        pass

    def request(self, method, path, payload=None, timeout=None):
        if path.endswith("/appium/settings"):
            self.calls.append((method, path, payload))
            if method == "POST":
                value = payload["settings"]["shouldUseCompactResponses"]
                if not value and self.fail_restore:
                    raise WDAError("pua_timeout", "private backend text")
                self.compact = value
            return {"value": {"shouldUseCompactResponses": self.compact, "snapshotMaxDepth": self.depth, "snapshotMaxChildren": 2147483647}}
        if path.endswith(("/element", "/elements")):
            self.calls.append((method, path, payload))
            assert self.compact, "Query without compact responses"
            self.on_query()
            if self.bulk_query_stalls and path.endswith("/elements"):
                raise WDAError("action_uncertain", "bulk element identity serialization timed out", uncertain=True)
            predicate = payload["value"]
            if self.visibility_first_stalls and predicate.startswith("visible"):
                raise WDAError("action_uncertain", "unrelated element visibility timed out", uncertain=True)
            found = not self.empty if "TabBar" in predicate else self.blocker if "Keyboard" in predicate else self.page
            if self.rich:
                return {"value": {"ELEMENT": "id", "value": "private input"} if path.endswith("/element") else [{"ELEMENT": "id", "value": "private input"}]}
            if path.endswith("/element"):
                if not found:
                    raise WDAError("no such element", "private backend text")
                return {"value": {"ELEMENT": "id"}}
            return {"value": [{"ELEMENT": "id"}] if found else []}
        if path not in ("/status", "/wda/locked", "/wda/activeAppInfo"):
            raise AssertionError("Forbidden auth request: " + path)
        return super().request(method, path, payload, timeout)


class AuthWaitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Runtime(directory.name)
        self.addCleanup(self.runtime.close)
        self.runtime.identify_device = Mock()
        self.client = ProbeClient()
        self.runtime.screen.set_paused(True)
        (self.runtime.state_dir / "session.json").write_text(json.dumps({"url": self.runtime.base_url, "session_id": "test-session"}))
        factory = patch("wda_auth.WDAClient", side_effect=lambda *a, **k: self.client)
        mock = factory.start()
        mock.remaining.side_effect = lambda deadline: max(.001, deadline-time.monotonic())
        self.addCleanup(factory.stop)

    def wait(self, **args):
        return self.runtime.call("pua_screen", {"action": "wait_auth", "bundle_id": self.client.app,
                                 "ready_label": "Account overview", "timeout": 2, **args})

    def test_phone_completion_resumes_without_dialog_or_user_answer(self):
        with patch("subprocess.run") as dialog:
            result = self.wait()
        self.assertTrue(result["resumed"])
        self.assertFalse(result["paused"])
        self.assertFalse(result["authentication_verified"])
        self.assertFalse(self.client.compact)
        dialog.assert_not_called()
        self.assertNotIn("nodes", result)
        self.assertNotIn("frame", result)
        self.assertEqual(sum(path == "/status" for _, path, _ in self.client.calls), 2)

    def test_auto_resume_when_bulk_identity_and_unfiltered_visibility_stall(self):
        # Real Settings page: /elements serializes many matched element IDs and
        # exceeds the deadline; existence needs only one matching element.
        self.client.bulk_query_stalls = True
        self.client.visibility_first_stalls = True
        result = self.wait()
        self.assertTrue(result.get("resumed"), result)
        self.assertFalse(result["paused"])
        self.assertFalse(self.client.compact)

    def test_minimum_wait_budget_can_collect_two_successful_observations(self):
        result = self.wait(timeout=1)
        self.assertTrue(result.get("resumed"), result)
        self.assertFalse(result["paused"])
        self.assertEqual(sum(path == "/status" for _, path, _ in self.client.calls), 2)

    def test_wait_auth_uses_phone_evidence_without_dialog(self):
        with patch("subprocess.run") as dialog:
            result = self.wait(action="wait_auth")
        dialog.assert_not_called()
        self.assertTrue(result["resumed"])
        self.assertFalse(result["confirmation_required"])
        self.assertEqual(result["runtime_version"], VERSION)

    def test_legacy_resume_alias_uses_the_same_phone_evidence_without_dialog(self):
        with patch("subprocess.run") as dialog:
            result = self.wait(action="resume")
        dialog.assert_not_called()
        self.assertTrue(result["resumed"])
        self.assertFalse(result["paused"])
        self.assertFalse(result["confirmation_required"])
        self.assertEqual(result["runtime_version"], VERSION)
        self.assertFalse(self.client.compact)
        self.assertEqual(sum(path == "/status" for _, path, _ in self.client.calls), 2)

    def test_resume_cannot_bypass_a_failed_automatic_check(self):
        for attr, value in (("locked", True), ("app", "other.app"), ("blocker", True), ("page", False)):
            with self.subTest(attr=attr):
                old = getattr(self.client, attr)
                setattr(self.client, attr, value)
                result = self.wait(action="resume", bundle_id="com.example.phone", timeout=1)
                self.assertEqual(result["state"], "waiting_auth")
                self.assertTrue(result["paused"])
                self.assertTrue(self.runtime.screen.paused())
                self.assertFalse(self.client.compact)
                setattr(self.client, attr, old)
        result = self.runtime.call("pua_screen", {"action": "resume"})
        self.assertEqual(result["state"], "auth_target_required")
        self.assertFalse(result["confirmation_required"])
        self.assertTrue(self.runtime.screen.paused())

    def test_resume_when_not_paused_keeps_the_preview_status_shape_without_phone_requests(self):
        self.runtime.screen.set_paused(False)
        result = self.runtime.call("pua_screen", {"action": "resume"})
        status = self.runtime.call("pua_screen", {})
        self.assertEqual(result["state"], "not_paused")
        self.assertFalse(result["paused"])
        self.assertFalse(result["confirmation_required"])
        self.assertTrue(set(status) <= set(result), set(status) - set(result))
        self.assertEqual(self.client.calls, [])

    def test_locked_wrong_app_blocker_missing_marker_and_empty_tree_keep_pause(self):
        for attr, value in (("locked", True), ("app", "other.app"), ("blocker", True), ("page", False), ("empty", True)):
            with self.subTest(attr=attr):
                old = getattr(self.client, attr)
                setattr(self.client, attr, value)
                result = self.wait(bundle_id="com.example.phone", timeout=1)
                self.assertEqual(result["state"], "waiting_auth")
                self.assertTrue(self.runtime.screen.paused())
                self.assertFalse(self.client.compact)
                setattr(self.client, attr, old)

    def test_restore_failure_and_noncompact_response_do_not_leak_or_resume(self):
        for attr in ("fail_restore", "rich"):
            with self.subTest(attr=attr):
                setattr(self.client, attr, True)
                wire = tool_result(self.runtime, {"name": "pua_screen", "arguments": {
                    "action": "wait_auth", "bundle_id": self.client.app, "ready_label": "Account overview", "timeout": 1}})
                self.assertNotIn("private", json.dumps(wire))
                self.assertTrue(self.runtime.screen.paused())
                setattr(self.client, attr, False)
                self.client.compact = False

    def test_new_pause_during_probe_cannot_be_cleared(self):
        self.client.on_query = lambda: self.runtime.screen.set_paused(True)
        result = self.wait()
        self.assertEqual(result["state"], "pause_changed")
        self.assertTrue(self.runtime.screen.paused())

    def test_device_unlock_needs_no_app_queries(self):
        self.runtime.screen.set_paused(False)
        self.runtime.screen.set_paused(True, reason="device_locked")
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "timeout": 2})
        self.assertTrue(result["resumed"])
        self.assertEqual({p for _, p, _ in self.client.calls}, {"/status", "/wda/locked"})

    def test_no_marker_requires_target_without_phone_requests(self):
        result = self.runtime.call("pua_screen", {"action": "wait_auth"})
        self.assertEqual(result["state"], "auth_target_required")
        self.assertEqual(self.client.calls, [])

    def test_auth_heading_must_disappear_without_claiming_success(self):
        self.client.page = True
        result = self.wait(auth_label="Enter password", timeout=1)
        self.assertTrue(result["paused"])
        self.client.page = False
        result = self.runtime.call("pua_screen", {"action": "wait_auth", "bundle_id": self.client.app,
                                   "auth_label": "Enter password", "timeout": 2})
        self.assertTrue(result["resumed"])
        self.assertFalse(result["authentication_verified"])

    def test_stale_work_remains_blocked_after_automatic_resume(self):
        epoch = self.runtime.access.capture_epoch()
        self.assertTrue(self.wait()["resumed"])
        with self.assertRaises(WDAError) as error:
            self.runtime.call("pua_tap", {"x": 1, "y": 1}, received_epoch=epoch)
        self.assertEqual(error.exception.code, "access_paused")

    def test_session_from_other_endpoint_stays_paused(self):
        (self.runtime.state_dir / "session.json").write_text(json.dumps({"url": "http://127.0.0.1:9999", "session_id": "old"}))
        self.assertEqual(self.wait()["error_code"], "auth_session_required")
        self.assertTrue(self.runtime.screen.paused())

    def test_shallow_snapshot_cannot_prove_auth_heading_disappeared(self):
        self.client.depth = 1
        result = self.wait(auth_label="Enter password")
        self.assertEqual(result["error_code"], "auth_snapshot_incomplete")
        self.assertTrue(self.runtime.screen.paused())
        self.assertFalse(any(p.endswith(("/element", "/elements")) for _, p, _ in self.client.calls))


if __name__ == "__main__":
    unittest.main()
