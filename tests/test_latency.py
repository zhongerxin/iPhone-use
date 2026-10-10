"""Regression tests for the 0.2.0 latency work.

They pin the three levers that work targets: how much each result puts into the model
context, how many model round trips a step needs, and bounded waits with no silent timeout.
"""
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime, result_content, serve, tool_result
from wda_client import WDAError
from wda_controller import PhoneController, compact_node
import wda_image
import wda_text
from test_controller import FakeWDA, node


def png(width, height):
    """A valid grayscale PNG of the given size, built without an image library."""
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    rows = (b"\x00" + bytes(width)) * height
    return (wda_image.PNG_SIGNATURE + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 1)) + chunk(b"IEND", b""))


class PhoneCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = FakeWDA()
        self.phone = PhoneController(self.client, self.directory.name)

    def assert_code(self, code, operation):
        with self.assertRaises(WDAError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def paths(self):
        return [path for _, path, _ in self.client.calls]


class CompactObservationTests(PhoneCase):
    def test_excluding_accessible_preserves_nested_content_and_controls(self):
        # Containers/children with accessible=false must stay in the tree. Dropping
        # the output attribute must not change bill text, field values or geometry.
        root=ET.Element("XCUIElementTypeApplication", {
            "type":"XCUIElementTypeApplication", "bundleId":"com.example.phone",
            "x":"0", "y":"0", "width":"390", "height":"844"})
        container=ET.SubElement(root,"XCUIElementTypeOther", {
            **{k:str(v) for k,v in node("账单",kind="Other",x=0,y=100,width=390,height=600).items()},
            "accessible":"false"})
        for item in (node("咖啡店，-28.00元，09-01",accessible=True),
                     node("咖啡店",kind="StaticText",y=250,accessible=False),
                     node("搜索",kind="TextField",y=310,name="bill-search",value="咖啡",enabled=False,accessible=False),
                     node("账单",kind="Button",y=400,enabled=True,accessible=True)):
            ET.SubElement(container,item["type"],{
                k:str(v).lower() if isinstance(v,bool) else str(v) for k,v in item.items()})
        legacy=ET.tostring(root,encoding="unicode")
        for element in root.iter():element.attrib.pop("accessible",None)
        lean=ET.tostring(root,encoding="unicode")
        self.assertLess(len(lean),len(legacy))
        for visibility in (False,True):
            with self.subTest(expensive_visibility=visibility):
                with patch.object(self.client,"source",return_value=legacy):
                    before=self.phone.observe(expensive_visibility=visibility)
                self.client.calls.clear()
                with patch.object(self.client,"source",return_value=lean):
                    after=self.phone.observe(expensive_visibility=visibility)
                self.assertEqual(after["nodes"],before["nodes"])
                self.assertEqual(after["total_nodes"],5)
                self.assertEqual(after["app"],before["app"])
                self.assertEqual(after["viewport"],before["viewport"])
                self.assertFalse(after["truncated"])
                self.assertEqual(after["nodes"][3],{
                    "type":"TextField", "label":"搜索", "name":"bill-search",
                    "value":"咖啡", "enabled":False, "rect":[60,310,250,44]})
                expected="accessible" if visibility else "visible,accessible"
                self.assertIn("/source?format=xml&excluded_attributes="+expected,self.paths())

    def test_node_keeps_each_fact_once(self):
        self.client.nodes = [
            node("返回", kind="Button", enabled=True),
            node("余额 1,234.56", y=300, kind="StaticText", value="余额 1,234.56"),
            {**node("通用", y=400), "name": "General", "value": "1", "enabled": False, "x": 16.4, "width": 250.6},
        ]
        observed = self.phone.observe()
        self.assertEqual(observed["nodes"], [
            {"type": "Button", "label": "返回", "rect": [60, 200, 250, 44]},
            {"type": "StaticText", "label": "余额 1,234.56", "rect": [60, 300, 250, 44]},
            {"type": "Cell", "label": "通用", "name": "General", "value": "1", "enabled": False, "rect": [16, 400, 251, 44]},
        ])
        self.assertEqual(observed["total_nodes"], 3)
        self.assertFalse(observed["truncated"])

    def test_typical_node_is_several_times_smaller_than_the_wda_attributes(self):
        native = {"type": "XCUIElementTypeButton", "name": "返回", "label": "返回", "enabled": "true",
                  "rect": {"x": 16.0, "y": 62.0, "width": 44.0, "height": 44.0}, "in_viewport": True}
        before = len(json.dumps(native, ensure_ascii=False))
        after = len(json.dumps(compact_node(native), ensure_ascii=False, separators=(",", ":")))
        self.assertLess(after * 2.5, before)

    def test_observation_carries_no_constant_prose_or_unread_fields(self):
        observed = self.phone.observe()
        self.assertEqual(set(observed), {"observation_id", "observed_at", "app", "viewport", "nodes", "total_nodes", "truncated"})
        self.assertRegex(observed["observed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(len(observed["observation_id"]), 12)
        self.assertEqual(set(self.phone.snapshots[observed["observation_id"]]), {"time", "viewport", "app", "nodes"})
        self.assertTrue(self.phone.observe(expensive_visibility=True)["visibility_computed"])

    def test_empty_tree_still_explains_itself(self):
        self.client.nodes = []
        observed = self.phone.observe()
        self.assertEqual(observed["nodes"], [])
        self.assertIn("Empty accessibility tree", observed["warnings"][0])

    def test_departures_from_defaults_stay_visible(self):
        self.client.nodes = [node("On screen"), node("Below", y=2000), {**node("Hidden", y=300), "visible": False}]
        observed = self.phone.observe(include_invisible=True, expensive_visibility=True)
        self.assertEqual([n.get("in_viewport", True) for n in observed["nodes"]], [True, False, True])
        self.assertEqual([n.get("visible", True) for n in observed["nodes"]], [True, True, False])
        self.assertEqual([n["label"] for n in self.phone.observe()["nodes"]], ["On screen"])

    def test_queries_and_collections_use_the_same_compact_nodes(self):
        self.client.rich_elements = True
        found = self.phone.find({"label": "Target"})
        self.assertEqual(found, {"matches": 1, "truncated": False, "elements": [
            {"index": 0, "type": "TextField", "label": "Target", "rect": [70, 200, 250, 44], "tap": [195, 222]}]})
        located = self.phone.scroll_find({"label": "Target"}, max_swipes=0)
        self.assertEqual(located["target"], {"index": 0, "type": "TextField", "label": "Target", "rect": [70, 200, 250, 44], "tap": [195, 222], "hittable": True})
        self.assertNotIn("element_id", json.dumps(located))
        rows = self.phone.collect_list(max_pages=1)["rows"]
        self.assertEqual(rows, [{"type": "Cell", "label": "Row 1", "rect": [60, 200, 250, 44]}])

    def test_wait_honours_a_selector_index(self):
        self.assertTrue(self.phone.wait({"label": "Target", "index": 0}, timeout_seconds=0)["verified"])
        self.assert_code("postcondition_failed", lambda: self.phone.wait({"label": "Target", "index": 1}, timeout_seconds=0))


class LongInputTests(PhoneCase):
    def test_long_text_is_typed_in_short_requests_with_matching_timeouts(self):
        text = "中文 mix 输入。" * 45
        self.assertEqual(len(text), 450)
        result = self.phone.type_text({"label": "Target"}, text)
        self.assertTrue(result["action_complete"])
        self.assertEqual(result["characters"], 450)
        self.assertNotIn("continue_token", result)
        self.assertEqual(self.client.elements[0]["value"], text)
        typing = [(path, timeout) for _, path, timeout in self.client.timeouts if path.endswith("/value") or path == "/wda/keys"]
        self.assertEqual([path for path, _ in typing], ["/element/target/value", "/wda/keys", "/wda/keys"])
        # 200 characters at 30 per second need 6.7 s; the old fixed 15 s covered about 450 in one request.
        self.assertAlmostEqual(typing[0][1], 200 / 30 * 2 + 10)
        self.assertEqual(typing[2][1], 15)
        sent = [body for _, path, body in self.client.actions() if path.endswith("/value") or path == "/wda/keys"]
        self.assertTrue(all(body["frequency"] == 30 for body in sent))
        self.assertFalse(any(path.endswith("/attribute/value") for path in self.paths()))

    def test_short_text_is_still_one_request(self):
        self.phone.type_text({"label": "Target"}, "x" * 200)
        self.assertEqual([path for _, path, _ in self.client.actions()],
                         ["/element/target/click", "/element/target/clear", "/element/target/value"])

    def test_budget_hands_back_a_token_and_finishes_with_the_original_options(self):
        self.phone.call_budget = 0
        text = "".join(chr(0x4E00 + i % 500) for i in range(500))
        first = self.phone.type_text({"label": "Target"}, text, verify=True, submit=True, observe="tree")
        self.assertEqual((first["input_complete"], first["action_complete"], first["submitted"]), (False, False, False))
        self.assertEqual((first["characters"], first["remaining_characters"]), (200, 300))
        self.assertNotIn("observation", first)
        self.assertFalse(any(body == {"value": ["\n"]} for _, _, body in self.client.actions()))
        second = self.phone.type_text(continue_token=first["continue_token"])
        self.assertEqual((second["characters"], second["remaining_characters"]), (400, 100))
        self.assertNotEqual(second["continue_token"], first["continue_token"])
        final = self.phone.type_text(continue_token=second["continue_token"])
        self.assertTrue(final["action_complete"])
        self.assertTrue(final["exact_readback"])
        self.assertTrue(final["submitted"])
        self.assertIn("nodes", final["observation"])
        self.assertEqual(self.client.elements[0]["value"], text)
        actions = self.client.actions()
        self.assertEqual(actions[-1][2], {"value": ["\n"]})
        self.assertEqual(sum(body == {"value": ["\n"]} for _, _, body in actions), 1)
        self.assertEqual(sum(path.endswith("/clear") for _, path, _ in actions), 1)
        self.assertEqual(sum(path.endswith("/attribute/value") for path in self.paths()), 1)
        self.assert_code("input_continuation_expired", lambda: self.phone.type_text(continue_token=second["continue_token"]))

    def test_another_phone_action_invalidates_the_continuation(self):
        self.phone.call_budget = 0
        first = self.phone.type_text({"label": "Target"}, "a" * 500)
        self.phone.observe()
        self.phone.tap(x=10, y=10)
        typed = self.client.elements[0]["value"]
        error = self.assert_code("input_continuation_expired", lambda: self.phone.type_text(continue_token=first["continue_token"]))
        self.assertFalse(error.details["action_executed"])
        self.assertFalse(error.details["recovery"]["replay_action"])
        self.assertEqual(self.client.elements[0]["value"], typed)

    def test_reads_do_not_invalidate_and_unknown_tokens_type_nothing(self):
        self.phone.call_budget = 0
        first = self.phone.type_text({"label": "Target"}, "a" * 300)
        self.phone.observe()
        self.phone.find({"label": "Target"})
        self.assert_code("input_continuation_expired", lambda: self.phone.type_text(continue_token="unknown"))
        self.assertEqual(len(self.client.elements[0]["value"]), 200)
        self.assert_code("input_continuation_expired", lambda: self.phone.type_text(continue_token=first["continue_token"]))

    def test_continuation_accepts_no_other_argument(self):
        for extra in ({"text": "more"}, {"selector": {"label": "Target"}}, {"submit": True}, {"replace": False},
                      {"verify": True}, {"observe": "tree"}, {"expect": {"label": "Target"}}, {"allow_newlines": True}):
            with self.subTest(extra=extra):
                error = self.assert_code("invalid_argument", lambda: self.phone.type_text(continue_token="token", **extra))
                self.assertFalse(error.details["action_executed"])
        self.assert_code("invalid_argument", lambda: self.phone.type_text(selector={"label": "Target"}))
        self.assertEqual(self.client.calls, [])

    def test_failure_midway_reports_what_was_accepted_and_stops(self):
        original = self.client.session
        sent = []

        def session(method, path, payload=None, timeout=None):
            if path == "/wda/keys":
                sent.append(payload)
                if len(sent) == 2:
                    self.client.calls.append((method, path, payload))
                    raise WDAError("action_uncertain", "Typing response timed out", uncertain=True)
            return original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=session):
            error = self.assert_code("action_uncertain", lambda: self.phone.type_text({"label": "Target"}, "a" * 900, submit=True))
        self.assertEqual((error.details["characters_confirmed"], error.details["characters_total"]), (400, 900))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(len(sent), 2)
        self.assertFalse(any(body == {"value": ["\n"]} for _, _, body in self.client.actions()))
        self.assertIsNone(self.phone.pending_input)

    def test_batch_stops_for_unfinished_input_and_for_its_time_budget(self):
        self.phone.call_budget = 0
        result = self.phone.batch([
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "a" * 300}},
            {"op": "press_button", "args": {"name": "home"}},
        ])
        self.assertEqual((result["stop_reason"], result["stopped_at"], result["completed_steps"]), ("input_continues", 0, 0))
        token = result["results"][0]["continue_token"]
        self.assertNotIn("/wda/homescreen", self.paths())
        resumed = self.phone.batch([
            {"op": "type_text", "args": {"continue_token": token}},
            {"op": "press_button", "args": {"name": "home"}},
        ])
        self.assertEqual((resumed["stop_reason"], resumed["stopped_at"], resumed["completed_steps"]), ("time_budget", 1, 1))
        self.assertFalse(resumed["complete"])
        self.assertEqual(self.client.elements[0]["value"], "a" * 300)
        self.assertNotIn("/wda/homescreen", self.paths())
        self.assertIsNone(self.phone._deadline)

    def test_batch_inside_its_budget_is_unchanged(self):
        result = self.phone.batch([{"op": "press_button", "args": {"name": "home"}}, {"op": "observe", "args": {}}])
        self.assertTrue(result["complete"])
        self.assertNotIn("stop_reason", result)

    def test_runtime_accepts_exactly_one_form_before_any_phone_request(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            self.addCleanup(runtime.close)
            runtime.client = self.client
            self.client.close = lambda: None
            runtime.phone = PhoneController(self.client, directory)
            for arguments in ({"selector": {"label": "Target"}}, {"observe": "tree"},
                              {"continue_token": "t", "text": "both"}, {"continue_token": ""}):
                with self.subTest(arguments=arguments):
                    self.assert_code("invalid_argument", lambda: runtime.call("pua_type_text", arguments))
            self.assertEqual(self.client.calls, [])
            self.assert_code("input_continuation_expired", lambda: runtime.call("pua_type_text", {"continue_token": "stale"}))


class CoordinateFallbackTests(PhoneCase):
    """A selector that leads to no action is not retried: the error equips a coordinate action."""
    def test_failed_selector_tap_returns_the_screen_and_points_to_coordinates(self):
        for code, arrange in (("no_such_element", lambda: self.client.elements.clear()),
                              ("occluded_target", lambda: self.client.elements[0].update(hittable=False)),
                              ("ambiguous_target", lambda: self.client.elements.append(
                                  {**self.client.elements[0], "id": "second", "rect": {"x": 70, "y": 400, "width": 250, "height": 44}}))):
            with self.subTest(code=code):
                self.setUp()
                arrange()
                error = self.assert_code(code, lambda: self.phone.tap(selector={"label": "Target"}))
                details = error.details
                self.assertFalse(details["action_executed"])
                self.assertEqual((details["recovery"]["use"], details["recovery"]["next_tool"]), ("coordinates", "pua_tap"))
                self.assertIn("Do not try other selector spellings", details["recovery"]["next_step"])
                self.assertEqual(Path(details["observation"]["image"]["path"]).read_bytes(), self.client.screenshot)
                self.assertEqual(self.client.actions(), [])
                # The model sees the screen in the same failed result, with no extra observe round.
                content = result_content({"error": error.as_dict()})["content"]
                self.assertEqual([item["type"] for item in content], ["text", "image"])
                if code == "occluded_target":
                    self.assertNotIn("next_arguments", details["recovery"])
                    self.assertIn("Inspect the attached screenshot FIRST", details["recovery"]["next_step"])
                if code == "ambiguous_target":
                    self.assertEqual([candidate["tap"] for candidate in details["candidates"]], [[195, 222], [195, 422]])

    def test_coordinate_tap_then_reaches_what_the_selector_could_not(self):
        self.client.elements[0]["hittable"] = False
        error = self.assert_code("occluded_target", lambda: self.phone.tap(selector={"label": "Target"}))
        # Only after inspecting the image may the caller choose a visible point.
        # The occluded background element no longer supplies an automatic action.
        self.assertNotIn("next_arguments", error.details["recovery"])
        result = self.phone.tap(x=195, y=222)
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.client.actions(), [("POST", "/wda/tap", {"x": 195, "y": 222})])

    def test_screen_capture_failure_never_hides_the_selector_error(self):
        self.client.elements.clear()
        original = self.client.request

        def request(method, path, payload=None, timeout=None):
            if path == "/screenshot":
                raise WDAError("pua_unreachable", "Screenshot channel disconnected")
            return original(method, path, payload, timeout)

        with patch.object(self.client, "request", side_effect=request):
            error = self.assert_code("no_such_element", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertEqual(error.details["observation_error"]["code"], "pua_unreachable")
        self.assertEqual(error.details["recovery"]["use"], "coordinates")

    def test_failed_selector_typing_points_to_tap_then_focused_input(self):
        self.client.elements.clear()
        error = self.assert_code("no_such_element", lambda: self.phone.type_text({"label": "Search"}, "query"))
        self.assertIn("no selector", error.details["recovery"]["next_step"])
        self.assertIn("image", error.details["observation"])
        self.assertEqual(self.client.actions(), [])

    def test_text_without_selector_goes_to_the_field_focused_by_a_coordinate_tap(self):
        field = self.client.elements[0]
        field["value"] = "old"
        self.client.focused = field
        result = self.phone.type_text(text="坐标聚焦后输入", verify=True)
        self.assertTrue(result["exact_readback"])
        self.assertEqual(field["value"], "坐标聚焦后输入")
        actions = [path for _, path, _ in self.client.actions()]
        # No second tap that could move the caret, and no selector query at all.
        self.assertEqual(actions, ["/element/target/clear", "/wda/keys"])
        self.assertFalse(any(path == "/elements" for path in self.paths()))
        appended = self.phone.type_text(text="++", replace=False)
        self.assertTrue(appended["action_complete"])
        self.assertEqual(field["value"], "坐标聚焦后输入++")

    def test_focused_input_keeps_every_text_safeguard(self):
        error = self.assert_code("no_focused_field", lambda: self.phone.type_text(text="nowhere"))
        self.assertFalse(error.details["action_executed"])
        self.assertEqual(error.details["recovery"]["use"], "coordinates")
        self.assertEqual(self.client.actions(), [])
        field = self.client.elements[0]
        self.client.focused = field
        self.assert_code("newline_unsafe", lambda: self.phone.type_text(text="a\nb", allow_newlines=True))
        for kind, secure in (("XCUIElementTypeSecureTextField", True), ("XCUIElementTypeButton", False)):
            with self.subTest(kind=kind):
                field["kind"] = kind
                error = self.assert_code("not_editable", lambda: self.phone.type_text(text="never typed"))
                self.assertEqual(error.details["secure_field"], secure)
                self.assertEqual(self.client.actions(), [])

    def test_secure_field_is_handed_to_the_user_without_a_screenshot_or_coordinates(self):
        self.client.elements[0]["kind"] = "XCUIElementTypeSecureTextField"
        error = self.assert_code("not_editable", lambda: self.phone.type_text({"label": "Target"}, "never typed"))
        self.assertTrue(error.details["secure_field"])
        self.assertNotIn("observation", error.details)
        self.assertNotIn("recovery", error.details)
        self.assertFalse(any(path == "/screenshot" for path in self.paths()))

    def test_exhausted_selector_search_ends_with_the_screen_for_coordinates(self):
        self.client.elements.clear()
        error = self.assert_code("search_exhausted", lambda: self.phone.scroll_find({"label": "Missing"}, max_swipes=2))
        self.assertTrue(error.details["action_executed"])
        self.assertEqual(error.details["recovery"]["use"], "coordinates")
        self.assertIn("image", error.details["observation"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_batch_stop_carries_the_same_evidence(self):
        self.client.elements.clear()
        result = self.phone.batch([{"op": "tap", "args": {"selector": {"label": "Gone"}}},
                                   {"op": "press_button", "args": {"name": "home"}}])
        self.assertEqual((result["stop_reason"], result["stopped_at"]), ("no_such_element", 0))
        self.assertEqual(result["error"]["recovery"]["use"], "coordinates")
        self.assertEqual([item["type"] for item in result_content(result)["content"]], ["text", "image"])
        self.assertEqual(self.client.actions(), [])


class SourceRootTests(PhoneCase):
    """The page source root names the app and spans the screen; do not ask for both again."""
    def setUp(self):
        super().setUp()
        self.client.source_root = {"name": "Example", "bundleId": "com.example.phone", "width": 390, "height": 844}

    def test_tree_observation_needs_one_request_once_the_viewport_is_known(self):
        first = self.phone.observe()
        self.assertEqual(self.paths(), ["/source?format=xml&excluded_attributes=visible,accessible", "/window/size"])
        self.client.calls.clear()
        second = self.phone.observe()
        self.assertEqual(self.paths(), ["/source?format=xml&excluded_attributes=visible,accessible"])
        self.assertEqual((second["app"], second["viewport"]), ("com.example.phone", first["viewport"]))
        self.client.calls.clear()
        result = self.phone.tap(selector={"label": "Target"}, observe="tree")
        self.assertEqual(result["observation"]["app"], "com.example.phone")
        self.assertFalse(any(path in ("/wda/activeAppInfo", "/window/size") for path in self.paths()))

    def test_rotation_or_a_missing_identity_falls_back_to_the_real_reads(self):
        self.phone.observe()
        self.client.size = {"width": 844, "height": 390}
        self.client.source_root.update(width=844, height=390)
        self.client.calls.clear()
        rotated = self.phone.observe()
        self.assertIn("/window/size", self.paths())
        self.assertEqual((rotated["viewport"]["width"], rotated["viewport"]["height"]), (844, 390))
        for bundle in (None, "", "local.pid.0"):
            with self.subTest(bundle=bundle):
                self.client.source_root.pop("bundleId", None)
                if bundle is not None:
                    self.client.source_root["bundleId"] = bundle
                self.client.calls.clear()
                self.assertEqual(self.phone.observe()["app"], "com.example.phone")
                self.assertIn("/wda/activeAppInfo", self.paths())

    def test_unresolved_foreground_is_still_a_channel_fault(self):
        self.client.source_root["bundleId"] = "local.pid.0"
        self.client.app = "local.pid.0"
        self.assert_code("pua_foreground_unavailable", lambda: self.phone.observe())

    def test_supplied_observation_still_checks_the_real_foreground_and_viewport(self):
        observed = self.phone.observe()
        self.client.app = "com.example.other"
        self.client.calls.clear()
        error = self.assert_code("stale_observation", lambda: self.phone.tap(x=10, y=10, observation_id=observed["observation_id"]))
        self.assertEqual(error.details["reason"], "foreground_changed")

    def test_screenshot_reuses_the_viewport_until_the_image_turns(self):
        self.phone.observe("screenshot")
        self.client.calls.clear()
        self.phone.observe("screenshot")
        self.assertEqual(self.paths(), ["/wda/activeAppInfo", "/screenshot"])
        self.client.size = {"width": 844, "height": 390}
        self.client.screenshot = png(4, 2)
        self.client.calls.clear()
        turned = self.phone.observe("screenshot")
        self.assertIn("/window/size", self.paths())
        self.assertEqual((turned["viewport"]["width"], turned["image"]["pixel_to_point"]), (844, [211.0, 195.0]))

    def test_post_action_screenshot_waits_briefly_only_when_settling_is_on(self):
        with patch("wda_controller.time.sleep") as sleep:
            self.phone.tap(x=10, y=10, observe="screenshot")
            sleep.assert_not_called()
            self.phone.settle_seconds = 0.8
            self.phone.tap(x=10, y=10, observe="screenshot")
            sleep.assert_called_once_with(0.5)
            self.phone.observe("screenshot")
            self.assertEqual(sleep.call_count, 1)


class TextSplittingTests(unittest.TestCase):
    def test_pieces_rejoin_exactly_and_respect_the_size(self):
        text = "你好，世界 hello " * 80
        for size in (1, 7, 200, 5000):
            with self.subTest(size=size):
                pieces = wda_text.split_text(text, size)
                self.assertEqual("".join(pieces), text)
                self.assertTrue(all(0 < len(piece) <= size for piece in pieces))

    def test_graphemes_are_not_separated(self):
        family, flags, accent = "👨\u200d👩\u200d👧", "🇨🇳🇺🇸", "e\u0301"
        text = ("ab" + family + flags + accent + "👍🏽" + "\r\n") * 60
        for size in (9, 10, 11, 50):
            pieces = wda_text.split_text(text, size)
            self.assertEqual("".join(pieces), text)
            joined = ""
            for piece in pieces[:-1]:
                joined += piece
                self.assertTrue(wda_text.safe_boundary(text, len(joined)), (size, piece))

    def test_an_unbreakable_run_is_typed_rather_than_stalling(self):
        text = "\u0301" * 50
        self.assertEqual("".join(wda_text.split_text(text, 8)), text)
        with self.assertRaises(ValueError):
            wda_text.split_text("abc", 0)

    def test_timeout_scales_with_the_piece(self):
        self.assertEqual(wda_text.request_timeout(30, 30), 15)
        self.assertEqual(wda_text.request_timeout(600, 30), 50)
        self.assertGreater(wda_text.request_timeout(200, 30), 200 / 30)


class ViewportCacheTests(PhoneCase):
    def count(self):
        return sum(path == "/window/size" for path in self.paths())

    def test_plain_actions_share_one_viewport_read(self):
        self.phone.tap(x=10, y=10)
        self.phone.tap(x=20, y=20)
        self.phone.swipe()
        self.phone.tap(selector={"label": "Target"})
        self.assertEqual(self.count(), 1)

    def test_a_supplied_observation_and_every_observation_read_it_again(self):
        observed = self.phone.observe()
        self.phone.tap(x=10, y=10)
        self.assertEqual(self.count(), 1)
        self.phone.tap(x=10, y=10, observation_id=observed["observation_id"])
        self.assertEqual(self.count(), 2)
        self.phone.observe()
        self.assertEqual(self.count(), 3)

    def test_app_switches_and_age_drop_the_cached_viewport(self):
        self.phone.tap(x=10, y=10)
        self.phone.launch_app("com.example.other")
        self.phone.tap(x=10, y=10)
        self.assertEqual(self.count(), 2)
        self.phone.press_button("home")
        self.phone.tap(x=10, y=10)
        self.assertEqual(self.count(), 3)
        with patch("wda_controller.VIEWPORT_TTL", 0):
            self.phone.tap(x=10, y=10)
        self.assertEqual(self.count(), 4)
        self.phone.reset()
        self.phone.tap(x=10, y=10)
        self.assertEqual(self.count(), 5)

    def test_rotation_seen_by_a_fresh_read_rejects_old_coordinates(self):
        self.phone.tap(x=380, y=10)
        self.client.size = {"width": 844, "height": 390}
        self.phone.observe()
        self.assert_code("invalid_argument", lambda: self.phone.tap(x=10, y=800))


class SettleTests(PhoneCase):
    def test_post_action_tree_read_waits_for_the_transition_then_restores_speed(self):
        self.phone.settle_seconds = 0.5
        self.phone.tap(selector={"label": "Target"}, observe="tree")
        paths = self.paths()
        source = next(i for i, path in enumerate(paths) if path.startswith("/source"))
        toggles = [i for i, path in enumerate(paths) if path == "/appium/settings"]
        self.assertEqual(len(toggles), 2)
        self.assertLess(toggles[0], source)
        self.assertGreater(toggles[1], source)
        self.assertEqual(self.client.settings, [{"animationCoolOffTimeout": 0.5}, {"animationCoolOffTimeout": 0}])

    def test_other_reads_and_the_default_do_not_toggle_settings(self):
        self.phone.tap(selector={"label": "Target"}, observe="tree")
        self.phone.settle_seconds = 0.5
        self.phone.observe()
        self.phone.tap(x=10, y=10, observe="screenshot")
        self.phone.tap(x=10, y=10)
        self.assertEqual(self.client.settings, [])

    def test_failed_toggle_never_fails_the_action_and_restores_settings_later(self):
        self.phone.settle_seconds = 0.5
        original = self.client.session
        restored = []
        self.client.reapply_settings = lambda: restored.append(True)

        def session(method, path, payload=None, timeout=None):
            if path == "/appium/settings":
                raise WDAError("action_uncertain", "Settings response lost", uncertain=True)
            return original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=session):
            result = self.phone.tap(selector={"label": "Target"}, observe="tree")
        self.assertTrue(result["action_complete"])
        self.assertIn("nodes", result["observation"])
        self.assertEqual(restored, [True])

    def test_speed_is_restored_when_the_read_fails(self):
        self.phone.settle_seconds = 0.5
        with patch.object(self.client, "source", side_effect=WDAError("pua_unreachable", "read lost")):
            error = self.assert_code("pua_unreachable", lambda: self.phone.tap(selector={"label": "Target"}, observe="tree"))
        self.assertTrue(error.details["action_executed"])
        self.assertEqual(self.client.settings[-1], {"animationCoolOffTimeout": 0})


@unittest.skipUnless(os.access(wda_image.SIPS, os.X_OK), "screenshot scaling uses macOS sips")
class ScreenshotTests(PhoneCase):
    def setUp(self):
        super().setUp()
        self.client.size = {"width": 440, "height": 956}
        self.client.screenshot = png(1320, 2868)

    def test_model_receives_a_bounded_jpeg_with_its_exact_point_scale(self):
        observed = self.phone.observe("screenshot")
        image = observed["image"]
        self.assertEqual((image["mimeType"], image["width"], image["height"]), ("image/jpeg", 722, 1568))
        self.assertEqual(image["pixel_to_point"], [round(440 / 722, 4), round(956 / 1568, 4)])
        path = Path(image["path"])
        self.assertEqual(path.suffix, ".jpg")
        self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # The native capture stays next to it as evidence.
        self.assertEqual(path.with_suffix(".png").read_bytes(), self.client.screenshot)
        result = result_content(observed)
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/jpeg")
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), path.read_bytes())
        self.assertNotIn("structuredContent", result)

    def test_original_png_is_used_on_request_or_when_scaling_is_unavailable(self):
        with patch.dict(os.environ, {"WDA_IMAGE": "original"}):
            image = self.phone.observe("screenshot")["image"]
        self.assertEqual((image["mimeType"], image["width"], image["height"]), ("image/png", 1320, 2868))
        self.assertEqual(image["pixel_to_point"], [round(440 / 1320, 4), round(956 / 2868, 4)])
        with patch.object(wda_image, "SIPS", "/nonexistent/sips"):
            image = self.phone.observe("both")["image"]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(Path(image["path"]).read_bytes(), self.client.screenshot)

    def test_a_failed_conversion_keeps_the_capture(self):
        with patch.object(wda_image.subprocess, "run", side_effect=OSError("cannot start")):
            image = self.phone.observe("screenshot")["image"]
        self.assertEqual(image["mimeType"], "image/png")
        self.assertFalse(list((Path(self.directory.name) / "artifacts").glob("*.jpg")))

    def test_both_kinds_of_artifact_are_pruned(self):
        artifacts = Path(self.directory.name) / "artifacts"
        artifacts.mkdir(mode=0o700)
        for number in range(104):
            for suffix in (".png", ".jpg"):
                (artifacts / f"00000000T000000{number:06d}Z-old{suffix}").write_bytes(b"old")
        self.phone.observe("screenshot")
        self.assertEqual((len(list(artifacts.glob("*.png"))), len(list(artifacts.glob("*.jpg")))), (100, 100))

    def test_fit_keeps_small_images_and_bounds_both_sides(self):
        self.assertEqual(wda_image.fit_size(1320, 2868), (722, 1568))
        self.assertEqual(wda_image.fit_size(2868, 1320), (1568, 722))
        self.assertEqual(wda_image.fit_size(1536, 2048), (768, 1024))
        self.assertEqual(wda_image.fit_size(600, 900), (600, 900))
        self.assertIsNone(wda_image.png_size(b"not a png"))
        self.assertEqual(wda_image.png_size(png(3, 5)), (3, 5))


class ResultContentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(self.directory.name)
        self.addCleanup(self.runtime.close)
        self.client = FakeWDA()
        self.client.close = lambda: None
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, self.directory.name)

    def test_model_results_are_one_compact_text_block_without_a_structured_copy(self):
        for name, arguments in (("pua_observe", {}), ("pua_tap", {"x": 10, "y": 10}), ("wda_missing", {}),
                                ("pua_tap", {"selector": {"label": "Absent"}})):
            with self.subTest(name=name, arguments=arguments):
                result = tool_result(self.runtime, {"name": name, "arguments": arguments})
                self.assertEqual(set(result), {"content", "isError"})
                text = result["content"][0]["text"]
                self.assertEqual(json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":")), text)

    def test_screenshot_is_delivered_as_an_image_block_the_host_cannot_drop(self):
        result = tool_result(self.runtime, {"name": "pua_tap", "arguments": {"x": 10, "y": 10, "observe": "screenshot"}})
        self.assertNotIn("structuredContent", result)
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), self.client.screenshot)
        self.assertNotIn(result["content"][1]["data"][:40], result["content"][0]["text"])

    def test_preview_tools_keep_the_structured_object_the_widget_reads(self):
        opened = tool_result(self.runtime, {"name": "pua_screen", "arguments": {}})
        self.assertEqual(json.loads(opened["content"][0]["text"]), opened["structuredContent"])
        frame = tool_result(self.runtime, {"name": "pua_screen_frame", "arguments": {}})
        self.assertEqual(frame["content"], [])
        self.assertIn("frame_available", frame["structuredContent"])
        self.runtime.screen.close()


