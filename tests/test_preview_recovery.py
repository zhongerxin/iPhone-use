"""Unlock recovery uses real shared preview state and a fake WDA channel."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime
from wda_client import WDAError
from wda_controller import PhoneController
from wda_screen import ScreenHub
from test_controller import FakeWDA
from test_screen import jpeg


class PreviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Runtime(directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = Mock()
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, directory.name)
        self.runtime.phone.screen = self.runtime.screen
        self.hub = self.runtime.screen
        self.other = ScreenHub(directory.name)
        self.addCleanup(self.other.close)
        self.runtime.setup_manager.mirroring_running = Mock(return_value=False)

    def ready(self):
        return self.runtime.call("pua_ready", {"screenshot": False, "recover": False})

    def lock_failure(self):
        self.client.locked = True
        with self.assertRaises(WDAError) as caught:
            self.ready()
        self.assertEqual(caught.exception.code, "phone_locked")

    def test_unlock_ready_clears_shared_lock_pause_and_allows_new_pixels(self):
        self.lock_failure()
        self.assertEqual(self.other.start()["pause_reason"], "device_locked")
        self.assertIsNone(self.other.start()["frame"])
        self.client.locked = False
        result = self.ready()
        self.assertTrue(result["ready"])
        self.assertEqual(result["preview"], {"paused": False, "pause_reason": None})
        self.assertFalse(self.other.paused())
        self.other._publish_frame(jpeg(), 440, 956, self.other._stop)
        self.assertTrue(self.other._wire(self.other._read_state())["frame_available"])
        # READY may create a session, but performs no user gesture or input.
        self.assertEqual([p for _, p, _ in self.client.actions()], ["/session"])
        self.assertFalse(any(p == "/screenshot" for _, p, _ in self.client.calls))

    def test_authentication_and_legacy_pause_require_explicit_resume(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                self.hub.set_paused(False)
                if legacy:
                    self.hub._state_path.write_text(json.dumps({"paused": True}))
                else:
                    self.runtime.call("pua_screen", {"action": "pause"})
                reason = "unknown" if legacy else "authentication"
                before = list(self.client.calls)
                for name, args in (("pua_ready", {"screenshot": False, "recover": False}),
                                   ("pua_screen_action", {"action": "refresh"})):
                    with self.assertRaises(WDAError) as caught:
                        self.runtime.call(name, args)
                    self.assertEqual(caught.exception.code, "access_paused")
                self.assertEqual(self.client.calls, before)
                self.assertEqual(self.other.start()["pause_reason"], reason)
                self.assertTrue(self.runtime.call("pua_screen", {})["paused"])
                self.runtime.identify_device = Mock()
                with patch("subprocess.run") as process:
                    result = self.runtime.call("pua_screen", {"action": "resume"})
                process.assert_not_called()
                self.assertFalse(result["paused"])
                self.assertFalse(self.hub.paused())
                self.assertEqual(self.client.calls, before)

    def test_failed_health_check_does_not_clear_a_lock_pause(self):
        self.lock_failure()
        self.client.locked = False
        self.client.status_ready = False
        with self.assertRaises(WDAError):
            self.ready()
        self.assertEqual(self.hub.pause_status(), {"paused": True, "pause_reason": "device_locked"})

    def test_new_pause_during_unlock_check_cannot_be_cleared(self):
        for reason in ("authentication", "device_locked"):
            with self.subTest(reason=reason):
                self.hub.set_paused(False)
                self.lock_failure()
                self.client.locked = False
                observe = self.runtime.phone.observe
                def concurrent_pause(*args, **kwargs):
                    result = observe(*args, **kwargs)
                    self.other.set_paused(True, reason=reason)
                    return result
                with patch.object(self.runtime.phone, "observe", side_effect=concurrent_pause):
                    with self.assertRaises(WDAError) as caught:
                        self.ready()
                self.assertEqual(caught.exception.code, "access_paused")
                self.assertTrue(caught.exception.details["response_suppressed"])
                self.assertNotIn("observation", caught.exception.details)
                self.assertEqual(self.other.pause_status(), {"paused": True, "pause_reason": reason})

    def test_refresh_cannot_clear_a_device_lock_pause_even_after_unlock(self):
        self.lock_failure()
        self.client.locked = False
        before = list(self.client.calls)
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_screen_action", {"action": "refresh"})
        self.assertEqual(caught.exception.code, "access_paused")
        self.assertEqual(self.client.calls, before)
        self.assertEqual(self.hub.pause_status(), {"paused": True, "pause_reason": "device_locked"})
        self.assertTrue(self.ready()["ready"])
        self.assertFalse(self.hub.paused())

    def test_pause_generation_is_private_and_survives_activity_transactions(self):
        self.hub.set_paused(True, reason="device_locked")
        identifier = self.hub.locked_pause_id()
        token = self.other.begin("ready")
        self.other.end(token)
        self.other.set_viewport({"width": 440, "height": 956})
        self.assertEqual(self.other.locked_pause_id(), identifier)
        self.assertNotIn(identifier, json.dumps(self.hub.start()))
        self.assertTrue(self.other.resume_after_unlock(identifier))
        self.assertFalse(self.hub.resume_after_unlock(identifier))

    def test_no_focus_returns_visual_replan_without_typing_or_proposed_old_point(self):
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_type_text", {"text": "query"})
        error = caught.exception
        self.assertEqual(error.code, "no_focused_field")
        self.assertFalse(error.details["action_executed"])
        recovery = error.details["recovery"]
        self.assertIn("popup", recovery["next_step"])
        self.assertIn("do not repeat", recovery["next_step"])
        self.assertIn("image(block)", recovery["next_step"])
        self.assertNotIn("next_arguments", recovery)
        self.assertTrue(Path(error.details["observation"]["image"]["path"]).is_file())
        self.assertEqual(self.client.actions(), [])
        self.assertFalse(self.hub.paused())


if __name__ == "__main__":
    unittest.main()
