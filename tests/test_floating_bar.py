from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from wda_client import WDAError
from wda_controller import ELEMENT_KEY, PhoneController


def page(rows, bar=True, tall_toolbar=False):
    """A list with rows at the given y (height 44) and, optionally, an iOS 26 floating tab bar at y=760."""
    cells = "".join(
        f'<XCUIElementTypeCell type="XCUIElementTypeCell" label="{label}" x="0" y="{y}" width="390" height="44"/>'
        for label, y in rows)
    extra = ""
    if bar:
        extra += ('<XCUIElementTypeTabBar type="XCUIElementTypeTabBar" label="Tab Bar" x="20" y="760" width="350" height="60">'
                  '<XCUIElementTypeButton type="XCUIElementTypeButton" label="Home" x="30" y="770" width="80" height="40"/>'
                  '</XCUIElementTypeTabBar>')
    if tall_toolbar:
        extra += '<XCUIElementTypeToolbar type="XCUIElementTypeToolbar" label="Container" x="0" y="0" width="390" height="844"/>'
    return ('<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="com.example.app" x="0" y="0" width="390" height="844">'
            f'<XCUIElementTypeTable type="XCUIElementTypeTable" x="0" y="0" width="390" height="844">{cells}</XCUIElementTypeTable>'
            f'{extra}</XCUIElementTypeApplication>')


PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXuoAAAAASUVORK5CYII="


class FakeWDA:
    def __init__(self, pages):
        self.pages, self.drags, self.calls = pages, 0, []

    def rows(self):
        xml = self.pages[min(self.drags, len(self.pages) - 1)]
        return {m[0]: float(m[1]) for m in re.findall(r'label="([^"]+)" x="0" y="([\d.]+)" width="390" height="44"', xml)}, xml

    def request(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if path == "/wda/activeAppInfo":
            return {"value": {"bundleId": "com.example.app"}}
        if path == "/screenshot":  # failures attach a screenshot
            return {"value": PIXEL}
        raise AssertionError(path)

    def session(self, method, path, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        rows, xml = self.rows()
        if path.startswith("/source"):
            return xml
        if path == "/window/size":
            return {"width": 390, "height": 844}
        if path == "/elements":
            label = re.search(r"label == '([^']*)'", payload["value"])[1]
            return [{ELEMENT_KEY: label, "rect": {"x": 0, "y": rows[label], "width": 390, "height": 44}}] if label in rows else []
        if path.startswith("/element/"):
            label = path.split("/")[2]
            if path.endswith("/rect"):
                return {"x": 0, "y": rows[label], "width": 390, "height": 44}
            if path.endswith("/attribute/hittable"):
                return True  # rows under a floating bar may still report hittable
            if path.endswith("/click"):
                return None
        if path == "/wda/dragfromtoforduration":
            self.drags += 1
            return None
        if path == "/wda/tap":
            return None
        raise AssertionError(path)


class FloatingBarTest(unittest.TestCase):
    def controller(self, pages):
        client = FakeWDA(pages)
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        controller = PhoneController(client, state.name)
        return controller, client

    def test_tree_marks_rows_under_bar_but_not_bar_buttons(self):
        controller, _ = self.controller([page([("Top", 100), ("Low", 760)])])
        nodes, _ = controller.tree()
        by_label = {node.get("label"): node for node in nodes}
        self.assertTrue(by_label["Low"].get("under_bar"))
        self.assertNotIn("under_bar", by_label["Top"])
        self.assertNotIn("under_bar", by_label["Home"])

    def test_screen_tall_toolbar_is_not_a_bar(self):
        controller, _ = self.controller([page([("Low", 760)], bar=False, tall_toolbar=True)])
        nodes, _ = controller.tree()
        self.assertFalse(any(node.get("under_bar") for node in nodes))

    def test_tap_on_covered_row_lifts_list_then_clicks(self):
        controller, client = self.controller([page([("Low", 760)]), page([("Low", 400)])])
        controller.tap({"label": "Low"})
        drags = [payload for _, path, payload in client.calls if path == "/wda/dragfromtoforduration"]
        self.assertEqual(len(drags), 1)
        self.assertLess(drags[0]["fromY"], 760 - 14)  # the drag starts above the bar, not on it
        self.assertLess(drags[0]["toY"], drags[0]["fromY"])
        self.assertIn(("POST", "/element/Low/click", {}), client.calls)

    def test_still_covered_after_lift_reports_occluded_without_tapping(self):
        controller, client = self.controller([page([("Low", 760)])])
        with self.assertRaises(WDAError) as caught:
            controller.tap({"label": "Low"})
        self.assertEqual(caught.exception.code, "occluded_target")
        self.assertTrue(caught.exception.details["action_executed"])
        self.assertNotIn(("POST", "/element/Low/click", {}), client.calls)

    def test_uncovered_row_taps_without_lift(self):
        controller, client = self.controller([page([("Mid", 400)])])
        controller.tap({"label": "Mid"})
        self.assertFalse(any(path == "/wda/dragfromtoforduration" for _, path, _ in client.calls))
        self.assertFalse(any(path.startswith("/source") for _, path, _ in client.calls))  # high targets skip the extra read

    def test_coordinate_tap_is_unchanged(self):
        controller, client = self.controller([page([("Low", 760)])])
        controller.tap(x=195, y=782)
        self.assertIn(("POST", "/wda/tap", {"x": 195, "y": 782}), client.calls)
        self.assertFalse(any(path == "/wda/dragfromtoforduration" for _, path, _ in client.calls))


if __name__ == "__main__":
    unittest.main()
