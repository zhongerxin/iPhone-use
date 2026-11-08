"""Native snapshot limits are opt-in, explicitly incomplete, and restored safely."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import SCHEMAS, validate
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


class SnapshotWDA(FakeWDA):
    def __init__(self):
        super().__init__()
        self.saved_settings = {"snapshotMaxDepth": 47, "snapshotMaxChildren": 123456,
                               "animationCoolOffTimeout": 0}
        self.source_root = {"name": "Example", "bundleId": "com.example.phone",
                            "width": 390, "height": 844}
        self.deferred_restores = []
        self.apply_error = None
        self.restore_error = None
        self.source_error = None

    def session(self, method, path, payload=None, timeout=None):
        if method == "GET" and path == "/appium/settings":
            self.calls.append((method, path, None))
            self.timeouts.append((method, path, timeout))
            return copy.deepcopy(self.saved_settings)
        if path.startswith("/source") and self.source_error:
            self.calls.append((method, path, copy.deepcopy(payload)))
            self.timeouts.append((method, path, timeout))
            raise self.source_error
        result = super().session(method, path, payload, timeout)
        if method == "POST" and path == "/appium/settings":
            error = self.apply_error if len(self.settings) == 1 else self.restore_error
            if error:
                raise error
        return result

    def reapply_settings(self, restore=None):
        self.deferred_restores.append(copy.deepcopy(restore))


class SnapshotLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.client = SnapshotWDA()
        self.phone = PhoneController(self.client, self.directory.name)

    def assert_code(self, code, operation):
        with self.assertRaises(WDAError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def setting_calls(self):
        return [call for call in self.client.calls if call[1] == "/appium/settings"]

    def test_limits_apply_before_source_and_restore_exact_originals(self):
        limits = {"snapshotMaxDepth": 20, "snapshotMaxChildren": 40}
        result = self.phone.observe(snapshot_max_depth=20, snapshot_max_children=40)
        self.assertEqual(self.setting_calls(), [
            ("GET", "/appium/settings", None),
            ("POST", "/appium/settings", {"settings": limits}),
            ("POST", "/appium/settings", {"settings": {"snapshotMaxDepth": 47, "snapshotMaxChildren": 123456}}),
        ])
        paths = [path for _, path, _ in self.client.calls]
        source = next(i for i, path in enumerate(paths) if path.startswith("/source"))
        setting_positions = [i for i, path in enumerate(paths) if path == "/appium/settings"]
        self.assertLess(setting_positions[1], source)
        self.assertGreater(setting_positions[2], source)
        self.assertEqual(result["snapshot_limits"], limits)
        self.assertFalse(result["tree_complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["total_nodes_scope"], "bounded_snapshot")
        self.assertGreater(result["total_nodes"], 0)
        self.assertLess(result["total_nodes"], 100)
        self.assertIn("not by visible area", result["warnings"][-1])
        self.assertEqual(self.client.deferred_restores, [])
        self.assertEqual(self.client.actions(), [])

    def test_one_limit_restores_only_that_setting(self):
        for argument, key, value in (("snapshot_max_depth", "snapshotMaxDepth", 20),
                                     ("snapshot_max_children", "snapshotMaxChildren", 40)):
            with self.subTest(argument=argument):
                self.client.calls.clear()
                self.client.settings.clear()
                result = self.phone.observe(**{argument: value})
                self.assertEqual(self.client.settings, [{key: value}, {key: self.client.saved_settings[key]}])
                self.assertEqual(result["snapshot_limits"], {key: value})

    def test_both_mode_restores_settings_before_capturing_image(self):
        result = self.phone.observe(mode="both", snapshot_max_children=40)
        self.assertIn("image", result)
        self.assertTrue(Path(result["image"]["path"]).is_file())
        self.assertFalse(result["tree_complete"])
        paths = [path for _, path, _ in self.client.calls]
        restore = max(i for i, path in enumerate(paths) if path == "/appium/settings")
        self.assertLess(restore, paths.index("/screenshot"))

    def test_limit_boundaries_are_shared_by_schema_and_controller(self):
        for depth, children in ((1, 1), (100, 500)):
            arguments = {"snapshot_max_depth": depth, "snapshot_max_children": children}
            validate(arguments, SCHEMAS["observe"])
            result = self.phone.observe(**arguments)
            self.assertEqual(result["snapshot_limits"], {
                "snapshotMaxDepth": depth, "snapshotMaxChildren": children})

    def test_ordinary_observation_has_no_settings_or_bounded_metadata(self):
        result = self.phone.observe()
        self.assertEqual(self.setting_calls(), [])
        self.assertEqual(set(result), {"observation_id", "observed_at", "app", "viewport",
                                       "nodes", "total_nodes", "truncated"})
        self.assertFalse(result["truncated"])

    def test_limits_are_rejected_for_screenshot_before_any_http(self):
        error = self.assert_code("invalid_argument", lambda: self.phone.observe(
            mode="screenshot", snapshot_max_children=40))
        self.assertFalse(error.details["action_executed"])
        self.assertEqual(self.client.calls, [])

    def test_invalid_limits_are_rejected_before_any_http(self):
        for argument, maximum in (("snapshot_max_depth", 100), ("snapshot_max_children", 500)):
            for value in (False, True, 0, -1, maximum + 1, 1.5, "20", float("inf")):
                with self.subTest(argument=argument, value=value):
                    self.assert_code("invalid_argument", lambda: self.phone.observe(**{argument: value}))
                    self.assertEqual(self.client.calls, [])

    def test_invalid_saved_settings_abort_before_apply_or_source(self):
        for saved in (None, [], {}, {"snapshotMaxDepth": True}, {"snapshotMaxDepth": 0},
                      {"snapshotMaxDepth": -1}, {"snapshotMaxDepth": 20.0}, {"snapshotMaxDepth": "50"}):
            with self.subTest(saved=saved):
                self.client.calls.clear()
                self.client.saved_settings = saved
                error = self.assert_code("invalid_response", lambda: self.phone.observe(snapshot_max_depth=20))
                self.assertFalse(error.details["action_executed"])
                self.assertEqual(self.client.calls, [("GET", "/appium/settings", None)])
                self.assertEqual(self.client.deferred_restores, [])

    def test_source_timeout_defers_restore_without_cleanup_http(self):
        self.client.source_error = WDAError("pua_timeout", "source timed out")
        error = self.assert_code("pua_timeout", lambda: self.phone.observe(
            snapshot_max_depth=20, snapshot_max_children=40))
        self.assertIs(error, self.client.source_error)
        self.assertEqual(self.client.settings, [{"snapshotMaxDepth": 20, "snapshotMaxChildren": 40}])
        self.assertTrue(self.client.calls[-1][1].startswith("/source"))
        self.assertEqual(self.client.deferred_restores, [{"snapshotMaxDepth": 47, "snapshotMaxChildren": 123456}])
        self.assertEqual(self.phone.snapshots, {})

    def test_failed_apply_defers_restore_without_source_or_cleanup_http(self):
        for failure in (WDAError("action_uncertain", "settings response lost", uncertain=True), RuntimeError("local failure")):
            with self.subTest(failure=type(failure).__name__):
                self.client.calls.clear()
                self.client.settings.clear()
                self.client.deferred_restores.clear()
                self.client.apply_error = failure
                with self.assertRaises(type(failure)) as caught:
                    self.phone.observe(snapshot_max_children=40)
                self.assertIs(caught.exception, failure)
                self.assertEqual(self.client.settings, [{"snapshotMaxChildren": 40}])
                self.assertEqual(self.client.calls[-1][1], "/appium/settings")
                self.assertFalse(any(path.startswith("/source") for _, path, _ in self.client.calls))
                self.assertEqual(self.client.deferred_restores, [{"snapshotMaxChildren": 123456}])

    def test_failed_success_restore_is_explicit_and_deferred(self):
        self.client.restore_error = WDAError("action_uncertain", "restore response lost", uncertain=True)
        error = self.assert_code("action_uncertain", lambda: self.phone.observe(snapshot_max_depth=20))
        self.assertFalse(error.details["action_executed"])
        self.assertEqual(error.details["operation"], "snapshot_settings_restore")
        self.assertEqual(self.client.settings, [{"snapshotMaxDepth": 20}, {"snapshotMaxDepth": 47}])
        self.assertEqual(self.client.deferred_restores, [{"snapshotMaxDepth": 47}])
        self.assertEqual(self.phone.snapshots, {})

    def test_empty_bounded_tree_retains_both_coverage_warnings(self):
        self.client.nodes = []
        result = self.phone.observe(snapshot_max_depth=20)
        self.assertEqual(result["total_nodes"], 0)
        self.assertFalse(result["tree_complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["warnings"]), 2)
        self.assertIn("Empty accessibility tree", result["warnings"][0])
        self.assertIn("not by visible area", result["warnings"][1])


if __name__ == "__main__":
    unittest.main()