class ServeTests(unittest.TestCase):
    def run_server(self, runtime, lines):
        output = io.StringIO()
        with patch.object(sys, "stdin", lines), contextlib.redirect_stdout(output):
            serve(runtime)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    @staticmethod
    def call(ident, name, arguments=None):
        return json.dumps({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}}) + "\n"

    def test_preview_polls_and_pings_are_answered_while_a_phone_tool_runs(self):
        release, started, output = threading.Event(), threading.Event(), io.StringIO()

        class SlowRuntime:
            def call(self, name, arguments):
                if name == "pua_tap":
                    started.set()
                    if not release.wait(10):
                        raise AssertionError("the reader never served the preview poll")
                    return {"action_executed": True}
                return {"frame": None, "frame_available": False}

            def note_response(self, *arguments):
                pass

            def replied(self):
                pass

        def lines():
            yield self.call("tap", "pua_tap")
            self.assertTrue(started.wait(10))
            yield self.call("frame", "pua_screen_frame")
            yield json.dumps({"jsonrpc": "2.0", "id": "ping", "method": "ping"}) + "\n"
            deadline = time.monotonic() + 10
            while output.getvalue().count("\n") < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            # Both replies were written while the phone tool was still blocked.
            self.assertEqual(output.getvalue().count("\n"), 2)
            release.set()

        with patch.object(sys, "stdin", lines()), contextlib.redirect_stdout(output):
            serve(SlowRuntime())
        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([response["id"] for response in responses], ["frame", "ping", "tap"])
        self.assertEqual(responses[0]["result"]["content"], [])

    def test_phone_tools_keep_their_order_and_finish_before_shutdown(self):
        seen = []

        class OrderedRuntime:
            def call(self, name, arguments):
                time.sleep(0.02 if arguments["n"] == 0 else 0)
                seen.append(arguments["n"])
                return {"n": arguments["n"]}

            def note_response(self, *arguments):
                pass

            def replied(self):
                pass

        responses = self.run_server(OrderedRuntime(), iter(self.call(n, "pua_tap", {"n": n}) for n in range(5)))
        self.assertEqual(seen, [0, 1, 2, 3, 4])
        self.assertEqual([response["id"] for response in responses], [0, 1, 2, 3, 4])

    def test_metrics_report_response_bytes_rounds_and_waits_then_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(directory)
            self.addCleanup(runtime.close)
            client = FakeWDA()
            client.close = lambda: None
            runtime.client = client
            runtime.phone = PhoneController(client, directory)
            responses = self.run_server(runtime, iter([
                self.call(1, "pua_observe"), self.call(2, "pua_tap", {"x": 10, "y": 10, "observe": "screenshot"}),
                self.call(3, "pua_screen_frame"), self.call(4, "pua_metrics", {"reset": True}), self.call(5, "pua_metrics")]))
            by_id = {response["id"]: response["result"] for response in responses}
            metrics = json.loads(by_id[4]["content"][0]["text"])
            self.assertEqual(metrics["responses"]["count"], 2)
            observe = metrics["responses"]["by_tool"]["pua_observe"]
            self.assertEqual(observe["text_bytes"], len(by_id[1]["content"][0]["text"].encode()))
            self.assertEqual(observe["image_bytes"], 0)
            self.assertEqual(metrics["responses"]["by_tool"]["pua_tap"]["image_bytes"], len(by_id[2]["content"][1]["data"]))
            self.assertNotIn("pua_screen_frame", metrics["responses"]["by_tool"])
            self.assertEqual((metrics["rounds"]["count"], metrics["rounds"]["waits"]), (2, 1))
            self.assertGreaterEqual(metrics["rounds"]["max_wait_seconds"], 0)
            self.assertEqual(metrics["tools"]["by_tool"]["pua_tap"]["count"], 1)
            self.assertEqual(metrics["tools"]["by_tool"]["pua_tap"]["errors"], 0)
            # Totals only: no labels, text, app identifiers or image data.
            self.assertNotIn("Row 1", by_id[4]["content"][0]["text"])
            self.assertNotIn("com.example", by_id[4]["content"][0]["text"])
            after = json.loads(by_id[5]["content"][0]["text"])
            self.assertEqual(set(after["responses"]["by_tool"]), {"pua_metrics"})
            self.assertEqual(after["tools"]["count"], 1)
            self.assertIsNone(after["rounds"]["median_wait_seconds"])


if __name__ == "__main__":
    unittest.main()
