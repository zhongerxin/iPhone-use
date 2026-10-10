import base64
import copy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from wda_client import WDAError
from wda_controller import ELEMENT_KEY, PhoneController, predicate


def node(label="Row 1", y=200, kind="Cell", **extra):
    return {"type": "XCUIElementType" + kind, "label": label, "name": label,
            "x": 60, "y": y, "width": 250, "height": 44, **extra}


class FakeWDA:
    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.session_id = None
        self.locked = False
        self.status_ready = True
        self.app = "com.example.phone"
        self.size = {"width": 390, "height": 844}
        self.nodes = [node()]
        self.source_pages = None
        self.swipe_count = 0
        self.elements = [{"id": "target", "label": "Target", "kind": "XCUIElementTypeTextField",
                          "rect": {"x": 70, "y": 200, "width": 250, "height": 44},
                          "hittable": True, "value": ""}]
        self.input_override = None
        self.click_error = None
        self.home_effective = True
        self.home_error = None
        self.activate_effective = True
        self.activate_error = None
        self.foreground_sequence = None
        self.gesture_effects = None
        self.focused = None
        self.rich_elements = False
        self.source_root = None
        self.settings = []
        self.screenshot = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXuoAAAAASUVORK5CYII=")

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/wda/homescreen":
            if self.home_error:
                raise self.home_error
            if self.home_effective:
                self.app = "com.apple.springboard"
            return {"value": None}
        if path == "/wda/activeAppInfo":
            if self.foreground_sequence:
                self.app = self.foreground_sequence.pop(0)
            return {"value": {"bundleId": self.app}}
        if path == "/screenshot":
            return {"value": base64.b64encode(self.screenshot).decode("ascii")}
        if path == "/wda/locked":
            return {"value": self.locked}
        if path == "/status":
            return {"value": {"ready": self.status_ready}}
        raise AssertionError("Unexpected request: " + path)

    def ensure_session(self):
        if self.session_id is None:
            self.calls.append(("POST", "/session", None))
            self.session_id = "fake-session"
        return self.session_id

    def source(self):
        nodes = self.nodes if self.source_pages is None else self.source_pages[min(self.swipe_count, len(self.source_pages)-1)]
        # A real WDA source root is the application itself, with its bundle ID and screen size.
        root = ET.Element("AppiumAUT") if self.source_root is None else ET.Element(
            "XCUIElementTypeApplication", {"type": "XCUIElementTypeApplication", "x": "0", "y": "0",
                                           **{k: str(v) for k, v in self.source_root.items()}})
        for item in nodes:
            ET.SubElement(root, item["type"], {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in item.items()})
        return ET.tostring(root, encoding="unicode")

    def session(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        self.timeouts.append((method, path, timeout))
        if path.startswith("/source"):
            return self.source()
        if path == "/window/size":
            return self.size.copy()
        if path == "/element/active":
            if self.focused is None:
                raise WDAError("no such element", "No element has keyboard focus")
            answer = {ELEMENT_KEY: self.focused["id"]}
            return {**answer, "type": self.focused["kind"]} if self.rich_elements else answer
        if path == "/elements":
            value = payload["value"]
            labels = re.findall(r"label == '([^']*)'", value)
            matches = [item for item in self.elements if not labels or item["label"] == labels[0]]
            enabled = re.search(r"enabled == (true|false)", value)
            if enabled:
                matches = [item for item in matches if item.get("enabled", True) is (enabled[1] == "true")]
            if "CONTAINS" in value:
                fragment = re.search(r"label CONTAINS '([^']*)'", value)[1]
                matches = [item for item in self.elements if fragment in item["label"]]
            if not self.rich_elements:
                return [{ELEMENT_KEY: item["id"]} for item in matches]
            # The pinned WDA answers like this once compact responses are switched off.
            return [{ELEMENT_KEY: item["id"], "ELEMENT": item["id"], "type": item["kind"], "label": item["label"],
                     "rect": item["rect"].copy(), "enabled": item.get("enabled", True),
                     "attribute/name": item.get("name", item["label"]), "attribute/value": item["value"] or None}
                    for item in matches]
        if path.startswith("/element/"):
            parts = path.split("/")
            element = next(item for item in self.elements if item["id"] == parts[2])
            suffix = "/".join(parts[3:])
            if suffix == "rect":
                return element["rect"].copy()
            if suffix == "attribute/hittable":
                return element["hittable"]
            if suffix == "attribute/type":
                return element["kind"]
            if suffix == "attribute/value":
                return element["value"]
            if suffix == "click":
                if self.click_error:
                    raise self.click_error
                self.focused = element
                return None
            if suffix == "clear":
                element["value"] = ""
                return None
            if suffix == "value":
                self.focused = element
                element["value"] = self.input_override if self.input_override is not None else element["value"] + payload["text"]
                return None
        if path in ("/wda/dragfromtoforduration", "/wda/swipe"):
            self.swipe_count += 1
            if self.gesture_effects and self.swipe_count <= len(self.gesture_effects):
                effect = self.gesture_effects[self.swipe_count - 1]
                if effect:
                    effect()
            return None
        if path == "/wda/apps/activate":
            if self.activate_error:
                raise self.activate_error
            if self.activate_effective:
                self.app = payload["bundleId"]
            return None
        if path == "/wda/keys":
            text = "".join(payload["value"])
            if self.focused is not None and text != "\n" and self.input_override is None:
                self.focused["value"] += text
            return None
        if path in ("/wda/tap", "/wda/pressButton", "/actions"):
            return None
        if path == "/appium/settings":
            self.settings.append(payload["settings"])
            return {}
        raise AssertionError("Unexpected session request: " + path)

    def actions(self):
        return [(method, path, body) for method, path, body in self.calls
                if method == "POST" and path not in ("/elements", "/appium/settings")]

    def reapply_settings(self):
        pass

    def metrics(self):
        return {"retained_requests": len(self.calls)}

    def clear_metrics(self):
        pass


class ControllerTests(unittest.TestCase):
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

    def test_coordinate_tap_accepts_optional_observation_without_age_limit(self):
        result = self.phone.tap(x=100, y=220)
        self.assertTrue(result["action_executed"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" or path == "/wda/activeAppInfo"
                             for _, path, _ in self.client.calls))
        observed = self.phone.observe()
        self.phone.snapshots[observed["observation_id"]]["time"] -= 3600
        self.client.calls.clear()
        result = self.phone.tap(x=100, y=220, observation_id=observed["observation_id"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.actions()[-1][1], "/wda/tap")

    def test_changed_app_or_orientation_rejects_supplied_coordinate_context(self):
        for change in ("app", "orientation"):
            with self.subTest(change=change):
                self.setUp()
                observed = self.phone.observe()
                if change == "app":
                    self.client.app = "com.example.other"
                else:
                    self.client.size = {"width": 844, "height": 390}
                self.client.calls.clear()
                self.assert_code("stale_observation", lambda: self.phone.tap(x=100, y=220, observation_id=observed["observation_id"], observe="none"))
                self.assertEqual(self.client.actions(), [])

    def test_observation_from_another_runtime_does_not_authorize_coordinates(self):
        observed = self.phone.observe()
        other = PhoneController(self.client, self.directory.name)
        self.client.calls.clear()
        error = self.assert_code("stale_observation", lambda: other.tap(x=100, y=220, observation_id=observed["observation_id"]))
        self.assertEqual(error.details["reason"], "unknown_observation")
        self.assertEqual(self.client.calls, [])

    def test_screenshot_observation_skips_source_and_stores_private_artifact(self):
        result = self.phone.observe("screenshot")
        self.assertNotIn("nodes", result)
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))
        path = Path(result["image"]["path"])
        self.assertEqual(path.parent, Path(self.directory.name) / "artifacts")
        self.assertEqual(path.read_bytes(), self.client.screenshot)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_screenshot_coordinate_context_tolerates_changing_pixels(self):
        observed = self.phone.observe("screenshot")
        self.phone.tap(x=100, y=220, observation_id=observed["observation_id"], observe="none")
        self.assertEqual(self.client.actions()[-1][1], "/wda/tap")
        self.client.screenshot += b"changed picture"
        self.client.calls.clear()
        result = self.phone.tap(x=100, y=220, observation_id=observed["observation_id"])
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.client.actions()[-1][1], "/wda/tap")
        self.assertFalse(any(path == "/screenshot" for _, path, _ in self.client.calls))
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))

    def test_coordinate_context_tolerates_changing_tree_and_image(self):
        observed = self.phone.observe("both")
        self.client.nodes = [node("New content on same page")]
        self.client.screenshot += b"custom-rendered content changed"
        self.client.calls.clear()
        self.phone.tap(x=100, y=220, observation_id=observed["observation_id"])
        self.assertEqual(self.client.actions()[-1][1], "/wda/tap")
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in self.client.calls))

    def test_custom_scroll_ignores_carousel_changes_outside_region(self):
        area = {"x": 30, "y": 300, "width": 300, "height": 400}
        for mode in ("tree", "both"):
            with self.subTest(mode=mode):
                self.setUp()
                self.client.nodes = [node("Banner A", y=100), node("Row 1", y=400)]
                observed = self.phone.observe(mode)
                self.client.source_pages = [
                    [node("Banner B", y=100), node("Row 1", y=400)],
                    [node("Banner C", y=100), node("Row 1", y=350), node("Row 2", y=400)],
                ]
                self.client.screenshot += b"changed carousel outside gesture area"
                self.client.calls.clear()
                result = self.phone.swipe(region=area, observation_id=observed["observation_id"], verify=True)
                self.assertTrue(result["verified"])
                self.assertEqual(self.client.swipe_count, 1)
                self.assertEqual(self.client.actions()[0][1], "/wda/dragfromtoforduration")
                self.assertFalse(any(path == "/screenshot" for _, path, _ in self.client.calls))

    def test_default_custom_scroll_tolerates_target_region_content_refresh(self):
        area = {"x": 30, "y": 300, "width": 300, "height": 400}
        self.client.nodes = [node("Banner A", y=100), node("Row 1", y=400)]
        observed = self.phone.observe()
        self.client.nodes = [node("Banner A", y=100), node("Different list content", y=400)]
        self.client.calls.clear()
        result = self.phone.swipe(region=area, observation_id=observed["observation_id"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in self.client.calls))

    def test_default_custom_scroll_needs_no_tree_or_observation_id(self):
        area = {"x": 30, "y": 300, "width": 300, "height": 400}
        result = self.phone.swipe(region=area)
        self.assertTrue(result["action_executed"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path in ("/screenshot", "/wda/activeAppInfo")
                             for _, path, _ in self.client.calls))

    def test_default_swipe_observe_tree_reads_once_for_next_decision(self):
        self.client.source_pages = [[node("Before")], [node("Next page")]]
        result = self.phone.swipe(observe="tree")
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["observation"]["nodes"][0]["label"], "Next page")
        self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 1)
        self.assertEqual(self.client.swipe_count, 1)

    def test_default_scroll_does_not_add_modal_verification_reads(self):
        self.client.nodes = [node("Row 1"), node("Blocking alert", y=100, kind="Alert")]
        result = self.phone.swipe()
        self.assertTrue(result["action_complete"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))

    def test_supplied_scroll_context_from_another_runtime_is_rejected(self):
        observed = self.phone.observe("both")
        other = PhoneController(self.client, self.directory.name)
        self.client.calls.clear()
        error = self.assert_code("stale_observation", lambda: other.swipe(observation_id=observed["observation_id"]))
        self.assertEqual(error.details["reason"], "unknown_observation")
        self.assertEqual(self.client.calls, [])

    def test_custom_scroll_guard_keeps_anchors_beyond_response_truncation(self):
        area = {"x": 30, "y": 300, "width": 300, "height": 400}
        self.client.nodes = [node("Banner", y=100), node("Row 1", y=400)]
        observed = self.phone.observe(max_nodes=1)
        self.assertTrue(observed["truncated"])
        self.client.source_pages = [self.client.nodes, [node("Banner", y=100), node("Row 1", y=350), node("Row 2", y=400)]]
        result = self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"])
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_custom_scroll_rejects_changed_app_or_orientation_context(self):
        area = {"x": 30, "y": 100, "width": 300, "height": 250}
        for change in ("app", "orientation"):
            with self.subTest(change=change):
                self.setUp()
                observed = self.phone.observe()
                if change == "app":
                    self.client.app = "com.example.other"
                else:
                    self.client.size = {"width": 844, "height": 390}
                self.client.calls.clear()
                self.assert_code("stale_observation", lambda: self.phone.swipe(region=area, observation_id=observed["observation_id"]))
                self.assertEqual(self.client.actions(), [])

    def test_custom_scroll_accepts_screenshot_context_or_empty_tree(self):
        area = {"x": 30, "y": 300, "width": 300, "height": 400}
        for mode in ("screenshot", "tree"):
            with self.subTest(mode=mode):
                self.setUp()
                self.client.nodes = []
                observed = self.phone.observe(mode)
                self.phone.snapshots[observed["observation_id"]]["time"] -= 3600
                self.client.calls.clear()
                result = self.phone.swipe(region=area, observation_id=observed["observation_id"])
                self.assertTrue(result["action_executed"])
                self.assertEqual(self.client.swipe_count, 1)
                self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in self.client.calls))

    def test_verified_scroll_requires_explicit_region_when_native_modal_is_present(self):
        for kind in ("Alert", "Sheet"):
            with self.subTest(kind=kind):
                self.setUp()
                self.client.nodes = [node("Row 1"), node("", kind=kind, x=40, y=100, width=310, height=600)]
                error = self.assert_code("modal_requires_region", lambda: self.phone.swipe(verify=True))
                self.assertFalse(error.details["action_executed"])
                self.assertEqual(self.client.actions(), [])
                self.assertEqual(self.client.swipe_count, 0)

    def test_fresh_explicit_region_inside_native_modal_can_scroll(self):
        area = {"x": 70, "y": 250, "width": 200, "height": 250}
        for kind in ("Alert", "Sheet"):
            with self.subTest(kind=kind):
                self.setUp()
                modal = node("", kind=kind, x=40, y=100, width=310, height=600)
                self.client.nodes = [modal, node("Row 1", y=300)]
                observed = self.phone.observe("both")
                self.client.source_pages = [self.client.nodes, [modal, node("Row 1", y=270), node("Row 2", y=300)]]
                self.client.calls.clear()
                result = self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"], observe="tree")
                self.assertTrue(result["verified"])
                self.assertTrue(result["changed"])
                self.assertEqual(result["attempts"], 1)
                self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_explicit_region_outside_or_crossing_native_modal_is_blocked(self):
        for area in ({"x": 70, "y": 200, "width": 200, "height": 150},
                     {"x": 70, "y": 400, "width": 200, "height": 200}):
            with self.subTest(area=area):
                self.setUp()
                self.client.nodes = [node("Row 1", y=area["y"] + 25),
                                     node("", kind="Sheet", x=40, y=500, width=310, height=200)]
                observed = self.phone.observe("tree")
                self.client.calls.clear()
                error = self.assert_code("blocked_scroll_region", lambda: self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"]))
                self.assertFalse(error.details["action_executed"])
                self.assertEqual(self.client.actions(), [])

    def test_region_inside_sheet_but_crossing_coexisting_alert_is_blocked(self):
        area = {"x": 70, "y": 250, "width": 200, "height": 250}
        sheet = node("", kind="Sheet", x=40, y=100, width=310, height=600)
        alert = node("", kind="Alert", x=90, y=300, width=170, height=100)
        self.client.nodes = [sheet, alert, node("Synthetic row", y=350)]
        observed = self.phone.observe("tree")
        self.client.calls.clear()
        error = self.assert_code("blocked_scroll_region", lambda: self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"]))
        self.assertFalse(error.details["action_executed"])
        self.assertEqual(len(error.details["native_modals"]), 2)
        self.assertEqual(self.client.actions(), [])

    def test_fresh_region_inside_all_coexisting_native_modals_can_scroll(self):
        area = {"x": 100, "y": 300, "width": 150, "height": 100}
        sheet = node("", kind="Sheet", x=40, y=100, width=310, height=600)
        alert = node("", kind="Alert", x=90, y=250, width=170, height=200)
        self.client.nodes = [sheet, alert, node("Synthetic row 1", x=100, y=325, width=150)]
        observed = self.phone.observe("tree")
        self.client.source_pages = [self.client.nodes, [sheet, alert, node("Synthetic row 1", x=100, y=315, width=150), node("Synthetic row 2", x=100, y=350, width=150)]]
        self.client.calls.clear()
        result = self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"], observe="none")
        self.assertTrue(result["verified"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_verified_region_inside_native_modal_needs_no_prior_observation(self):
        area = {"x": 70, "y": 250, "width": 200, "height": 250}
        modal = node("", kind="Sheet", x=40, y=100, width=310, height=600)
        self.client.source_pages = [[modal, node("First row", y=350)], [modal, node("First row", y=300)]]
        result = self.phone.swipe(region=area, verify=True)
        self.assertTrue(result["progress_verified"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_modal_appearing_after_gesture_is_not_counted_as_scroll_progress(self):
        for placement in ("inside", "outside"):
            for mode in ("none", "tree", "screenshot", "both"):
                with self.subTest(placement=placement, mode=mode):
                    self.setUp()
                    modal = node("", kind="Alert", x=80,
                                 y=300 if placement == "inside" else 60, width=230, height=80)
                    self.client.source_pages = [[node("Row 1")], [node("Row 2"), modal]]
                    error = self.assert_code("scroll_context_changed", lambda: self.phone.swipe(verify=True, observe=mode))
                    self.assertTrue(error.details["action_executed"])
                    self.assertFalse(error.details["verified"])
                    self.assertFalse(error.details["changed"])
                    self.assertFalse(error.details["action_complete"])
                    self.assertEqual(error.details["attempts"], 1)
                    self.assertEqual(error.details["reasons"], ["modal_changed"])
                    self.assertFalse(error.details["recovery"]["same_gesture_retry"])
                    self.assertFalse(error.details["recovery"]["replay_action"])
                    self.assertEqual(self.client.swipe_count, 1)
                    self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])
                    observed = error.details["observation"]
                    self.assertIn(observed["observation_id"], self.phone.snapshots)
                    self.assertIn("image", observed)
                    self.assertEqual("nodes" in observed, mode in ("tree", "both"))
                    self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)
                    if "nodes" in observed:
                        self.assertTrue(any(n["type"] == "Alert" for n in observed["nodes"]))

    def test_modal_disappearance_or_geometry_change_stops_intentional_modal_scroll(self):
        area = {"x": 70, "y": 250, "width": 200, "height": 250}
        modal = node("", kind="Sheet", x=40, y=100, width=310, height=600)
        for change in ("removed", "moved"):
            with self.subTest(change=change):
                self.setUp()
                self.client.nodes = [modal, node("Row 1", y=300)]
                observed = self.phone.observe("tree")
                current = [node("Row 2", y=300)]
                if change == "moved":
                    current.append({**modal, "y": 120})
                self.client.source_pages = [self.client.nodes, current]
                self.client.calls.clear()
                error = self.assert_code("scroll_context_changed", lambda: self.phone.swipe(verify=True, region=area, observation_id=observed["observation_id"]))
                self.assertEqual(error.details["attempts"], 1)
                self.assertFalse(error.details["verified"])
                self.assertEqual(error.details["reasons"], ["modal_changed"])
                self.assertEqual(self.client.swipe_count, 1)
                self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_orientation_change_after_gesture_stops_before_progress_or_fallback(self):
        self.client.source_pages = [[node("Row 1", y=350)], [node("Row 1", y=300), node("Row 2", y=350)]]
        self.client.gesture_effects = [lambda: setattr(self.client, "size", {"width": 844, "height": 390})]
        error = self.assert_code("scroll_context_changed", lambda: self.phone.swipe(verify=True, observe="tree"))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["verified"])
        self.assertFalse(error.details["changed"])
        self.assertEqual(error.details["attempts"], 1)
        self.assertEqual(error.details["reasons"], ["viewport_changed"])
        self.assertEqual(error.details["observation"]["viewport"], {**self.client.size, "units": "iPhone points"})
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_no_progress_stops_before_unseen_fallback_can_open_a_modal(self):
        self.client.source_pages = [[node("Row 1")], [node("Row 1")],
                                    [node("Row 1"), node("", kind="Alert", y=80)]]
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, max_attempts=2, observe="none"))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["verified"])
        self.assertEqual(error.details["attempts"], 1)
        self.assertIn("image", error.details["observation"])
        self.assertTrue(error.details["recovery"]["visual_check_required"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_default_swipe_does_not_probe_modal_after_accepted_gesture(self):
        self.client.source_pages = [[node("Row 1")], [node("Row 1"), node("", kind="Alert", y=80)]]
        result = self.phone.swipe()
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))

    def test_modal_and_orientation_changes_are_both_reported_after_one_gesture(self):
        self.client.source_pages = [[node("Row 1")], [node("Row 2"), node("", kind="Sheet", y=80)]]
        self.client.gesture_effects = [lambda: setattr(self.client, "size", {"width": 844, "height": 390})]
        error = self.assert_code("scroll_context_changed", lambda: self.phone.swipe(verify=True, observe="none"))
        self.assertEqual(set(error.details["reasons"]), {"viewport_changed", "modal_changed"})
        self.assertFalse(error.details["changed"])
        self.assertEqual(error.details["attempts"], 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_arbitrary_other_nodes_are_not_treated_as_native_modals(self):
        overlay = node("Custom overlay", kind="Other", x=40, y=100, width=310, height=600)
        self.client.source_pages = [[node("Row 1", y=350), overlay], [node("Row 1", y=300), node("Row 2", y=350), overlay]]
        result = self.phone.swipe(verify=True, observe="none")
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_home_navigation_requires_foreground_evidence(self):
        result = self.phone.press_button("home", verify=True, observe="none")
        self.assertTrue(result["action_executed"])
        self.assertTrue(result["verified"])
        self.assertTrue(result["foreground_verified"])
        self.assertEqual(self.client.app, "com.apple.springboard")
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/homescreen"])
        self.assertTrue(any(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls))

    def test_default_home_accepts_navigation_without_a_foreground_probe(self):
        self.client.home_effective = False
        result = self.phone.press_button("home")
        self.assertTrue(result["action_complete"])
        self.assertFalse(result["foreground_verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.calls, [("POST", "/wda/homescreen", {})])

    def test_default_launch_accepts_navigation_without_polling_transition(self):
        self.client.activate_effective = False
        result = self.phone.launch_app("com.example.requested")
        self.assertTrue(result["action_complete"])
        self.assertFalse(result["foreground_verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.calls, [("POST", "/wda/apps/activate", {"bundleId": "com.example.requested"})])

    def test_launch_observation_reads_current_page_without_foreground_poll_loop(self):
        result = self.phone.launch_app("com.example.requested", observe="tree")
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["observation"]["app"], "com.example.requested")
        self.assertEqual(sum(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls), 1)
        self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 1)

    def test_home_http_success_without_foreground_change_is_failure(self):
        self.client.home_effective = False
        self.assert_code("postcondition_failed", lambda: self.phone.press_button("home", verify=True, observe="none"))
        self.assertEqual(self.client.app, "com.example.phone")
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/homescreen"])

    def test_uncertain_home_navigation_is_not_replayed(self):
        self.client.home_error = WDAError("action_uncertain", "Timeout after sending Home", uncertain=True)
        error = self.assert_code("action_uncertain", lambda: self.phone.press_button("home", observe="none"))
        self.assertTrue(error.uncertain)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/homescreen"])
        self.assertFalse(any(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls))

    def test_home_post_action_read_failure_retains_execution_evidence(self):
        with patch.object(self.phone, "active_app", side_effect=WDAError("pua_unreachable", "read timed out")):
            error = self.assert_code("pua_unreachable", lambda: self.phone.press_button("home", verify=True, observe="none"))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["home_foreground_verified"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/homescreen"])

    def test_volume_button_transport_success_remains_unverified(self):
        for name in ("volumeup", "volumedown"):
            with self.subTest(name=name):
                self.setUp()
                result = self.phone.press_button(name, observe="none")
                self.assertTrue(result["action_executed"])
                self.assertFalse(result["verified"])
                self.assertEqual(self.client.actions(), [("POST", "/wda/pressButton", {"name": name})])

    def test_launch_waits_for_foreground_transition_without_reactivating(self):
        requested = "com.example.requested"
        self.client.foreground_sequence = ["com.example.previous", "com.example.previous", requested]
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch("wda_controller.time.monotonic", side_effect=lambda: clock[0]), \
                patch("wda_controller.time.sleep", side_effect=sleep) as sleeper:
            result = self.phone.launch_app(requested, verify=True, observe="none")
        self.assertTrue(result["foreground_verified"])
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.client.app, requested)
        self.assertGreater(sleeper.call_count, 0)
        self.assertLessEqual(clock[0] - 100, 5)
        self.assertEqual(self.client.actions(), [("POST", "/wda/apps/activate", {"bundleId": requested})])
        self.assertEqual(sum(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls), 3)

    def test_launch_foreground_mismatch_has_bounded_reads_and_execution_evidence(self):
        requested = "com.example.requested"
        self.client.activate_effective = False
        clock = [100.0]
        read_times = []
        original_request = self.client.request

        def sleep(seconds):
            clock[0] += seconds

        def request(method, path, payload=None, timeout=None):
            if path == "/wda/activeAppInfo":
                read_times.append(clock[0])
            return original_request(method, path, payload, timeout)

        with patch("wda_controller.time.monotonic", side_effect=lambda: clock[0]), \
                patch("wda_controller.time.sleep", side_effect=sleep), \
                patch.object(self.client, "request", side_effect=request):
            error = self.assert_code("postcondition_failed", lambda: self.phone.launch_app(requested, verify=True, observe="none"))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(error.details["foreground_app"], "com.example.phone")
        self.assertEqual(error.details["requested_app"], requested)
        self.assertFalse(error.details["recovery"]["replay_action"])
        self.assertAlmostEqual(clock[0] - 100, 5)
        reads = sum(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls)
        self.assertGreater(reads, 1)
        self.assertLessEqual(reads, 60)
        self.assertTrue(all(now < 105 for now in read_times))
        self.assertEqual(self.client.actions(), [("POST", "/wda/apps/activate", {"bundleId": requested})])

    def test_uncertain_launch_is_not_replayed_or_polled(self):
        self.client.activate_error = WDAError("action_uncertain", "Activation response timed out", uncertain=True)
        error = self.assert_code("action_uncertain", lambda: self.phone.launch_app("com.example.requested", verify=True, observe="none"))
        self.assertTrue(error.uncertain)
        self.assertEqual(self.phone.accepted_actions, 0)
        self.assertFalse(any(path == "/wda/activeAppInfo" for _, path, _ in self.client.calls))
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/apps/activate"])

    def test_launch_deadline_crossing_never_sends_negative_timeout(self):
        # The deadline may pass between reads: use one remaining-time sample.
        with patch("wda_controller.time.monotonic", side_effect=[100,104.99,105.01,105.02,105.02,105.02]), \
                patch("wda_controller.time.sleep"), \
                patch.object(self.phone,"active_app",return_value="com.example.previous") as active:
            error=self.assert_code("postcondition_failed",lambda:self.phone.launch_app("com.example.requested", verify=True, observe="none"))
        active.assert_called_once()
        self.assertGreater(active.call_args.kwargs["timeout"],0)
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual([path for _,path,_ in self.client.actions()],["/wda/apps/activate"])

    def test_launch_channel_failure_after_activation_is_not_replayed(self):
        fault = WDAError("unknown error", "XCTDaemonErrorDomain Code=41 Not authorized for performing UI testing actions")
        with patch.object(self.phone, "active_app", side_effect=fault) as read:
            error = self.assert_code("unknown error", lambda: self.phone.launch_app("com.example.requested", verify=True, observe="none"))
        read.assert_called_once()
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/apps/activate"])

    def test_exact_target_offscreen_or_occluded_is_not_clicked(self):
        for condition, code in (("offscreen", "offscreen_target"), ("occluded", "occluded_target")):
            with self.subTest(condition=condition):
                self.setUp()
                if condition == "offscreen":
                    self.client.elements[0]["rect"]["y"] = 900
                else:
                    self.client.elements[0]["hittable"] = False
                error = self.assert_code(code, lambda: self.phone.tap(selector={"label": "Target"}, observe="none"))
                self.assertFalse(error.details["action_executed"])
                self.assertEqual(error.details["target_rect"], self.client.elements[0]["rect"])
                self.assertEqual(error.details["viewport"], {**self.client.size, "units": "iPhone points"})
                # The failed selector step hands back the screen: the next call acts by coordinates.
                recovery = error.details["recovery"]
                self.assertEqual(recovery["use"], "coordinates")
                self.assertFalse(recovery["replay_action"])
                self.assertEqual(Path(error.details["observation"]["image"]["path"]).read_bytes(), self.client.screenshot)
                self.assertNotIn("nodes", error.details["observation"])
                if condition == "occluded":
                    self.assertEqual(recovery["next_tool"], "pua_tap")
                    self.assertNotIn("next_arguments", recovery)
                    self.assertIn("popup", recovery["next_step"])
                    self.assertIn("NOT proof", recovery["next_step"])
                    self.assertEqual(error.details["tap_point"], {"x": 195, "y": 222})
                else:
                    self.assertEqual(recovery["next_tool"], "pua_swipe")
                self.assertEqual(self.client.actions(), [])

    def add_match(self, ident, **changes):
        element = copy.deepcopy(self.client.elements[0])
        element.update(id=ident, **{k: v for k, v in changes.items() if k != "rect"})
        element["rect"].update(changes.get("rect", {}))
        self.client.elements.append(element)
        return element

    def test_separate_duplicate_targets_report_candidates_instead_of_guessing(self):
        for rich in (False, True):
            with self.subTest(rich=rich):
                self.setUp()
                self.client.rich_elements = rich
                self.add_match("duplicate", rect={"y": 400})
                error = self.assert_code("ambiguous_target", lambda: self.phone.tap(selector={"label": "Target"}, observe="none"))
                self.assertEqual(error.details["matches"], 2)
                self.assertFalse(error.details["action_executed"])
                self.assertEqual([c["index"] for c in error.details["candidates"]], [0, 1])
                self.assertEqual([c["rect"] for c in error.details["candidates"]], [[70, 200, 250, 44], [70, 400, 250, 44]])
                self.assertTrue(all(c["hittable"] for c in error.details["candidates"]))
                self.assertEqual("type" in error.details["candidates"][0], rich)
                self.assertNotIn("element_id", json.dumps(error.details))
                self.assertEqual(self.client.actions(), [])

    def test_selector_index_picks_one_reported_candidate(self):
        self.add_match("duplicate", rect={"y": 400})
        result = self.phone.tap(selector={"label": "Target", "index": 1})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/duplicate/click"])
        error = self.assert_code("no_such_element", lambda: self.phone.tap(selector={"label": "Target", "index": 2}))
        self.assertEqual(error.details["matches"], 2)
        self.assertFalse(error.details["action_executed"])

    def test_nested_or_coincident_matches_are_one_touch_target(self):
        for inner in ({"x": 80, "y": 210, "width": 60, "height": 22}, {}):
            with self.subTest(inner=inner):
                self.setUp()
                self.add_match("inner", rect=inner)
                result = self.phone.tap(selector={"label": "Target"})
                self.assertEqual(result["target"]["chosen"], "innermost_of_nested_matches")
                self.assertEqual(result["target"]["matches"], 2)
                expected = "inner" if inner else "target"
                self.assertEqual([path for _, path, _ in self.client.actions()], [f"/element/{expected}/click"])

    def test_nested_matches_skip_an_occluded_inner_element(self):
        self.add_match("inner", rect={"x": 80, "y": 210, "width": 60, "height": 22}, hittable=False)
        self.phone.tap(selector={"label": "Target"})
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])
        self.client.elements[0]["hittable"] = False
        self.client.calls.clear()
        error = self.assert_code("occluded_target", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertEqual(error.details["recovery"]["use"], "coordinates")
        self.assertEqual([candidate["tap"] for candidate in error.details["candidates"]], [[110, 221], [195, 222]])
        self.assertEqual(self.client.actions(), [])

    def test_offscreen_duplicate_does_not_block_the_one_visible_match(self):
        self.add_match("below", rect={"y": 2000})
        result = self.phone.tap(selector={"label": "Target"})
        self.assertEqual(result["target"], {"matches": 2, "chosen": "only_match_on_screen", "index": 0})
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])
        # Narrowing never replaces the hittable check of the chosen element.
        self.client.elements[0]["hittable"] = False
        self.client.calls.clear()
        self.assert_code("occluded_target", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertEqual(self.client.actions(), [])

    def test_all_matches_offscreen_is_not_reported_as_ambiguity(self):
        self.client.elements[0]["rect"]["y"] = 1500
        self.add_match("below", rect={"y": 2000})
        error = self.assert_code("offscreen_target", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertEqual(len(error.details["candidates"]), 2)
        self.assertFalse(error.details["action_executed"])
        self.assertEqual(self.client.actions(), [])

    def test_bare_id_matches_beyond_the_inspection_limit_stay_ambiguous(self):
        for number in range(12):
            self.add_match(f"far-{number}", rect={"y": 3000 + number * 50})
        error = self.assert_code("ambiguous_target", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertEqual(error.details["matches"], 13)
        self.assertTrue(error.details["candidates_truncated"])
        self.assertEqual(sum(path.endswith("/rect") for _, path, _ in self.client.calls), 8)
        self.assertEqual(self.client.actions(), [])
        # With inline rects every match is weighed, so the single visible one is certain.
        self.client.rich_elements = True
        self.client.calls.clear()
        result = self.phone.tap(selector={"label": "Target"})
        self.assertEqual(result["target"]["chosen"], "only_match_on_screen")
        self.assertFalse(any(path.endswith("/rect") for _, path, _ in self.client.calls))

    def test_typing_prefers_the_editable_match_among_same_labels(self):
        self.client.rich_elements = True
        self.add_match("container", kind="XCUIElementTypeOther", rect={"x": 0, "y": 180, "width": 390, "height": 90})
        self.add_match("key", kind="XCUIElementTypeButton", rect={"y": 700})
        result = self.phone.type_text({"label": "Target"}, "query")
        self.assertTrue(result["action_complete"])
        self.assertEqual(self.client.elements[0]["value"], "query")
        self.assertFalse(any("/element/container/" in path or "/element/key/" in path for _, path, _ in self.client.calls))

    def test_inline_element_attributes_replace_rect_and_type_reads(self):
        self.client.rich_elements = True
        self.phone.tap(selector={"label": "Target"})
        self.assertEqual([path for _, path, _ in self.client.calls],
                         ["/elements", "/window/size", "/element/target/attribute/hittable", "/element/target/click"])
        self.client.calls.clear()
        self.phone.type_text({"label": "Target"}, "text")
        self.assertEqual([path for _, path, _ in self.client.calls],
                         ["/elements", "/element/target/attribute/hittable", "/element/target/click",
                          "/element/target/clear", "/element/target/value"])

    def test_inline_type_still_refuses_secure_and_noneditable_fields(self):
        self.client.rich_elements = True
        for kind, secure in (("XCUIElementTypeSecureTextField", True), ("XCUIElementTypeButton", False)):
            with self.subTest(kind=kind):
                self.client.elements[0]["kind"] = kind
                error = self.assert_code("not_editable", lambda: self.phone.type_text({"label": "Target"}, "never typed"))
                self.assertEqual(error.details["secure_field"], secure)
                self.assertEqual(self.client.actions(), [])

    def test_label_contains_is_an_escaped_substring_query(self):
        self.client.elements[0]["label"] = "Alex, see you tomorrow, 19:30"
        result = self.phone.find({"label_contains": "Alex", "type": "TextField"})
        self.assertEqual(result["matches"], 1)
        self.assertEqual(result["elements"][0]["rect"], [70, 200, 250, 44])
        self.assertNotIn("element_id", result["elements"][0])
        self.assertEqual(predicate({"label_contains": "it's \n"}), "label CONTAINS 'it\\'s \\u000a'")
        for selector in ({"index": 0}, {"label": "a", "index": -1}, {"label": "a", "index": True},
                         {"label": "a", "index": 200}, {"predicate": "label == 'a'", "label_contains": "a"}):
            with self.subTest(selector=selector):
                self.assert_code("invalid_selector", lambda: predicate(selector))
        self.assertEqual(predicate({"predicate": "label BEGINSWITH 'a'", "index": 3}), "label BEGINSWITH 'a'")

    def test_unicode_text_round_trips_exactly_before_submit(self):
        text = "你好 👋 Café 漢字 e\u0301"
        result = self.phone.type_text({"label": "Target"}, text, submit=True, observe="none", verify=True)
        self.assertTrue(result["exact_readback"])
        self.assertTrue(result["submitted"])
        self.assertFalse(result["submission_verified"])
        self.assertEqual(self.client.elements[0]["value"], text)
        paths = [call[1] for call in self.client.calls]
        self.assertLess(max(i for i, path in enumerate(paths) if path.endswith("/attribute/value")), paths.index("/wda/keys"))
        self.assertEqual(self.client.actions()[-1][2], {"value": ["\n"]})

    def test_default_text_input_sends_full_text_once_without_trial_or_value_reads(self):
        text = "完整中文输入 👋 Café e\u0301 " * 100
        result = self.phone.type_text({"label": "Target"}, text)
        self.assertTrue(result["action_complete"])
        self.assertTrue(result["verification_deferred"])
        self.assertFalse(result["exact_readback"])
        self.assertFalse(result["verified"])
        self.assertEqual(self.client.elements[0]["value"], text)
        self.assertEqual(sum(path.endswith("/attribute/type") for _, path, _ in self.client.calls), 1)
        self.assertFalse(any(path.endswith("/attribute/value") or path.startswith("/source")
                             for _, path, _ in self.client.calls))
        self.assertEqual(sum(path == "/element/target/value" for _, path, _ in self.client.actions()), 1)

    def test_default_append_does_not_clear_or_read_existing_text(self):
        self.client.elements[0]["value"] = "Existing "
        result = self.phone.type_text({"label": "Target"}, "追加", replace=False)
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(self.client.elements[0]["value"], "Existing 追加")
        self.assertFalse(any(path.endswith("/clear") or path.endswith("/attribute/value")
                             for _, path, _ in self.client.calls))

    def test_uncertain_default_input_stops_without_retyping_or_submission(self):
        original = self.client.session

        def session(method, path, payload=None, timeout=None):
            if path == "/element/target/value":
                self.client.calls.append((method, path, payload))
                raise WDAError("action_uncertain", "Input response timed out", uncertain=True)
            return original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=session):
            error = self.assert_code("action_uncertain", lambda: self.phone.type_text({"label": "Target"}, "one full input", submit=True))
        self.assertTrue(error.uncertain)
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(sum(path == "/element/target/value" for _, path, _ in self.client.actions()), 1)
        self.assertFalse(any(path == "/wda/keys" for _, path, _ in self.client.actions()))

    def test_optimistic_input_still_refuses_a_secure_field_before_any_mutation(self):
        self.client.elements[0]["kind"] = "XCUIElementTypeSecureTextField"
        self.assert_code("not_editable", lambda: self.phone.type_text({"label": "Target"}, "do not type"))
        self.assertEqual(self.client.actions(), [])

    def test_input_mismatch_does_not_submit_or_retype(self):
        self.client.input_override = "你好 ?"
        error = self.assert_code("input_mismatch", lambda: self.phone.type_text({"label": "Target"}, "你好 👋", submit=True, observe="none", verify=True))
        self.assertIn("image", error.details["observation"])
        self.assertTrue(error.details["recovery"]["visual_check_required"])
        self.assertTrue(error.details["action_executed"])
        paths = [call[1] for call in self.client.actions()]
        self.assertNotIn("/wda/keys", paths)
        self.assertEqual(paths.count("/element/target/value"), 1)

    def test_newline_in_single_line_field_is_blocked_even_with_intent(self):
        self.assert_code("newline_requires_intent", lambda: self.phone.type_text({"label": "Target"}, "a\nb", observe="none"))
        self.assert_code("newline_unsafe", lambda: self.phone.type_text({"label": "Target"}, "a\nb", allow_newlines=True, observe="none"))
        self.assertEqual(self.client.actions(), [])

    def test_explicit_multiline_text_view_round_trips(self):
        self.client.elements[0]["kind"] = "XCUIElementTypeTextView"
        result = self.phone.type_text({"label": "Target"}, "第一行\n第二行", allow_newlines=True, observe="none", verify=True)
        self.assertTrue(result["exact_readback"])
        self.assertFalse(result["submitted"])

    def test_append_preserves_existing_text_and_verifies_combined_value(self):
        self.client.elements[0]["value"] = "Existing "
        result = self.phone.type_text({"label": "Target"}, "追加", replace=False, observe="none", verify=True)
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.elements[0]["value"], "Existing 追加")
        self.assertFalse(any(path.endswith("/clear") for _, path, _ in self.client.actions()))
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in self.client.calls), 2)

    def test_verified_replace_has_only_one_final_readback(self):
        self.client.elements[0]["value"] = "old value"
        result = self.phone.type_text({"label": "Target"}, "new value", verify=True)
        self.assertTrue(result["exact_readback"])
        self.assertEqual(sum(path.endswith("/attribute/value") for _, path, _ in self.client.calls), 1)
        self.assertEqual(sum(path.endswith("/attribute/type") for _, path, _ in self.client.calls), 1)

    def test_numeric_or_label_refresh_at_fixed_geometry_is_not_scroll_progress(self):
        for change in ("value", "label", "width", "opposite_direction"):
            with self.subTest(change=change):
                self.setUp()
                before = node("Counter 100", y=350, name="Stable row", value="100.00")
                after = copy.deepcopy(before)
                if change == "value":
                    after["value"] = "100.25"
                elif change == "label":
                    after["label"] = "Counter 200"
                elif change == "width":
                    after.update(y=300, width=300)
                else:
                    after["y"] = 400
                self.client.source_pages = [[before], [after]]
                error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, max_attempts=1))
                self.assertTrue(error.details["action_executed"])
                self.assertFalse(error.details["changed"])
                self.assertEqual(self.client.swipe_count, 1)

    def test_stable_name_can_verify_movement_while_numeric_label_and_value_refresh(self):
        self.client.source_pages = [
            [node("Balance 100", y=350, name="Stable row", value="100.00")],
            [node("Balance 200", y=300, name="Stable row", value="200.00")],
        ]
        result = self.phone.swipe(verify=True)
        self.assertTrue(result["progress_verified"])
        self.assertEqual(result["attempts"], 1)

    def test_duplicate_anchor_labels_do_not_create_false_scroll_progress(self):
        self.client.source_pages = [
            [node("Repeated row", y=350), node("Repeated row", y=450)],
            [node("Repeated row", y=300), node("Repeated row", y=400)],
        ]
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, max_attempts=1))
        self.assertFalse(error.details["changed"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_no_scroll_progress_returns_a_screenshot_before_an_alternate_gesture(self):
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, max_attempts=2, observe="tree"))
        self.assertEqual(error.details["attempts"], 1)
        self.assertEqual([call[1] for call in self.client.actions()], ["/wda/dragfromtoforduration"])
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["verified"])
        self.assertFalse(error.details["changed"])
        self.assertEqual(error.details["observation"]["nodes"][0]["label"], "Row 1")
        self.assertIn("image", error.details["observation"])
        self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 2)
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)
        self.assertFalse(error.details["recovery"]["same_gesture_retry"])
        self.assertFalse(error.details["recovery"]["end_of_list_proven"])

    def test_swipe_observation_modes_do_not_disable_progress_verification(self):
        for mode in ("none", "tree", "screenshot", "both"):
            with self.subTest(mode=mode):
                self.setUp()
                self.client.source_pages = [[node("Row 1", y=350)], [node("Row 1", y=300), node("Row 2", y=350)]]
                result = self.phone.swipe(verify=True, observe=mode)
                self.assertTrue(result["verified"])
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(self.client.swipe_count, 1)
                if mode == "none":
                    self.assertNotIn("observation", result)
                else:
                    observed = result["observation"]
                    self.assertIn("observation_id", observed)
                    self.assertEqual("image" in observed, mode in ("screenshot", "both"))
                    self.assertEqual("nodes" in observed, mode in ("tree", "both"))
                self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 2)

    def test_no_progress_with_observe_none_still_returns_visual_fallback(self):
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, observe="none"))
        self.assertIn("image", error.details["observation"])
        self.assertNotIn("nodes", error.details["observation"])
        self.assertTrue(error.details["action_executed"])
        self.assertEqual(error.details["attempts"], 1)
        self.assertEqual(self.client.swipe_count, 1)

    def test_post_swipe_tree_failure_preserves_accepted_gesture_without_retry(self):
        baseline = self.client.source()
        fault = WDAError("stale element reference", "Application local.pid.0 is not running")
        with patch.object(self.client, "source", side_effect=[baseline, fault]) as source:
            error = self.assert_code("stale element reference", lambda: self.phone.swipe(verify=True, observe="tree"))
        self.assertEqual(source.call_count, 2)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual(self.phone.accepted_actions, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertIn("verification_required", error.details)

    def test_post_swipe_screenshot_failure_retains_accepted_gesture_and_stops(self):
        self.client.source_pages = [[node("Row 1", y=350)], [node("Row 1", y=300), node("Row 2", y=350)]]
        original = self.client.request

        def request(method, path, payload=None, timeout=None):
            if path == "/screenshot":
                raise WDAError("pua_unreachable", "Screenshot channel disconnected")
            return original(method, path, payload, timeout)

        with patch.object(self.client, "request", side_effect=request):
            error = self.assert_code("pua_unreachable", lambda: self.phone.swipe(verify=True, observe="both"))
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual(self.phone.accepted_actions, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_uncertain_first_gesture_does_not_claim_an_accepted_action(self):
        original = self.client.session

        def session(method, path, payload=None, timeout=None):
            if path == "/wda/dragfromtoforduration":
                self.client.calls.append((method, path, payload))
                raise WDAError("action_uncertain", "Gesture response timed out", uncertain=True)
            return original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=session):
            error = self.assert_code("action_uncertain", lambda: self.phone.swipe())
        self.assertTrue(error.uncertain)
        self.assertFalse(error.details.get("action_executed", False))
        self.assertEqual(self.phone.accepted_actions, 0)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])

    def test_unseen_native_fallback_is_not_sent_even_if_it_would_time_out(self):
        original = self.client.session

        def session(method, path, payload=None, timeout=None):
            if path == "/wda/swipe":
                self.client.calls.append((method, path, payload))
                raise WDAError("action_uncertain", "Fallback response timed out", uncertain=True)
            return original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=session):
            error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, max_attempts=2))
        self.assertFalse(error.uncertain)
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(self.phone.accepted_actions, 1)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/dragfromtoforduration"])
        self.assertIn("image", error.details["observation"])

    def test_no_progress_returns_requested_screenshot_for_inspection(self):
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, observe="both", max_attempts=1))
        observed = error.details["observation"]
        self.assertEqual(Path(observed["image"]["path"]).read_bytes(), self.client.screenshot)
        self.assertEqual(observed["nodes"][0]["label"], "Row 1")
        self.assertIn(observed["observation_id"], self.phone.snapshots)
        self.assertEqual(error.details["attempts"], 1)

    def test_default_swipe_reports_deferred_verification_without_forcing_a_stop(self):
        result = self.phone.swipe()
        self.assertTrue(result["action_executed"])
        self.assertTrue(result["action_complete"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertFalse(result["progress_verified"])
        self.assertNotIn("verification_required", result)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertNotIn("observation", result)

    def test_exact_enabled_accepts_tree_strings_and_booleans_equally(self):
        self.client.elements.append({**self.client.elements[0], "id": "disabled", "enabled": False})
        for value, expected_id in ((True, "target"), ("true", "target"), (False, "disabled"), ("false", "disabled")):
            with self.subTest(value=value):
                result = self.phone.query({"label": "Target", "enabled": value})
                self.assertEqual(result["matches"], 1)
                self.assertEqual(result["elements"][0]["element_id"], expected_id)

    def test_exact_enabled_rejects_guesses_before_device_query(self):
        for value in ("TRUE", "yes", 1, None):
            with self.subTest(value=value):
                self.assert_code("invalid_selector", lambda: predicate({"label": "Target", "enabled": value}))
        self.assertEqual(self.client.calls, [])

    def test_exact_selector_rejects_nul_instead_of_silently_mismatching(self):
        self.assert_code("invalid_selector", lambda: self.phone.find({"label": "a\x00b"}))
        self.assertEqual(self.client.calls, [])

    def test_another_swipe_requires_a_new_call_after_visual_fallback(self):
        self.client.source_pages = [[node("Row 1", y=350)], [node("Row 1", y=350)], [node("Row 1", y=300), node("Row 2", y=350)]]
        error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, observe="tree"))
        self.assertEqual(self.client.swipe_count, 1)
        self.assertIn("image", error.details["observation"])
        result = self.phone.swipe(verify=True, observe="tree")
        self.assertTrue(result["verified"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["strategy"], "short_drag")
        self.assertEqual(self.client.swipe_count, 2)
        self.assertEqual(result["observation"]["nodes"][-1]["label"], "Row 2")

    def test_wait_caps_each_transport_request_at_two_seconds(self):
        with patch("wda_controller.time.monotonic", side_effect=[100.0, 100.1]):
            result = self.phone.wait({"label": "Target"}, timeout_seconds=8)
        self.assertTrue(result["verified"])
        self.assertEqual(self.client.timeouts, [("POST", "/elements", 2)])

    def test_wait_uses_remaining_deadline_for_later_poll(self):
        # The fake clock advances across a failed first query and the bounded sleep.
        original = self.client.session
        requests = []

        def first_missing_then_found(method, path, payload=None, timeout=None):
            requests.append(timeout)
            return [] if len(requests) == 1 else original(method, path, payload, timeout)

        with patch.object(self.client, "session", side_effect=first_missing_then_found), \
                patch("wda_controller.time.monotonic", side_effect=[100.0, 100.1, 100.2, 100.25, 100.9]), \
                patch("wda_controller.time.sleep") as sleeper:
            result = self.phone.wait({"label": "Target"}, timeout_seconds=1)
        self.assertTrue(result["verified"])
        self.assertEqual(result["polls"], 2)
        self.assertAlmostEqual(requests[0], 0.9)
        self.assertAlmostEqual(requests[1], 0.1)
        sleeper.assert_called_once_with(0.25)

    def test_zero_wait_performs_only_one_short_probe(self):
        self.client.elements.clear()
        with patch("wda_controller.time.monotonic", return_value=100.0):
            error = self.assert_code("postcondition_failed", lambda: self.phone.wait({"label": "Missing"}, timeout_seconds=0))
        self.assertEqual(error.details["polls"], 1)
        self.assertEqual([call for call in self.client.timeouts if call[1] == "/elements"], [("POST", "/elements", 0.5)])
        self.assertIn("image", error.details["observation"])

    def test_batch_continues_routine_actions_with_deferred_verification(self):
        result = self.phone.batch([
            {"op": "tap", "args": {"selector": {"label": "Target"}}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "continue"}},
            {"op": "press_button", "args": {"name": "home"}},
        ])
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 3)
        self.assertTrue(all(item["verification_deferred"] for item in result["results"]))
        self.assertEqual(self.client.elements[0]["value"], "continue")
        self.assertEqual(self.client.actions()[-1][1], "/wda/homescreen")
        self.assertFalse(any(path.startswith("/source") or path == "/wda/activeAppInfo" or path.endswith("/attribute/value")
                             for _, path, _ in self.client.calls))

    def test_batch_stops_after_uncertain_error(self):
        self.client.click_error = WDAError("action_uncertain", "Timeout", uncertain=True)
        result = self.phone.batch([
            {"op": "tap", "args": {"selector": {"label": "Target"}, "observe": "none"}},
            {"op": "press_button", "args": {"name": "home", "observe": "none"}},
        ])
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 0)
        self.assertTrue(result["error"]["uncertain"])
        self.assertEqual(result["stop_reason"], "action_uncertain")
        self.assertFalse(any(path == "/wda/pressButton" for _, path, _ in self.client.actions()))
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in self.client.actions()))

    def test_batch_blocked_target_preserves_failed_step_and_does_not_continue(self):
        self.client.elements[0]["hittable"] = False
        result = self.phone.batch([
            {"op": "observe", "args": {}},
            {"op": "tap", "args": {"selector": {"label": "Target"}, "observe": "none"}},
            {"op": "press_button", "args": {"name": "home", "observe": "none"}},
        ])
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stop_reason"], "occluded_target")
        self.assertFalse(result["error"]["action_executed"])
        self.assertEqual(result["error"]["recovery"]["use"], "coordinates")
        self.assertIn("image", result["error"]["observation"])
        self.assertEqual(self.client.actions(), [])

    def test_batch_submission_is_a_verification_barrier(self):
        result = self.phone.batch([
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Verified input", "submit": True, "observe": "none"}},
            {"op": "press_button", "args": {"name": "home", "observe": "none"}},
        ])
        self.assertEqual(result["stop_reason"], "submission_requires_verification")
        self.assertEqual(result["completed_steps"], 1)
        self.assertFalse(any(path == "/wda/pressButton" for _, path, _ in self.client.actions()))
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in self.client.actions()))

    def test_batch_can_continue_after_explicit_submission_expectation(self):
        result = self.phone.batch([
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Full input",
                                          "submit": True, "expect": {"label": "Target"}}},
            {"op": "press_button", "args": {"name": "home"}},
        ])
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 2)
        self.assertTrue(result["results"][0]["submission_verified"])
        self.assertEqual(self.client.actions()[-1][1], "/wda/homescreen")

    def test_batch_explicit_expectation_failure_stops_before_next_action(self):
        # Batch budget start, viewport stamp, wait deadline, first poll, then past the deadline.
        clock = iter([100, 100, 100, 100.1])
        with patch("wda_controller.time.monotonic", side_effect=lambda: next(clock, 107)):
            result = self.phone.batch([
                {"op": "tap", "args": {"selector": {"label": "Target"}, "expect": {"label": "Missing"}}},
                {"op": "press_button", "args": {"name": "home"}},
            ])
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "postcondition_failed")
        self.assertTrue(result["error"]["action_executed"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])

    def test_batch_continues_after_verified_home_navigation(self):
        result = self.phone.batch([
            {"op": "press_button", "args": {"name": "home", "observe": "none", "verify": True}},
            {"op": "observe", "args": {}},
        ])
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 2)
        self.assertTrue(result["results"][0]["foreground_verified"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/homescreen"])

    def test_batch_completes_observation_and_verified_input_steps(self):
        result = self.phone.batch([
            {"op": "observe", "args": {}},
            {"op": "wait", "args": {"selector": {"label": "Target"}, "timeout_seconds": 0}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Verified input", "observe": "none", "verify": True}},
        ])
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 3)
        self.assertEqual(self.client.elements[0]["value"], "Verified input")

    def test_collect_list_does_not_claim_completeness_at_page_budget(self):
        self.client.source_pages = [[node("Row 1", y=350)], [node("Row 1", y=300), node("Row 2", y=350)]]
        result = self.phone.collect_list(max_pages=2)
        self.assertEqual([row["label"] for row in result["rows"]], ["Row 1", "Row 2"])
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(result["stop_reason"], "page_budget")
        self.assertFalse(result["complete"])
        self.assertFalse(result["coverage_verified"])
        self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 2)

    def test_scroll_find_reuses_the_one_matching_query_for_hittability(self):
        result = self.phone.scroll_find({"label": "Target"}, max_swipes=0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["swipes"], 0)
        self.assertEqual(sum(path == "/elements" for _, path, _ in self.client.calls), 1)
        self.assertEqual(sum(path == "/element/target/rect" for _, path, _ in self.client.calls), 1)
        self.assertEqual(sum(path == "/element/target/attribute/hittable" for _, path, _ in self.client.calls), 1)
        self.assertEqual(self.client.actions(), [])

    def test_scroll_find_requeries_once_after_lightweight_scroll(self):
        self.client.elements[0]["rect"]["y"] = 900
        self.client.gesture_effects = [lambda: self.client.elements[0]["rect"].update(y=200)]
        result = self.phone.scroll_find({"label": "Target"}, max_swipes=1)
        self.assertTrue(result["verified"])
        self.assertEqual(result["swipes"], 1)
        self.assertEqual(sum(path == "/elements" for _, path, _ in self.client.calls), 2)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))

    def test_scroll_find_returns_screen_after_one_unresolved_swipe_even_with_large_budget(self):
        self.client.elements.clear()
        error = self.assert_code("search_exhausted", lambda: self.phone.scroll_find({"label": "Missing"}, max_swipes=10))
        self.assertEqual(error.details["swipes"], 1)
        self.assertEqual(error.details["max_swipes"], 10)
        self.assertEqual(error.details["stop_reason"], "visual_check_required")
        self.assertTrue(error.details["recovery"]["visual_check_required"])
        self.assertTrue(error.details["action_executed"])
        self.assertFalse(error.details["action_complete"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual(sum(path == "/elements" for _, path, _ in self.client.calls), 2)
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)
        self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))

    def test_scroll_find_does_not_swipe_a_target_covered_by_a_popup(self):
        self.client.elements[0]["hittable"] = False
        error = self.assert_code("occluded_target", lambda: self.phone.scroll_find({"label": "Target"}, max_swipes=10))
        self.assertEqual(error.details["swipes"], 0)
        self.assertFalse(error.details["action_executed"])
        self.assertIn("image", error.details["observation"])
        self.assertEqual(self.client.actions(), [])

    def test_scroll_find_stops_when_the_target_remains_offscreen_after_one_swipe(self):
        self.client.elements[0]["rect"]["y"] = 900
        error = self.assert_code("offscreen_target", lambda: self.phone.scroll_find({"label": "Target"}, max_swipes=5))
        self.assertEqual(error.details["swipes"], 1)
        self.assertTrue(error.details["action_executed"])
        self.assertIn("image", error.details["observation"])
        self.assertEqual(self.client.swipe_count, 1)

    def test_scroll_find_zero_budget_returns_screen_without_a_gesture(self):
        self.client.elements.clear()
        error = self.assert_code("search_exhausted", lambda: self.phone.scroll_find({"label": "Missing"}, max_swipes=0))
        self.assertEqual(error.details["swipes"], 0)
        self.assertFalse(error.details["action_executed"])
        self.assertIn("image", error.details["observation"])
        self.assertEqual(self.client.actions(), [])

    def test_nested_page_expectation_failure_captures_once_and_stops_batch(self):
        clock = iter([100] * 4)
        with patch("wda_controller.time.monotonic", side_effect=lambda: next(clock, 120)):
            result = self.phone.batch([
                {"op": "tap", "args": {"x": 100, "y": 200, "expect": {"label": "Missing"}}},
                {"op": "press_button", "args": {"name": "home"}},
            ])
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"]["code"], "postcondition_failed")
        self.assertTrue(result["error"]["action_executed"])
        self.assertIn("image", result["error"]["observation"])
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/wda/tap"])

    def test_screenshot_failure_keeps_original_ui_error_and_does_not_retry(self):
        original = self.client.request
        def request(method, path, payload=None, timeout=None):
            if path == "/screenshot":
                self.client.calls.append((method, path, payload))
                raise WDAError("pua_unreachable", "Screenshot disconnected")
            return original(method, path, payload, timeout)
        self.client.elements.clear()
        with patch.object(self.client, "request", side_effect=request):
            error = self.assert_code("search_exhausted", lambda: self.phone.scroll_find({"label": "Missing"}, max_swipes=10))
        self.assertTrue(error.details["action_executed"])
        self.assertEqual(error.details["observation_error"]["code"], "pua_unreachable")
        self.assertEqual(error.details["recovery"]["next_arguments"], {"mode": "screenshot"})
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)

    def test_wda_click_state_error_returns_screen_without_repeating_the_click(self):
        self.client.click_error = WDAError("element not interactable", "A popup intercepted the click")
        error = self.assert_code("element not interactable", lambda: self.phone.tap(selector={"label": "Target"}))
        self.assertIn("image", error.details["observation"])
        self.assertTrue(error.details["recovery"]["visual_check_required"])
        self.assertFalse(error.details["recovery"]["replay_action"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/target/click"])
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)

    def test_no_progress_screenshot_failure_preserves_failure_and_requested_tree(self):
        original = self.client.request
        def request(method, path, payload=None, timeout=None):
            if path == "/screenshot":
                self.client.calls.append((method, path, payload))
                raise WDAError("pua_unreachable", "Screenshot disconnected")
            return original(method, path, payload, timeout)
        with patch.object(self.client, "request", side_effect=request):
            error = self.assert_code("no_scroll_progress", lambda: self.phone.swipe(verify=True, observe="both"))
        self.assertEqual(error.details["observation_error"]["code"], "pua_unreachable")
        self.assertTrue(error.details["action_executed"])
        self.assertEqual(error.details["observation"]["nodes"][0]["label"], "Row 1")
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)

    def test_end_marker_verifies_coverage_but_requires_business_reconciliation(self):
        self.client.elements[0]["label"] = "End"
        result = self.phone.collect_list(max_pages=3, end_selector={"label": "End"})
        self.assertTrue(result["end_marker_seen"])
        self.assertTrue(result["coverage_verified"])
        self.assertFalse(result["complete"])
        self.assertEqual(result["stop_reason"], "explicit_end_marker")
        self.assertEqual(self.client.actions(), [])
        self.assertEqual(sum(path == "/elements" for _, path, _ in self.client.calls), 1)

    def test_collect_list_stops_on_no_progress_without_claiming_coverage(self):
        result = self.phone.collect_list(max_pages=4)
        self.assertEqual(result["stop_reason"], "no_progress")
        self.assertEqual(len(result["pages"]), 2)
        self.assertFalse(result["complete"])
        self.assertFalse(result["coverage_verified"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertEqual([row["label"] for row in result["rows"]], ["Row 1"])
        self.assertIn("image", result["observation"])
        self.assertTrue(result["recovery"]["visual_check_required"])
        self.assertEqual(sum(path == "/screenshot" for _, path, _ in self.client.calls), 1)

    def test_collect_list_collects_virtualized_replacement_pages_without_fallback(self):
        self.client.source_pages = [[node(label=label, y=350)] for label in ("A", "B", "C")]
        result = self.phone.collect_list(max_pages=3)
        self.assertEqual([row["label"] for row in result["rows"]], ["A", "B", "C"])
        self.assertEqual(len(result["pages"]), 3)
        self.assertEqual(result["stop_reason"], "page_budget")
        self.assertFalse(result["complete"])
        self.assertEqual(self.client.swipe_count, 2)
        self.assertEqual(sum(path.startswith("/source") for _, path, _ in self.client.calls), 3)
        self.assertFalse(any(path == "/wda/swipe" for _, path, _ in self.client.calls))

    def test_collect_list_value_or_unrelated_carousel_refresh_does_not_drive_more_scrolls(self):
        self.client.source_pages = [
            [node("Stable row", y=350, value=value), node("Banner " + value, y=50, kind="StaticText")]
            for value in ("100", "101", "102")
        ]
        result = self.phone.collect_list(max_pages=6)
        self.assertEqual(result["stop_reason"], "no_progress")
        self.assertEqual(len(result["pages"]), 2)
        self.assertEqual(self.client.swipe_count, 1)
        self.assertFalse(result["coverage_verified"])
        self.assertEqual([row["value"] for row in result["rows"]], ["100", "101"])

    def test_scroll_find_read_failure_after_gesture_preserves_execution_and_stops_batch(self):
        original = self.client.session
        def fail_later_query(method, path, payload=None, timeout=None):
            if path == "/elements" and self.client.swipe_count:
                raise WDAError("pua_unreachable", "Read response lost", uncertain=False)
            return original(method, path, payload, timeout)
        with patch.object(self.client, "session", side_effect=fail_later_query):
            result = self.phone.batch([
                {"op": "scroll_find", "args": {"selector": {"label": "Missing"}, "max_swipes": 2}},
                {"op": "press_button", "args": {"name": "home"}},
            ])
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped_at"], 0)
        self.assertFalse(result["error"]["uncertain"])
        self.assertTrue(result["error"]["action_executed"])
        self.assertFalse(result["error"]["action_complete"])
        self.assertEqual(self.client.swipe_count, 1)
        self.assertNotIn("/wda/homescreen", [path for _, path, _ in self.client.calls])

    def test_collect_list_read_failure_after_gesture_preserves_execution(self):
        self.client.source_pages = [[node(label="Stable row", y=350)], [node(label="Stable row", y=300)]]
        original = self.client.session
        def fail_later_query(method, path, payload=None, timeout=None):
            if path == "/elements" and self.client.swipe_count:
                raise WDAError("pua_unreachable", "Read response lost", uncertain=False)
            return original(method, path, payload, timeout)
        with patch.object(self.client, "session", side_effect=fail_later_query), self.assertRaises(WDAError) as caught:
            self.phone.collect_list(max_pages=3, end_selector={"label": "Missing"})
        self.assertTrue(caught.exception.details["action_executed"])
        self.assertFalse(caught.exception.details["action_complete"])
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(self.client.swipe_count, 1)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("clang"), "Apple Foundation requires macOS and clang")
class FoundationPredicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.probe = Path(cls.directory.name) / "predicate-probe"
        built = subprocess.run(["clang", "-framework", "Foundation", str(Path(__file__).with_name("predicate_probe.m")), "-o", str(cls.probe)],
                               capture_output=True, text=True, timeout=60)
        if built.returncode:
            raise AssertionError("Foundation predicate probe did not build: " + built.stderr)

    def evaluate(self, cases):
        result = subprocess.run([str(self.probe)], input=json.dumps(cases, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_exact_text_literals_preserve_controls_quotes_and_literal_escapes(self):
        values = ["第一行\n第二行", "literal\\n", "a'b", "a\\b", "a\\'b", "a\r\nb", "a\tb", "a\x01\x7fb",
                  "一\u2028二\u2029三", "你好 👋 Café e\u0301", "100%@", "a' OR TRUEPREDICATE OR label == 'b"]
        cases = []
        for value in values:
            query = predicate({"label": value})
            cases.extend([{"predicate": query, "object": {"label": value}},
                          {"predicate": query, "object": {"label": "different " + value}}])
        for index, result in enumerate(self.evaluate(cases)):
            with self.subTest(value=values[index // 2], same=index % 2 == 0):
                self.assertTrue(result["parsed"], result.get("error"))
                self.assertEqual(result["matched"], index % 2 == 0)

    def test_real_newline_and_literal_backslash_n_select_different_objects(self):
        cases = [{"predicate": predicate({"label": text}), "object": {"label": other}}
                 for text, other in (("a\nb", "a\\nb"), ("a\\nb", "a\nb"))]
        self.assertEqual(self.evaluate(cases), [{"parsed": True, "matched": False}, {"parsed": True, "matched": False}])

    def test_enabled_tree_strings_boolean_filter_and_combined_fields_match_exactly(self):
        cases = []
        for enabled in (True, "true", False, "false"):
            flag = enabled is True or enabled == "true"
            query = predicate({"label": "返回\n上一页", "name": "back", "type": "Button", "enabled": enabled})
            obj = {"label": "返回\n上一页", "name": "back", "type": "XCUIElementTypeButton", "enabled": flag}
            cases.extend([{"predicate": query, "object": obj}, {"predicate": query, "object": {**obj, "enabled": not flag}}])
        for index, result in enumerate(self.evaluate(cases)):
            self.assertTrue(result["parsed"], result.get("error"))
            self.assertEqual(result["matched"], index % 2 == 0)


if __name__ == "__main__":
    unittest.main()
