import fcntl
import tempfile
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime, TOOLS
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


HID_PATH = "/wda/performIoHidEvent"


class ScreenshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(self.directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = lambda: None
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, self.directory.name)
        self.runtime.phone.screen = self.runtime.screen

    def test_capture_uses_device_event_without_downloading_pixels_or_claiming_photo_verification(self):
        tool = next(tool for tool in TOOLS if tool["name"] == "pua_screenshot")
        self.assertFalse(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["destructiveHint"])
        self.assertFalse(tool["annotations"]["idempotentHint"])
        result = self.runtime.call("pua_screenshot", {})
        self.assertTrue(result["screenshot_requested"])
        self.assertTrue(result["action_complete"])
        self.assertFalse(result["verified"])
        self.assertNotIn("image", result)
        self.assertEqual(self.client.actions(), [
            ("POST", HID_PATH, {"page": 12, "usage": 101, "duration": 0.005})])
        self.assertFalse(any(path in ("/source", "/screenshot") for _, path, _ in self.client.calls))

    def test_locked_phone_refuses_capture(self):
        self.client.locked = True
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_screenshot", {})
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(self.client.actions(), [])

    def test_authentication_pause_refuses_capture(self):
        self.runtime.screen.set_paused(True)
        with self.assertRaises(WDAError) as caught:
            self.runtime.call("pua_screenshot", {})
        self.assertEqual(caught.exception.code, "preview_paused")
        self.assertEqual(self.client.actions(), [])

    def test_shared_operation_lock_refuses_concurrent_capture(self):
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(WDAError) as caught:
                self.runtime.call("pua_screenshot", {})
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertEqual(self.client.actions(), [])

    def test_invalid_arguments_fail_before_capture(self):
        for args in ({"observe": "invalid"}, {"page": 12}):
            with self.subTest(args=args), self.assertRaises(WDAError) as caught:
                self.runtime.call("pua_screenshot", args)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(self.client.calls, [])

    def test_uncertain_capture_is_not_replayed(self):
        original = self.client.session
        def session(method, path, payload=None, timeout=None):
            result = original(method, path, payload, timeout)
            if path == HID_PATH:
                raise WDAError("action_uncertain", "Timeout after sending event", uncertain=True)
            return result
        with patch.object(self.client, "session", side_effect=session):
            with self.assertRaises(WDAError) as caught:
                self.runtime.call("pua_screenshot", {})
        self.assertEqual(caught.exception.code, "action_uncertain")
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual([path for _, path, _ in self.client.actions()], [HID_PATH])


if __name__ == "__main__":
    unittest.main()
