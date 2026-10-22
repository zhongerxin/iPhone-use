import base64
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from wda_client import WDAError
from wda_controller import PhoneController, WDAController


def make_banner_xml(bundle_id="com.apple.springboard", banner_type="XCUIElementTypeBanner",
                    banner_label="Notification", text_label="TIKTOK, now, New message",
                    y=50, width=370, height=80, own_text=None):
    child = f'<XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="{text_label}" x="20" y="{y+10}" width="300" height="30"/>' if text_label else ""
    label_attr = f'label="{banner_label}"' if banner_label else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="{bundle_id}" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="390" height="844">
    <{banner_type} type="{banner_type}" {label_attr} x="10" y="{y}" width="{width}" height="{height}">
      {child}
    </{banner_type}>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


def make_app_xml(bundle_id="com.example.app", label="Welcome to App"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="{bundle_id}" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="390" height="844">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="{label}" x="20" y="100" width="200" height="40"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


def make_lock_screen_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="com.apple.springboard" x="0" y="0" width="390" height="844">
  <XCUIElementTypeWindow type="XCUIElementTypeWindow" x="0" y="0" width="390" height="844">
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="9:41" x="150" y="100" width="90" height="40"/>
    <XCUIElementTypeStaticText type="XCUIElementTypeStaticText" label="Wednesday, March 18" x="120" y="150" width="150" height="20"/>
    <XCUIElementTypeButton type="XCUIElementTypeButton" label="Flashlight" x="50" y="750" width="50" height="50"/>
  </XCUIElementTypeWindow>
</XCUIElementTypeApplication>"""


class FakeWDAClient:
    def __init__(self):
        self.calls = []
        self.actions = []
        self.session_id = "fake-session"
        self.app = "com.example.app"
        self.size = {"width": 390, "height": 844}
        self.source_sequence = []
        self.active_app_sequence = []
        self.screenshot = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXuoAAAAASUVORK5CYII=")

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            self.actions.append((method, path, copy.deepcopy(payload)))
        if path == "/wda/activeAppInfo":
            app = self.active_app_sequence.pop(0) if self.active_app_sequence else self.app
            return {"value": {"bundleId": app}}
        if path == "/wda/homescreen":
            self.app = "com.apple.springboard"
            return {"value": None}
        if path == "/screenshot":
            return {"value": base64.b64encode(self.screenshot).decode("ascii")}
        if path == "/wda/locked":
            return {"value": False}
        return {"value": None}

    def session(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            self.actions.append((method, path, copy.deepcopy(payload)))
        if path.startswith("/source"):
            if self.source_sequence:
                return self.source_sequence.pop(0)
            return make_app_xml(self.app)
        if path == "/window/size":
            return self.size.copy()
        if path == "/wda/dragfromtoforduration":
            return None
        if path == "/wda/tap":
            return None
        if path == "/wda/apps/activate":
            if payload and "bundleId" in payload:
                self.app = payload["bundleId"]
            return None
        return None


class TestBannerSweep(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.client = FakeWDAClient()
        self.phone = PhoneController(self.client, self.directory.name)
        self.sleep_patcher = patch("time.sleep")
        self.mock_sleep = self.sleep_patcher.start()

    def tearDown(self):
        self.sleep_patcher.stop()
        self.directory.cleanup()

    def test_wda_controller_alias(self):
        self.assertIs(WDAController, PhoneController)

    def test_banner_swept_and_original_app_tree_returned(self):
        """A1, A3, A4, A6: Banner identified, dragged off, original app tree returned with banners_dismissed."""
        self.phone._expected_app = "com.example.app"
        # 1st source: springboard banner; 2nd source: restored app
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, New message from Alice", y=50, width=370, height=80),
            make_app_xml(bundle_id="com.example.app", label="Original App Content"),
        ]

        result = self.phone.observe("tree")

        # Verify drag was executed from banner center to y-40, duration 0.1
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 1)
        _, _, payload = drags[0]
        self.assertEqual(payload["fromX"], 195.0)  # 10 + 370 / 2
        self.assertEqual(payload["fromY"], 90.0)   # 50 + 80 / 2
        self.assertEqual(payload["toX"], 195.0)
        self.assertEqual(payload["toY"], 50.0)     # 90 - 40
        self.assertEqual(payload["duration"], 0.1)

        # Verify wait of 0.35s
        self.mock_sleep.assert_called_with(0.35)

        # Verify banners_dismissed contents
        self.assertEqual(result.get("banners_dismissed"), ["TIKTOK, now, New message from Alice"])
        self.assertEqual(result["app"], "com.example.app")
        self.assertTrue(any(n.get("label") == "Original App Content" for n in result["nodes"]))
        self.assertEqual(self.phone._source_app, "com.example.app")

    def test_banners_dismissed_contents_and_deduplication(self):
        """A4: Deduplication and order preservation."""
        self.phone._expected_app = "com.example.app"
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, Message 1"),
            make_banner_xml(text_label="WECHAT, now, Message 2"),
            make_banner_xml(text_label="TIKTOK, now, Message 1"),  # Duplicate
            make_app_xml(),
        ]

        result = self.phone.observe("tree")
        self.assertEqual(result.get("banners_dismissed"), [
            "TIKTOK, now, Message 1",
            "WECHAT, now, Message 2",
        ])

    def test_no_banner_dismissed_key_when_not_dismissed(self):
        """A4: No banners_dismissed key if no banners were dismissed."""
        self.phone._expected_app = "com.example.app"
        self.client.source_sequence = [make_app_xml()]

        result = self.phone.observe("tree")
        self.assertNotIn("banners_dismissed", result)

    def test_banners_dismissed_cleared_per_top_level_tool_call(self):
        """A12: banners_dismissed cleared per top-level tool call."""
        self.phone._expected_app = "com.example.app"
        # 1st call dismisses a banner
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, Message 1"),
            make_app_xml(),
        ]
        res1 = self.phone.observe("tree")
        self.assertEqual(res1.get("banners_dismissed"), ["TIKTOK, now, Message 1"])

        # 2nd call does not dismiss any banner
        self.client.source_sequence = [make_app_xml()]
        res2 = self.phone.observe("tree")
        self.assertNotIn("banners_dismissed", res2)
        self.assertEqual(self.phone.banners_dismissed, [])

    def test_banners_dismissed_only_in_top_level_result_not_in_observation(self):
        """A8: banners_dismissed in top-level tool result, not repeated inside observation sub-dict."""
        self.phone._expected_app = "com.example.app"
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, Alert"),
            make_app_xml(),
        ]

        result = self.phone.tap(x=100, y=200, observe="tree")
        self.assertEqual(result.get("banners_dismissed"), ["TIKTOK, now, Alert"])
        self.assertIn("observation", result)
        self.assertNotIn("banners_dismissed", result["observation"])

    def test_expected_app_is_springboard_no_action(self):
        """A2, A6: Expected app is springboard -> do not sweep banner."""
        self.phone._expected_app = "com.apple.springboard"
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, Ignored"),
        ]

        result = self.phone.observe("tree")
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 0)
        self.assertNotIn("banners_dismissed", result)
        self.assertEqual(result["app"], "com.apple.springboard")

    def test_lock_screen_not_identified_as_banner(self):
        """A3, A6: Lock screen tree (no banner elements) is not treated as banner."""
        self.phone._expected_app = "com.example.app"
        self.client.source_sequence = [make_lock_screen_xml()]

        result = self.phone.observe("tree")
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 0)
        self.assertNotIn("banners_dismissed", result)

    def test_sweep_max_three_times_and_returns_springboard_tree(self):
        """A3, A6, A11: Retries up to 3 times; if still springboard, returns it and keeps expected app."""
        self.phone._expected_app = "com.example.app"
        # Always returns banner XML
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, Persistent 1"),
            make_banner_xml(text_label="TIKTOK, now, Persistent 2"),
            make_banner_xml(text_label="TIKTOK, now, Persistent 3"),
            make_banner_xml(text_label="TIKTOK, now, Persistent 4"),
        ]

        result = self.phone.observe("tree")
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 3)
        self.assertEqual(self.phone._expected_app, "com.example.app")  # Expected foreground app unchanged
        self.assertEqual(result["app"], "com.apple.springboard")

    def test_banner_label_fallback_to_own_label_or_none(self):
        """A9: No comma in label falls back to own label; if no own label, records nothing but still sweeps."""
        self.phone._expected_app = "com.example.app"
        # Case 1: banner has own label without comma, child has no comma
        self.client.source_sequence = [
            make_banner_xml(banner_label="CustomNotification", text_label="SimpleAlert"),
            make_app_xml(),
        ]
        res1 = self.phone.observe("tree")
        self.assertEqual(res1.get("banners_dismissed"), ["CustomNotification"])

        # Case 2: banner has no label and child has no comma
        self.client.actions.clear()
        self.client.source_sequence = [
            make_banner_xml(banner_label="", text_label=""),
            make_app_xml(),
        ]
        res2 = self.phone.observe("tree")
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 1)  # Drag still performed
        self.assertNotIn("banners_dismissed", res2)

    def test_banner_y_boundary(self):
        """A1: Banner with y <= 180 is identified, y > 180 is not."""
        self.phone._expected_app = "com.example.app"
        # y = 180 -> recognized as banner
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, At 180", y=180),
            make_app_xml(),
        ]
        res1 = self.phone.observe("tree")
        self.assertEqual(res1.get("banners_dismissed"), ["TIKTOK, now, At 180"])

        # y = 181 -> not recognized as banner
        self.client.actions.clear()
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, At 181", y=181),
        ]
        res2 = self.phone.observe("tree")
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 0)
        self.assertNotIn("banners_dismissed", res2)

    def test_active_app_unexpected_springboard_sweeps_banner(self):
        """A10: active_app reads springboard unexpectedly -> runs tree sweep first."""
        self.phone._expected_app = "com.example.app"
        self.client.active_app_sequence = ["com.apple.springboard"]
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, ActiveAppSweep"),
            make_app_xml(bundle_id="com.example.app"),
        ]

        app = self.phone.active_app()
        drags = [c for c in self.client.actions if c[1] == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 1)
        self.assertEqual(app, "com.example.app")

    def test_page_guard_and_no_app_changed_when_banner_swept(self):
        """A5: Page guard does not fail with foreground_changed or app_changed when banner swept."""
        viewport_data = {"width": 390, "height": 844, "units": "iPhone points"}
        obs_id = self.phone.remember([], viewport_data, "com.example.app")
        self.phone._expected_app = "com.example.app"

        # When guard calls active_app(), it returns springboard, which sweeps banner and restores app
        self.client.active_app_sequence = ["com.apple.springboard"]
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, GuardSweep"),
            make_app_xml(),
        ]

        # guard() should not raise stale foreground_changed
        viewport = self.phone.guard(obs_id)
        self.assertEqual(viewport["width"], 390)

    def test_expected_app_lifecycle(self):
        """A7: launch_app updates it, press_button('home') sets springboard, first observation sets it."""
        # 1. Initially None
        self.assertIsNone(self.phone._expected_app)

        # 2. First non-springboard observation sets it
        self.client.source_sequence = [make_app_xml(bundle_id="com.first.app")]
        self.phone.observe("tree")
        self.assertEqual(self.phone._expected_app, "com.first.app")

        # 3. Subsequent observations do not overwrite it
        self.client.source_sequence = [make_app_xml(bundle_id="com.second.app")]
        self.phone.observe("tree")
        self.assertEqual(self.phone._expected_app, "com.first.app")

        # 4. launch_app updates it
        self.phone.launch_app("com.launched.app")
        self.assertEqual(self.phone._expected_app, "com.launched.app")

        # 5. press_button('home') sets com.apple.springboard
        self.phone.press_button("home")
        self.assertEqual(self.phone._expected_app, "com.apple.springboard")

    def test_batch_tool_result_banners_dismissed(self):
        """A4, A8: Batch tool result contains banners_dismissed at top level, not in step results."""
        self.phone._expected_app = "com.example.app"
        self.client.source_sequence = [
            make_banner_xml(text_label="TIKTOK, now, BatchBanner"),
            make_app_xml(),
        ]

        batch_result = self.phone.batch([
            {"op": "tap", "args": {"x": 100, "y": 200, "observe": "tree"}},
        ])
        self.assertEqual(batch_result.get("banners_dismissed"), ["TIKTOK, now, BatchBanner"])
        step_res = batch_result["results"][0]
        self.assertNotIn("banners_dismissed", step_res)
        self.assertNotIn("banners_dismissed", step_res.get("observation", {}))


if __name__ == "__main__":
    unittest.main()
