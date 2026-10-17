import copy
import re
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from test_controller import FakeWDA
from wda_client import WDAError
from wda_controller import ELEMENT_KEY, PhoneController


class SwitchFakeWDA(FakeWDA):
    def session(self, method, path, payload=None, timeout=None):
        if path == "/elements":
            value = payload["value"]
            matches = list(self.elements)
            labels = re.findall(r"label == '([^']*)'", value)
            if labels:
                matches = [item for item in matches if item.get("label") == labels[0]]
            types = re.findall(r"type == '([^']*)'", value)
            if types:
                matches = [item for item in matches if item.get("kind") == types[0]]
            enabled = re.search(r"enabled == (true|false)", value)
            if enabled:
                matches = [item for item in matches if item.get("enabled", True) is (enabled[1] == "true")]
            if "CONTAINS" in value:
                fragment = re.search(r"label CONTAINS '([^']*)'", value)[1]
                matches = [item for item in matches if fragment in item.get("label", "")]
            if not self.rich_elements:
                return [{ELEMENT_KEY: item["id"]} for item in matches]
            return [{ELEMENT_KEY: item["id"], "ELEMENT": item["id"], "type": item["kind"], "label": item["label"],
                     "rect": item["rect"].copy(), "enabled": item.get("enabled", True),
                     "attribute/name": item.get("name", item["label"]), "attribute/value": item.get("value") or None}
                    for item in matches]
        return super().session(method, path, payload, timeout)


class SwitchTapTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = SwitchFakeWDA()
        self.phone = PhoneController(self.client, self.directory.name)

    def assert_code(self, code, operation):
        with self.assertRaises(WDAError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_switch_with_same_name_label_taps_right_end(self):
        for rich in (False, True):
            with self.subTest(rich=rich):
                self.setUp()
                self.client.rich_elements = rich
                self.client.elements = [
                    {"id": "sw", "label": "Wi-Fi", "kind": "XCUIElementTypeSwitch",
                     "rect": {"x": 30, "y": 100, "width": 330, "height": 44}, "hittable": True, "value": "1"},
                    {"id": "lbl", "label": "Wi-Fi", "kind": "XCUIElementTypeStaticText",
                     "rect": {"x": 30, "y": 110, "width": 80, "height": 24}, "hittable": True, "value": "Wi-Fi"},
                ]
                result = self.phone.tap(selector={"label": "Wi-Fi"})
                self.assertTrue(result["action_executed"])
                self.assertEqual(result["target"]["chosen"], "switch_knob")
                self.assertEqual(result["target"]["matches"], 2)
                # x = Switch.x + Switch.width - 26 = 30 + 330 - 26 = 334
                # y = Switch 垂直中心 = 100 + 44 / 2 = 122
                expected_x = 30 + 330 - 26
                expected_y = 100 + 22
                actions = self.client.actions()
                self.assertEqual(len(actions), 1)
                method, path, payload = actions[0]
                self.assertEqual(method, "POST")
                self.assertEqual(path, "/wda/tap")
                self.assertEqual(payload, {"x": expected_x, "y": expected_y})
                self.assertIsInstance(payload["x"], int)
                self.assertIsInstance(payload["y"], int)

    def test_two_independent_matches_stay_ambiguous(self):
        # A switch and an independent element with the same label at a different location
        for rich in (False, True):
            with self.subTest(rich=rich):
                self.setUp()
                self.client.rich_elements = rich
                self.client.elements = [
                    {"id": "sw", "label": "Wi-Fi", "kind": "XCUIElementTypeSwitch",
                     "rect": {"x": 30, "y": 100, "width": 330, "height": 44}, "hittable": True, "value": "1"},
                    {"id": "other", "label": "Wi-Fi", "kind": "XCUIElementTypeCell",
                     "rect": {"x": 30, "y": 400, "width": 330, "height": 44}, "hittable": True, "value": ""},
                ]
                error = self.assert_code("ambiguous_target", lambda: self.phone.tap(selector={"label": "Wi-Fi"}))
                self.assertEqual(error.details["matches"], 2)
                self.assertEqual(self.client.actions(), [])

    def test_two_switches_stay_ambiguous(self):
        # Two switches matching the selector
        self.client.elements = [
            {"id": "sw1", "label": "Option", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 30, "y": 100, "width": 330, "height": 44}, "hittable": True, "value": "1"},
            {"id": "sw2", "label": "Option", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 30, "y": 200, "width": 330, "height": 44}, "hittable": True, "value": "0"},
        ]
        error = self.assert_code("ambiguous_target", lambda: self.phone.tap(selector={"label": "Option"}))
        self.assertEqual(error.details["matches"], 2)
        self.assertEqual(self.client.actions(), [])

    def test_non_switch_target_taps_center(self):
        # Non-switch target clicks center via /element/{id}/click; screen telemetry records center point
        mock_screen = unittest.mock.Mock()
        self.phone.screen = mock_screen
        self.client.elements = [
            {"id": "btn", "label": "Submit", "kind": "XCUIElementTypeButton",
             "rect": {"x": 60, "y": 200, "width": 250, "height": 44}, "hittable": True, "value": ""},
        ]
        result = self.phone.tap(selector={"label": "Submit"})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in self.client.actions()], ["/element/btn/click"])
        # Non-switch center: x = 60 + 250/2 = 185, y = 200 + 44/2 = 222
        self.assertEqual(self.phone._screen_targets["/element/btn"], {"x": 185.0, "y": 222.0})
        mock_screen.gesture.assert_called_with("tap", point={"x": 185.0, "y": 222.0}, viewport=unittest.mock.ANY)

    def test_single_switch_match_taps_right_end(self):
        self.client.elements = [
            {"id": "sw", "label": "Airplane Mode", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 20, "y": 150, "width": 330, "height": 44}, "hittable": True, "value": "0"},
        ]
        result = self.phone.tap(selector={"label": "Airplane Mode"})
        self.assertTrue(result["action_executed"])
        self.assertEqual(result["target"]["chosen"], "switch_knob")
        self.assertEqual(result["target"]["matches"], 1)
        expected_x = 20 + 330 - 26
        expected_y = 150 + 22
        self.assertEqual(self.client.actions(), [("POST", "/wda/tap", {"x": expected_x, "y": expected_y})])

    def test_switch_resolved_by_index_taps_right_end(self):
        self.client.elements = [
            {"id": "sw1", "label": "Setting", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 20, "y": 100, "width": 330, "height": 44}, "hittable": True, "value": "1"},
            {"id": "sw2", "label": "Setting", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 20, "y": 200, "width": 330, "height": 44}, "hittable": True, "value": "0"},
        ]
        result = self.phone.tap(selector={"label": "Setting", "index": 1})
        self.assertTrue(result["action_executed"])
        self.assertEqual(result["target"]["chosen"], "switch_knob")
        self.assertEqual(result["target"]["matches"], 2)
        expected_x = 20 + 330 - 26
        expected_y = 200 + 22
        self.assertEqual(self.client.actions(), [("POST", "/wda/tap", {"x": expected_x, "y": expected_y})])

    def test_occluded_switch_reports_occluded_target_without_falling_back(self):
        # Switch is not hittable, but inner label is hittable; must not fall back to label
        self.client.elements = [
            {"id": "sw", "label": "Bluetooth", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 30, "y": 100, "width": 330, "height": 44}, "hittable": False, "value": "1"},
            {"id": "lbl", "label": "Bluetooth", "kind": "XCUIElementTypeStaticText",
             "rect": {"x": 30, "y": 110, "width": 80, "height": 24}, "hittable": True, "value": "Bluetooth"},
        ]
        error = self.assert_code("occluded_target", lambda: self.phone.tap(selector={"label": "Bluetooth"}))
        self.assertEqual(self.client.actions(), [])

    def test_nested_cell_switch_and_label_resolves_to_switch(self):
        # Cell contains Switch, Switch contains Label
        self.client.elements = [
            {"id": "cell", "label": "Cellular", "kind": "XCUIElementTypeCell",
             "rect": {"x": 0, "y": 90, "width": 390, "height": 60}, "hittable": True, "value": ""},
            {"id": "sw", "label": "Cellular", "kind": "XCUIElementTypeSwitch",
             "rect": {"x": 30, "y": 98, "width": 330, "height": 44}, "hittable": True, "value": "1"},
            {"id": "lbl", "label": "Cellular", "kind": "XCUIElementTypeStaticText",
             "rect": {"x": 30, "y": 108, "width": 80, "height": 24}, "hittable": True, "value": "Cellular"},
        ]
        result = self.phone.tap(selector={"label": "Cellular"})
        self.assertTrue(result["action_executed"])
        self.assertEqual(result["target"]["chosen"], "switch_knob")
        expected_x = 30 + 330 - 26
        expected_y = 98 + 22
        self.assertEqual(self.client.actions(), [("POST", "/wda/tap", {"x": expected_x, "y": expected_y})])

    def test_type_text_and_coordinate_tap_behavior_unchanged(self):
        # Coordinate tap taps given coordinates
        result = self.phone.tap(x=100, y=200)
        self.assertTrue(result["action_executed"])
        self.assertEqual(self.client.actions()[-1], ("POST", "/wda/tap", {"x": 100, "y": 200}))

        # type_text on an editable field is unchanged
        self.client.elements = [
            {"id": "field", "label": "Name", "kind": "XCUIElementTypeTextField",
             "rect": {"x": 60, "y": 200, "width": 250, "height": 44}, "hittable": True, "value": ""},
        ]
        text_result = self.phone.type_text({"label": "Name"}, "Hello")
        self.assertTrue(text_result["action_complete"])
        self.assertEqual(self.client.elements[0]["value"], "Hello")


if __name__ == "__main__":
    unittest.main()
