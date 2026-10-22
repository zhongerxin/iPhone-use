import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import preview_fast


class FakeError(Exception):
    """Shaped like WDAError: the code is what the selector inspects."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakePhone:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def post(self, path, payload, **kwargs):
        self.calls.append((path, payload))
        if self.error is not None:
            raise self.error
        return {}

    def viewport(self, max_age=0):
        self.calls.append(("viewport", max_age))
        return {"width": 390, "height": 844}


class SelectorTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(preview_fast.enabled())
            self.assertEqual(preview_fast.stream_settings(), {})

    def test_truthy_values(self):
        for value in ("1", "true", "TRUE", " on ", "yes"):
            with patch.dict(os.environ, {preview_fast.ENABLED_VAR: value}):
                self.assertTrue(preview_fast.enabled(), value)

    def test_falsy_values(self):
        for value in ("", "0", "false", "off", "no", "enabled"):
            with patch.dict(os.environ, {preview_fast.ENABLED_VAR: value}):
                self.assertFalse(preview_fast.enabled(), value)

    def test_stream_settings_only_when_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(preview_fast.stream_settings(), {})
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            settings = preview_fast.stream_settings()
            self.assertEqual(settings["mjpegServerFramerate"], 24)
            self.assertEqual(settings["mjpegServerScreenshotQuality"], 35)

    def test_stream_settings_returns_a_copy(self):
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            preview_fast.stream_settings()["mjpegServerFramerate"] = 1
            self.assertEqual(preview_fast.stream_settings()["mjpegServerFramerate"], 24)

    def test_gesture_viewport_asks_the_phone_when_disabled(self):
        phone = FakePhone()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(preview_fast.gesture_viewport(phone)["width"], 390)
        self.assertEqual(phone.calls, [("viewport", 0)])

    def test_gesture_viewport_reuses_geometry_when_enabled(self):
        phone = FakePhone()
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            preview_fast.gesture_viewport(phone)
        self.assertEqual(phone.calls, [("viewport", preview_fast.VIEWPORT_MAX_AGE)])

    def test_tap_needs_the_flag(self):
        phone = FakePhone()
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(preview_fast.Unavailable):
                preview_fast.tap(phone, 10, 20)
        self.assertEqual(phone.calls, [])

    def test_tap_uses_the_immediate_route(self):
        phone = FakePhone()
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            preview_fast.tap(phone, 10, 20)
        self.assertEqual(phone.calls, [(preview_fast.TAP_ROUTE, {"x": 10, "y": 20})])

    def test_missing_route_falls_back(self):
        phone = FakePhone(FakeError("http_error", "PUA returned HTTP 404."))
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            with self.assertRaises(preview_fast.Unavailable):
                preview_fast.tap(phone, 10, 20)

    def test_other_errors_are_not_swallowed(self):
        phone = FakePhone(FakeError("timeout", "PUA HTTP request timed out."))
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            with self.assertRaises(FakeError) as caught:
                preview_fast.tap(phone, 10, 20)
        self.assertEqual(caught.exception.code, "timeout")

    def test_drag_converts_and_clamps_the_duration(self):
        phone = FakePhone()
        with patch.dict(os.environ, {preview_fast.ENABLED_VAR: "1"}):
            preview_fast.drag(phone, 1, 2, 3, 4, 400)
            preview_fast.drag(phone, 1, 2, 3, 4, 0)
            preview_fast.drag(phone, 1, 2, 3, 4, 60000)
        durations = [payload["duration"] for _, payload in phone.calls]
        self.assertEqual(durations, [0.4, 0.05, 10.0])


if __name__ == "__main__":
    unittest.main()