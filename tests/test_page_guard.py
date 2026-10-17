import copy
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from test_controller import FakeWDA, node
from wda_client import WDAError
from wda_controller import PhoneController, follow_coordinates, page_change, page_sig


class TestPageGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.client = FakeWDA()
        self.client.close = lambda: None
        self.phone = PhoneController(self.client, self.tmp)

    def tearDown(self):
        self.phone.stop_watcher()
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("IPHONE_USE_PAGE_WATCH", None)

    def observe(self, mode="tree"):
        obs = self.phone.observe(mode)
        if mode in ("tree", "both"):
            obs_id = obs["observation_id"]
            nodes = self.phone.snapshots[obs_id]["nodes"]
            self.phone.set_page_baseline(nodes, obs["viewport"], obs["app"], obs)
        else:
            self.phone.clear_page_baseline()
        return obs

    def test_page_unchanged_and_watcher_fresh_skips_source_read(self):
        """A8 & A11: 页面不变放行且后台新鲜时不读 /source"""
        self.client.nodes = [node("Alpha", y=200), node("Beta", y=300)]
        self.observe("tree")
        self.assertTrue(self.phone.eligible_for_page_check)

        # Simulate fresh watcher check within 1.0s with no change
        self.phone._watcher_state = {
            "t": time.monotonic(),
            "changed": None,
            "tree": (self.client.nodes, {"width": 390, "height": 844}, "com.example.phone"),
            "fails": 0
        }

        source_calls_before = sum(1 for m, p, _ in self.client.calls if p.startswith("/source"))
        res = self.phone.tap(x=100, y=220)
        self.assertTrue(res["action_executed"])

        source_calls_after = sum(1 for m, p, _ in self.client.calls if p.startswith("/source"))
        self.assertEqual(source_calls_before, source_calls_after)
        self.assertFalse(self.phone.eligible_for_page_check)

    def test_scrolled_page_allows_selector_tap(self):
        """A4 & A11: 滚动后按文字 tap 放行"""
        self.client.nodes = [node("First", y=200), node("Target", y=300), node("Third", y=400), node("Fourth", y=500)]
        self.client.elements = [{"id": "tgt", "label": "Target", "kind": "XCUIElementTypeCell",
                                "rect": {"x": 60, "y": 300, "width": 250, "height": 44},
                                "hittable": True, "value": ""}]
        self.observe("tree")

        # Scroll happens: all nodes move by -60pt
        self.client.nodes = [node("First", y=140), node("Target", y=240), node("Third", y=340), node("Fourth", y=440)]
        self.client.elements[0]["rect"]["y"] = 240
        self.phone.stop_watcher()

        res = self.phone.tap(selector={"label": "Target"})
        self.assertTrue(res["action_executed"])
        self.assertFalse(self.phone.eligible_for_page_check)

    def test_scrolled_page_follows_coordinate_tap(self):
        """A5 & A11: 滚动后坐标 tap 跟随"""
        self.client.nodes = [node("Submit", y=200, width=100, height=50)]
        self.observe("tree")

        # Page scrolls: element moves by dy = -40
        self.client.nodes = [node("Submit", y=160, width=100, height=50)]
        self.phone.stop_watcher()

        res = self.phone.tap(x=110, y=220)
        self.assertTrue(res["action_executed"])
        self.assertIn("followed", res)
        self.assertEqual(res["followed"]["text"], "Submit")
        self.assertEqual(res["followed"]["from"], [110, 220])
        self.assertEqual(res["followed"]["to"], [110, 180])
        self.assertEqual(res["followed"]["reason"], "scrolled")

        # Verify the actual tap sent to WDA was at followed coordinates
        tap_calls = [(m, p, b) for m, p, b in self.client.calls if p == "/wda/tap"]
        self.assertEqual(tap_calls[-1][2], {"x": 110, "y": 180})

    def test_title_changed_refuses_gesture_with_new_observation(self):
        """A3, A6 & A11: 标题变化拒绝并附新观察"""
        self.client.nodes = [node("Home", kind="NavigationBar", y=44), node("Row", y=200)]
        obs1 = self.observe("tree")

        # Navigation title changes
        self.client.nodes = [node("Settings", kind="NavigationBar", y=44), node("Row", y=200)]
        self.phone.stop_watcher()

        with self.assertRaises(WDAError) as ctx:
            self.phone.tap(x=100, y=200)

        err = ctx.exception
        self.assertEqual(err.code, "stale_observation")
        self.assertFalse(err.details["action_executed"])
        self.assertEqual(err.details["reason"], "title_changed")
        self.assertIn("observation", err.details)
        new_obs = err.details["observation"]
        self.assertIn("observation_id", new_obs)
        self.assertNotEqual(new_obs["observation_id"], obs1["observation_id"])

        # New observation became the baseline (A6)
        self.assertEqual(self.phone.page_signature["title"], ["Settings"])
        self.assertTrue(self.phone.eligible_for_page_check)

    def test_numbers_and_keyboard_changes_not_treated_as_page_change(self):
        """A1 & A11: 数字和键盘变化不算变化"""
        self.client.nodes = [node("Inbox 3", y=200)]
        self.observe("tree")

        # Number changes (badge counter), keyboard key added at y=600
        self.client.nodes = [node("Inbox 4", y=200), node("a", kind="Key", y=600)]
        self.phone.stop_watcher()

        res = self.phone.tap(x=100, y=210)
        self.assertTrue(res["action_executed"])

    def test_alert_appearance_refuses_gesture(self):
        """A3 & A11: Alert 出现拒绝"""
        self.client.nodes = [node("Home", kind="NavigationBar", y=44), node("Row", y=200)]
        self.observe("tree")

        # Alert appears
        self.client.nodes = [node("Home", kind="NavigationBar", y=44),
                             node("Confirm Delete", kind="Alert", y=300),
                             node("Row", y=200)]
        self.phone.stop_watcher()

        with self.assertRaises(WDAError) as ctx:
            self.phone.tap(x=100, y=200)

        err = ctx.exception
        self.assertEqual(err.code, "stale_observation")
        self.assertFalse(err.details["action_executed"])
        self.assertEqual(err.details["reason"], "popup_changed")
        self.assertIn("observation", err.details)

    def test_alert_dismissal_refuses_gesture(self):
        """A3: Alert 消失拒绝"""
        self.client.nodes = [node("Confirm Delete", kind="Alert", y=300), node("Row", y=200)]
        self.observe("tree")

        # Alert is dismissed
        self.client.nodes = [node("Row", y=200)]
        self.phone.stop_watcher()

        with self.assertRaises(WDAError) as ctx:
            self.phone.tap(x=100, y=200)

        err = ctx.exception
        self.assertEqual(err.code, "stale_observation")
        self.assertFalse(err.details["action_executed"])
        self.assertEqual(err.details["reason"], "popup_changed")

    def test_no_check_after_action_performed(self):
        """A2: 有动作后不检查"""
        self.client.nodes = [node("PageAlpha", kind="NavigationBar", y=44), node("Target", y=200)]
        self.client.elements = [{"id": "tgt", "label": "Target", "kind": "XCUIElementTypeCell",
                                "rect": {"x": 60, "y": 200, "width": 250, "height": 44},
                                "hittable": True, "value": ""}]
        self.observe("tree")

        # Action 1 executes
        res1 = self.phone.tap(selector={"label": "Target"})
        self.assertTrue(res1["action_executed"])
        self.assertFalse(self.phone.eligible_for_page_check)

        # Title changes, but no observation was made since action 1
        self.client.nodes = [node("PageBeta", kind="NavigationBar", y=44), node("Target", y=200)]
        self.phone.stop_watcher()

        # Action 2 executes without being blocked
        res2 = self.phone.tap(selector={"label": "Target"})
        self.assertTrue(res2["action_executed"])

    def test_launch_app_clears_baseline_and_skips_check(self):
        """A12: pua_launch_app 不做页面检查，并清掉页面基线"""
        self.client.nodes = [node("PageAlpha", kind="NavigationBar", y=44), node("Row", y=200)]
        self.observe("tree")

        # Title changed on screen
        self.client.nodes = [node("PageBeta", kind="NavigationBar", y=44), node("Row", y=200)]
        self.phone.stop_watcher()

        res = self.phone.launch_app("com.example.other")
        self.assertTrue(res["action_executed"])
        self.assertIsNone(self.phone.page_signature)
        self.assertFalse(self.phone.eligible_for_page_check)

    def test_action_with_observe_tree_creates_new_baseline(self):
        """A13: 动作附带 observe=tree 时成为新基线并启动比对"""
        from iphone_use import Runtime
        runtime = Runtime(state_dir=self.tmp)
        runtime.client = self.client
        runtime.phone = PhoneController(self.client, self.tmp)

        self.client.nodes = [node("PageAlpha", kind="NavigationBar", y=44), node("Target", y=200)]
        self.client.elements = [{"id": "tgt", "label": "Target", "kind": "XCUIElementTypeCell",
                                "rect": {"x": 60, "y": 200, "width": 250, "height": 44},
                                "hittable": True, "value": ""}]

        res = runtime.call("pua_tap", {"selector": {"label": "Target"}, "observe": "tree"})
        self.assertTrue(res["action_executed"])
        self.assertIn("observation", res)
        self.assertIsNotNone(runtime.phone.page_signature)
        self.assertTrue(runtime.phone.eligible_for_page_check)
        runtime.close()

    def test_observe_screenshot_does_not_create_baseline(self):
        """A13: 只截图的观察不建基线"""
        self.observe("screenshot")
        self.assertIsNone(self.phone.page_signature)
        self.assertFalse(self.phone.eligible_for_page_check)

    def test_coordinate_closed_interval_and_tie_breaking(self):
        """A14: 闭区间判断及并列最小框位移相同跟随/不同拒绝"""
        viewport = {"width": 390, "height": 844}
        old_sig = {
            "v": 2,
            "title": ["Test"],
            "modal": [],
            "anchors": {
                "Box A": [100, 200, 50, 30, "Box A"],
                "Box B": [100, 200, 50, 30, "Box B"]
            }
        }

        # Closed interval: exact edge (150, 230) is inside [100, 150] x [200, 230]
        # Both boxes have same displacement (dx=0, dy=-20)
        new_sig_same = {
            "v": 2,
            "title": ["Test"],
            "modal": [],
            "anchors": {
                "Box A": [100, 180, 50, 30, "Box A"],
                "Box B": [100, 180, 50, 30, "Box B"]
            }
        }
        res = follow_coordinates(old_sig, new_sig_same, 150, 230, viewport)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], 150)
        self.assertEqual(res[1], 210)

        # Different displacements between tied boxes -> must refuse
        new_sig_diff = {
            "v": 2,
            "title": ["Test"],
            "modal": [],
            "anchors": {
                "Box A": [100, 180, 50, 30, "Box A"],
                "Box B": [100, 170, 50, 30, "Box B"]
            }
        }
        res_diff = follow_coordinates(old_sig, new_sig_diff, 150, 230, viewport)
        self.assertIsNone(res_diff)

    def test_page_watch_env_disabled(self):
        """A9: IPHONE_USE_PAGE_WATCH=0 关闭后台线程"""
        os.environ["IPHONE_USE_PAGE_WATCH"] = "0"
        self.client.nodes = [node("Home", kind="NavigationBar", y=44), node("Row", y=200)]
        self.observe("tree")
        self.assertIsNone(self.phone._watcher_thread)

        # Still performs pre-gesture check by reading page directly
        self.client.nodes = [node("Settings", kind="NavigationBar", y=44), node("Row", y=200)]
        with self.assertRaises(WDAError) as ctx:
            self.phone.tap(x=100, y=200)
        self.assertEqual(ctx.exception.details["reason"], "title_changed")

    def test_swipe_without_region_allowed_with_region_refused_on_scroll(self):
        """A4: 滚动后不带 region swipe 照常，带 region swipe 拒绝"""
        self.client.nodes = [node("Alpha", y=100), node("Beta", y=200), node("Gamma", y=300), node("Delta", y=400)]
        self.observe("tree")

        self.client.nodes = [node("Alpha", y=50), node("Beta", y=150), node("Gamma", y=250), node("Delta", y=350)]
        self.phone.stop_watcher()

        # Swipe without region succeeds
        res = self.phone.swipe("up")
        self.assertTrue(res["action_executed"])

        # Observe again and scroll again
        self.observe("tree")
        self.client.nodes = [node("Alpha", y=20), node("Beta", y=120), node("Gamma", y=220), node("Delta", y=320)]
        self.phone.stop_watcher()

        # Swipe with explicit region is refused
        with self.assertRaises(WDAError) as ctx:
            self.phone.swipe("up", region={"x": 50, "y": 50, "width": 200, "height": 200})
        self.assertEqual(ctx.exception.code, "stale_observation")
        self.assertEqual(ctx.exception.details["reason"], "scrolled")

    def test_type_text_and_press_button_allowed_on_scroll(self):
        """A4: 滚动后 type_text 与 press_button 照常执行"""
        self.client.nodes = [node("Alpha", y=100), node("Beta", y=200), node("Gamma", y=300), node("Delta", y=400)]
        self.client.elements = [{"id": "fld", "label": "Input", "kind": "XCUIElementTypeTextField",
                                "rect": {"x": 60, "y": 100, "width": 250, "height": 44},
                                "hittable": True, "value": ""}]
        self.observe("tree")

        self.client.nodes = [node("Alpha", y=50), node("Beta", y=150), node("Gamma", y=250), node("Delta", y=350)]
        self.phone.stop_watcher()

        res_type = self.phone.type_text(selector={"label": "Input"}, text="Test Text")
        self.assertTrue(res_type["action_executed"])

        self.observe("tree")
        self.client.nodes = [node("Alpha", y=20), node("Beta", y=120), node("Gamma", y=220), node("Delta", y=320)]
        self.phone.stop_watcher()

        res_btn = self.phone.press_button("volumeup")
        self.assertTrue(res_btn["action_executed"])

    def test_background_watcher_detects_change_and_stops(self):
        """A7: 遇到第一次变化时后台比对线程停止"""
        self.client.nodes = [node("Home", kind="NavigationBar", y=44), node("Row", y=200)]
        self.observe("tree")
        self.assertIsNotNone(self.phone._watcher_thread)

        # Change node to Settings
        self.client.nodes = [node("Settings", kind="NavigationBar", y=44), node("Row", y=200)]

        # Wait for watcher to detect change (at 20ms intervals)
        for _ in range(50):
            if self.phone._watcher_state and self.phone._watcher_state.get("changed") == "title_changed":
                break
            time.sleep(0.01)

        self.assertEqual(self.phone._watcher_state.get("changed"), "title_changed")
        # Watcher thread should have exited
        self.phone._watcher_thread.join(timeout=1.0)
        self.assertFalse(self.phone._watcher_thread.is_alive())

    def test_background_watcher_stops_on_five_fails(self):
        """A7: 连续 5 次 WDA 失败时停止"""
        self.client.nodes = [node("Home", kind="NavigationBar", y=44)]
        self.observe("tree")

        # Make source fail
        def broken_source():
            raise WDAError("unknown error", "WDA failure")
        self.client.source = broken_source

        # Wait for 5 failures to accumulate
        for _ in range(50):
            if self.phone._watcher_state and self.phone._watcher_state.get("fails") >= 5:
                break
            time.sleep(0.01)

        self.assertEqual(self.phone._watcher_state.get("fails"), 5)
        self.phone._watcher_thread.join(timeout=1.0)
        self.assertFalse(self.phone._watcher_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
