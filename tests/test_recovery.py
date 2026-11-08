"""Bounded WDA channel recovery without repeating the user's phone actions."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime
from wda_client import WDAClient, WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(self.directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = Mock()
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, self.directory.name)
        self.manager = Mock()
        self.manager.mirroring_running.return_value = False
        self.manager.pending_recovery.return_value = None
        self.manager.recover.return_value = {"ok": True, "job_id": "recovery-test", "recovery": {"state": "queued"}}
        self.runtime.setup_manager = self.manager

    def assert_error(self, code, tool="pua_ready", **args):
        with self.assertRaises(WDAError) as caught:
            self.runtime.call(tool, args)
        self.assertEqual(caught.exception.code, code)
        return caught.exception.as_dict()

    def assert_no_phone_mutation(self):
        self.assertEqual([path for _, path, _ in self.client.actions() if path != "/session"], [])

    def test_transient_foreground_failure_retries_only_fresh_read_once(self):
        original = self.client.request
        attempts = []

        def request(method, path, payload=None, timeout=None):
            if path == "/wda/activeAppInfo":
                attempts.append(path)
                if len(attempts) == 1:
                    self.client.calls.append((method, path, payload))
                    return {"value": {"bundleId": "local.pid.0"}}
            return original(method, path, payload, timeout)

        with patch.object(self.client, "request", side_effect=request):
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["foreground_resolved"])
        self.assertTrue(result["recovery"]["session_recreated"])
        self.assertFalse(result["recovery"]["replayed_action"])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 2)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_persistent_foreground_error_queues_recovery_and_explicit_poll_guidance(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assert_pending_state(result)
        self.assertEqual(result["category"], "channel_runtime")
        self.assertTrue(result["session_read_retried"])
        self.assertEqual(result["cause"]["code"], "pua_foreground_unavailable")
        self.assertEqual(result["recovery"]["status_tool"], "pua_setup")
        self.assertEqual(result["recovery"]["status_arguments"], {"action": "status", "job_id": "recovery-test"})
        self.assertEqual(result["recovery"]["next_tool"], "pua_ready")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.manager.recover.assert_called_once_with()
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 2)
        self.assert_no_phone_mutation()

    def test_local_pid_stale_error_from_source_uses_same_bounded_recovery(self):
        fault = WDAError("stale element reference", "Application local.pid.0 is not running")
        with patch.object(self.client, "source", side_effect=fault) as source:
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(source.call_count, 2)
        self.assert_pending_state(result)
        self.assertTrue(result["session_read_retried"])
        self.manager.recover.assert_called_once_with()
        self.assert_no_phone_mutation()

    def test_diagnose_mode_explains_runtime_failure_without_service_restart(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_not_ready_state(result, "recovery_required")
        self.assertEqual(result["reason"], "recovery_disabled")
        self.assertTrue(result["session_read_retried"])
        self.assertEqual(result["category"], "channel_runtime")
        self.assertEqual(result["cause"]["code"], "pua_foreground_unavailable")
        self.assertEqual(result["recovery"]["state"], "disabled")
        self.assertEqual(result["recovery"]["next_tool"], "pua_ready")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.assertIn("user", result["recovery"]["permission_note"].lower())
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_authorization_code_41_skips_session_read_retry_and_queues_recovery(self):
        fault = WDAError("unknown error", "Error Domain=XCTDaemonErrorDomain Code=41 Not authorized for performing UI testing actions")
        with patch.object(self.client, "source", side_effect=fault) as source:
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(source.call_count, 1)
        self.assert_pending_state(result)
        self.assertFalse(result["session_read_retried"])
        self.manager.recover.assert_called_once_with()
        self.assert_no_phone_mutation()

    def test_unproven_service_owner_never_claims_recovery_started(self):
        self.client.app = "local.pid.0"
        self.manager.recover.return_value = {"ok": False, "error": "owned service not found", "next_steps": ["Use the original owner to restart WDA"]}
        error = self.assert_error("pua_recovery_required", screenshot=False)
        self.assertFalse(error["ready"])
        self.assertIn("owned service", error["message"])
        self.assertNotIn("job_id", error["recovery"])
        self.assertFalse(error["recovery"]["replay_action"])
        self.assert_no_phone_mutation()

    def test_pending_service_recovery_handles_temporarily_unreachable_status(self):
        self.manager.pending_recovery.return_value = {"job_id": "in-flight", "state": "restarting"}
        fault = WDAError("pua_unreachable", "Service is restarting")
        with patch.object(self.client, "request", side_effect=fault):
            result = self.runtime.call("pua_ready", {"screenshot": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["job_id"], "in-flight")
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_cold_start_failure_directs_setup_without_starting_or_claiming_ready(self):
        for code in ("pua_unreachable", "not_ready"):
            for recover in (True, False):
                with self.subTest(code=code, recover=recover):
                    fault = WDAError(code, "Service is not accepting connections")
                    with patch.object(self.client, "request", side_effect=fault):
                        error = self.assert_error(code, screenshot=False, recover=recover)
                    self.assertFalse(error["ready"])
                    self.assertFalse(error["action_executed"])
                    self.assertTrue(error["initialization_required"])
                    self.assertEqual(error["recovery"]["next_tool"], "pua_setup")
                    self.assertEqual(error["recovery"]["next_arguments"], {"action": "status"})
                    self.assertFalse(error["recovery"]["replay_action"])
                    self.assertNotIn("proof", error)
                    self.manager.recover.assert_not_called()
                    self.manager.dispatch.assert_not_called()
                    self.assert_no_phone_mutation()

    def test_ready_timeout_guides_delayed_ready_retry_without_setup_or_restart(self):
        for screenshot in (True, False):
            for recover in (True, False):
                for readonly in (True, False):
                    with self.subTest(screenshot=screenshot, recover=recover, readonly=readonly):
                        fault = WDAClient.transport_error(TimeoutError("slow"), readonly)
                        with patch.object(self.client, "request", side_effect=fault) as request:
                            error = self.assert_error("pua_timeout" if readonly else "action_uncertain",
                                                      screenshot=screenshot, recover=recover)
                        self.assertFalse(error["ready"])
                        self.assertFalse(error["action_executed"])
                        self.assertFalse(error["initialization_required"])
                        self.assertEqual(error["recovery"]["next_tool"], "pua_ready")
                        self.assertEqual(error["recovery"]["next_arguments"], {"screenshot": screenshot, "recover": recover})
                        self.assertEqual(error["recovery"]["max_wait_retries"], 2)
                        self.assertGreater(error["recovery"]["retry_after_seconds"], 0)
                        self.assertFalse(error["recovery"]["replay_action"])
                        self.assertNotIn("proof", error)
                        request.assert_called_once()
                        self.manager.recover.assert_not_called()
                        self.manager.dispatch.assert_not_called()
                        self.assert_no_phone_mutation()

    def test_ready_source_timeout_can_succeed_on_a_later_call(self):
        fault = WDAClient.transport_error(TimeoutError("slow snapshot"), readonly=True)
        original = self.client.source
        with patch.object(self.client, "source", side_effect=[fault, original()]) as source:
            error = self.assert_error("pua_timeout", screenshot=False)
            self.assertEqual(source.call_count, 1)
            result = self.runtime.call(error["recovery"]["next_tool"], error["recovery"]["next_arguments"])
        self.assertTrue(result["ready"])
        self.assertEqual(source.call_count, 2)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_timeout_after_foreground_read_retry_still_guides_wait_not_restart(self):
        foreground = WDAError("pua_foreground_unavailable", "Application local.pid.0 unavailable")
        timeout = WDAClient.transport_error(TimeoutError("slow snapshot"), readonly=True)
        with patch.object(self.client, "source", side_effect=[foreground, timeout]):
            error = self.assert_error("pua_timeout", screenshot=False, recover=False)
        self.assertEqual(error["recovery"]["next_tool"], "pua_ready")
        self.assertEqual(error["recovery"]["next_arguments"], {"screenshot": False, "recover": False})
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_action_observation_timeout_preserves_executed_action_and_wait_guidance(self):
        fault = WDAClient.transport_error(TimeoutError("slow snapshot"), readonly=True)
        with patch.object(self.client, "source", side_effect=fault):
            error = self.assert_error("pua_timeout", tool="pua_tap", selector={"label": "Target"}, observe="tree")
        self.assertTrue(error["action_executed"])
        self.assertFalse(error["action_complete"])
        self.assertEqual(error["recovery"]["next_tool"], "pua_observe")
        self.assertFalse(error["recovery"]["replay_action"])
        self.assertEqual(sum(path.endswith("/click") for _, path, _ in self.client.actions()), 1)
        self.manager.recover.assert_not_called()

    def assert_not_ready_state(self, result, state):
        self.assertFalse(result["ready"])
        self.assertEqual(result["state"], state)
        self.assertFalse(result["action_executed"])
        self.assertNotIn("error", result)
        self.assertNotIn("proof", result)
        self.assertNotIn("observation", result)
        self.assertNotIn("image", result)
        self.assertFalse(result["recovery"]["replay_action"])

    def assert_pending_state(self, result):
        self.assert_not_ready_state(result, "recovering")
        self.assertIn("job_id", result["recovery"])
        self.assertIsNone(self.client.session_id)
        self.assertEqual(self.runtime.phone.snapshots, {})

    def test_diagnostic_recovery_guidance_preserves_requested_screenshot(self):
        self.client.app = "local.pid.0"
        result = self.runtime.call("pua_ready", {"screenshot": True, "recover": False})
        self.assert_not_ready_state(result, "recovery_required")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": True, "recover": True})
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_pending_recovery_keeps_diagnostic_mode_in_next_ready_arguments(self):
        self.manager.pending_recovery.return_value = {"job_id": "already-running", "state": "restarting"}
        fault = WDAError("pua_unreachable", "Service is restarting")
        with patch.object(self.client, "request", side_effect=fault):
            result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": False})
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_healthy_ready_contains_state_and_does_not_try_recovery(self):
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["foreground_resolved"])
        self.assertGreater(result["observation"]["total_nodes"], 0)
        self.assertNotIn("error", result)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_pending_recovery_never_proves_ready_from_old_healthy_listener(self):
        self.manager.pending_recovery.return_value = {"job_id": "waiting-to-stop-old-service", "state": "queued"}
        self.client.session_id = "old-session"
        self.runtime.phone.snapshots["old-observation"] = {"nodes": []}
        result = self.runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assert_pending_state(result)
        self.assertEqual(result["recovery"]["job_id"], "waiting-to-stop-old-service")
        self.assertEqual(result["recovery"]["next_arguments"], {"screenshot": False, "recover": False})
        self.assertEqual(self.client.calls, [])
        self.assertFalse(result["session_read_retried"])
        self.manager.recover.assert_not_called()

    def test_recovery_cooldown_remains_actionable_error(self):
        self.client.app = "local.pid.0"
        self.manager.recover.return_value = {"ok": False, "error": "Recovery cooldown is active", "recovery": {"state": "cooldown", "retry_after_seconds": 120}}
        error = self.assert_error("pua_recovery_required", screenshot=False)
        self.assertEqual(error["recovery"]["state"], "cooldown")
        self.assertEqual(error["recovery"]["retry_after_seconds"], 120)
        self.assertNotIn("job_id", error["recovery"])
        self.assertFalse(error["action_executed"])
        self.assert_no_phone_mutation()

    def test_locked_phone_never_recreates_session_or_restarts_service(self):
        self.client.locked = True
        error = self.assert_error("phone_locked", screenshot=False)
        self.assertEqual(error["code"], "phone_locked")
        self.assertIsNone(self.client.session_id)
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_phone_lock_during_foreground_retry_stops_recovery(self):
        original = self.client.request

        def request(method, path, payload=None, timeout=None):
            result = original(method, path, payload, timeout)
            if path == "/wda/activeAppInfo":
                self.client.locked = True
                return {"value": {"bundleId": "local.pid.0"}}
            return result

        with patch.object(self.client, "request", side_effect=request):
            self.assert_error("phone_locked", screenshot=False)
        self.assertEqual(sum(path == "/session" for _, path, _ in self.client.calls), 1)
        self.assertIsNone(self.client.session_id)
        self.manager.recover.assert_not_called()
        self.assert_no_phone_mutation()

    def test_generic_stale_element_is_not_misclassified_as_channel_failure(self):
        fault = WDAError("stale element reference", "Previously found element Button 'Continue' is not present")
        with patch.object(self.client, "source", side_effect=fault) as source:
            error = self.assert_error("stale element reference", screenshot=False)
        self.assertEqual(source.call_count, 1)
        self.assertNotIn("category", error)
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_observe_foreground_failure_points_to_ready_without_restart(self):
        self.client.app = "local.pid.0"
        error = self.assert_error("pua_foreground_unavailable", tool="pua_observe", mode="tree")
        self.assertFalse(error["action_executed"])
        self.assertEqual(error["category"], "channel_runtime")
        self.assertEqual(error["recovery"]["tool"], "pua_ready")
        self.assertFalse(error["recovery"]["replay_action"])
        self.manager.recover.assert_not_called()
        self.client.close.assert_not_called()
        self.assert_no_phone_mutation()

    def test_successful_click_followed_by_stale_source_does_not_replay_click_or_restart(self):
        fault = WDAError("stale element reference", "Application local.pid.0 is not running")
        with patch.object(self.client, "source", side_effect=fault) as source:
            error = self.assert_error("stale element reference", tool="pua_tap", selector={"label": "Target"}, observe="tree")
        self.assertEqual(source.call_count, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])
        self.assertEqual(self.runtime.phone.accepted_actions, 1)
        self.assertTrue(error["action_executed"])
        self.assertFalse(error["action_complete"])
        self.assertIn("verification_required", error)
        self.manager.recover.assert_not_called()


if __name__ == "__main__":
    unittest.main()
