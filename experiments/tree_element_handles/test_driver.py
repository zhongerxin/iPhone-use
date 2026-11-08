import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import quote


SPEC = importlib.util.spec_from_file_location(
    "tree_element_handles_driver", Path(__file__).with_name("driver.py")
)
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


def target_node(**overrides):
    return dict(
        {"type": "Cell", "label": "Assistant, preview", "rect": [0, 272, 440, 73],
         "element_id": "handle/one ?"},
        **overrides,
    )


class RecordingPhone:
    def __init__(self, hittable=True, matches=1):
        self.events = []
        self.hittable = hittable
        self.handle_tree = {
            "nodes": [target_node()],
            "timings": {"snapshot": 0.1, "registration": 0.2},
            "registered_handles": 1,
        }
        self.lookup_tree = {
            "nodes": [target_node(element_id=None, native_uid="native-uid")],
            "timings": {"snapshot": 0.1, "registration": 0.0},
            "registered_handles": 0,
        }
        self.query_result = {"matches": matches, "elements": [target_node()]}
        self.observation = {"image": "mock-screenshot"}

    def get(self, path):
        self.events.append(("get", path))
        if path == "/wda/sourceWithHandles?max_nodes=200":
            return self.handle_tree
        if path == "/wda/sourceWithHandles?max_nodes=200&register_handles=false":
            return self.lookup_tree
        if path.endswith("/attribute/hittable"):
            return self.hittable
        raise AssertionError("Unexpected GET: " + path)

    def tree(self, include_invisible=False, expensive_visibility=False):
        self.events.append(("tree", (include_invisible, expensive_visibility)))
        return {"nodes": [target_node(element_id=None)]}

    def query(self, selector):
        self.events.append(("query", selector))
        return self.query_result

    def post(self, path, body):
        self.events.append(("post", path, body))
        return None

    def observe(self, mode):
        self.events.append(("observe", mode))
        return self.observation


class SelectTargetTests(unittest.TestCase):
    def test_selects_unique_cell_by_label_substring(self):
        selected = target_node()
        nodes = [
            target_node(type="StaticText"),
            target_node(label="Other conversation"),
            selected,
        ]
        self.assertIs(driver.select_target(nodes, "Assistant"), selected)

    def test_missing_or_multiple_matching_cells_are_rejected(self):
        cases = {
            "no matching label": [target_node(label="Other conversation")],
            "wrong type": [target_node(type="Button")],
            "multiple matching cells": [target_node(), target_node(element_id="two")],
        }
        for name, nodes in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(RuntimeError, "exactly one visible target Cell"):
                    driver.select_target(nodes, "Assistant")

    def test_matching_cell_must_have_registered_handle(self):
        for element_id in (None, ""):
            with self.subTest(element_id=element_id):
                with self.assertRaisesRegex(RuntimeError, "no registered handle"):
                    driver.select_target([target_node(element_id=element_id)], "Assistant")

    def test_native_uid_alone_is_not_a_registered_handle(self):
        node = target_node(native_uid="native-uid")
        del node["element_id"]
        with self.assertRaisesRegex(RuntimeError, "no registered handle"):
            driver.select_target([node], "Assistant")


class BuildCommandTests(unittest.TestCase):
    def test_overrides_source_and_derived_path_without_mutating_manager_config(self):
        config = {"source_dir": "/stock/WDA", "udid": "test-device", "local_port": 8100}
        original = copy.deepcopy(config)
        manager = SimpleNamespace(config=config)

        def native_command(config_argument, action):
            return [
                "xcodebuild", "-project", config_argument["source_dir"] + "/WebDriverAgent.xcodeproj",
                "-derivedDataPath", "/stock/derived", action,
            ]

        manager._build_command = Mock(side_effect=native_command)
        command = driver.build_command(
            manager, Path("/experiment/WDA"), Path("/experiment/derived"), "build-for-testing"
        )

        self.assertIs(manager.config, config)
        self.assertEqual(manager.config, original)
        passed_config, action = manager._build_command.call_args.args
        self.assertIsNot(passed_config, manager.config)
        self.assertEqual(passed_config, dict(original, source_dir="/experiment/WDA"))
        self.assertEqual(action, "build-for-testing")
        self.assertEqual(command, [
            "xcodebuild", "-project", "/experiment/WDA/WebDriverAgent.xcodeproj",
            "-derivedDataPath", "/experiment/derived",
            "OTHER_CFLAGS=$(inherited) -Wno-error=poison-system-directories", "build-for-testing",
        ])


class TrialTests(unittest.TestCase):
    def make_experiment(self, phone):
        experiment = driver.Experiment.__new__(driver.Experiment)
        def session(method, path):
            self.assertEqual(method, "GET")
            return phone.get(path)
        experiment.runtime = SimpleNamespace(phone=phone, client=SimpleNamespace(session=session))
        experiment.last_tree = None
        return experiment

    def element_path(self, suffix):
        return "/element/" + quote(target_node()["element_id"], safe="") + suffix

    def assert_success_stages(self, stages, names):
        for name in names:
            self.assertIsInstance(stages[name], (float, int))
            self.assertGreaterEqual(stages[name], 0)
        self.assertEqual(stages["target_rect"], target_node()["rect"])
        self.assertEqual(stages["path_total"], sum(stages[name] for name in names))

    def test_handles_route_reads_tree_then_checks_hittable_then_clicks_once(self):
        phone = RecordingPhone()
        experiment = self.make_experiment(phone)
        entry = {}
        result = experiment.trial({"route": "handles", "target": "Assistant"}, entry)

        self.assertEqual(phone.events, [
            ("get", "/wda/sourceWithHandles?max_nodes=200"),
            ("get", self.element_path("/attribute/hittable")),
            ("post", self.element_path("/click"), {}),
            ("observe", "screenshot"),
        ])
        self.assertIs(experiment.last_tree, phone.handle_tree)
        self.assertEqual(result, {"clicked": True, "image_observation": phone.observation})
        self.assertEqual(entry["stages"]["server_timings"], phone.handle_tree["timings"])
        self.assertEqual(entry["stages"]["registered_handles"], 1)
        self.assertNotIn("lookup", entry["stages"])
        self.assert_success_stages(entry["stages"], ("observation", "hittable", "click"))

    def test_ordinary_route_observes_then_queries_and_uses_same_safety_check(self):
        phone = RecordingPhone(hittable="true")
        experiment = self.make_experiment(phone)
        entry = {}
        result = experiment.trial({"route": "ordinary", "target": "Assistant"}, entry)

        self.assertEqual(phone.events, [
            ("tree", (False, False)),
            ("query", {"type": "Cell", "label_contains": "Assistant"}),
            ("get", self.element_path("/attribute/hittable")),
            ("post", self.element_path("/click"), {}),
            ("observe", "screenshot"),
        ])
        self.assertIsNone(experiment.last_tree)
        self.assertEqual(result, {"clicked": True, "image_observation": phone.observation})
        self.assertNotIn("server_timings", entry["stages"])
        self.assert_success_stages(entry["stages"], ("observation", "lookup", "hittable", "click"))

    def test_lookup_control_disables_registration_then_queries_before_clicking(self):
        phone = RecordingPhone()
        experiment = self.make_experiment(phone)
        entry = {}
        result = experiment.trial({"route": "lookup-control", "target": "Assistant"}, entry)

        self.assertEqual(phone.events, [
            ("get", "/wda/sourceWithHandles?max_nodes=200&register_handles=false"),
            ("query", {"type": "Cell", "label_contains": "Assistant"}),
            ("get", self.element_path("/attribute/hittable")),
            ("post", self.element_path("/click"), {}),
            ("observe", "screenshot"),
        ])
        self.assertIsNone(experiment.last_tree)
        self.assertEqual(result, {"clicked": True, "image_observation": phone.observation})
        self.assertEqual(entry["stages"]["server_timings"], phone.lookup_tree["timings"])
        self.assertEqual(entry["stages"]["registered_handles"], 0)
        self.assert_success_stages(entry["stages"], ("observation", "lookup", "hittable", "click"))

    def test_false_hittable_never_posts_click_or_observes_screenshot(self):
        for route in ("handles", "ordinary", "lookup-control"):
            for hittable in (False, "false"):
                with self.subTest(route=route, hittable=hittable):
                    phone = RecordingPhone(hittable=hittable)
                    experiment = self.make_experiment(phone)
                    entry = {}
                    with self.assertRaisesRegex(RuntimeError, "not hittable; no click executed"):
                        experiment.trial({"route": route, "target": "Assistant"}, entry)
                    self.assertEqual(phone.events[-1], ("get", self.element_path("/attribute/hittable")))
                    self.assertFalse(any(event[0] == "post" for event in phone.events))
                    self.assertFalse(any(event[0] == "observe" for event in phone.events))
                    self.assertNotIn("click", entry["stages"])

    def test_nonunique_query_never_checks_or_clicks_target(self):
        for route, observation in (("ordinary", "tree"), ("lookup-control", "get")):
            for matches in (0, 2):
                with self.subTest(route=route, matches=matches):
                    phone = RecordingPhone(matches=matches)
                    experiment = self.make_experiment(phone)
                    with self.assertRaisesRegex(RuntimeError, "Target is not unique"):
                        experiment.trial({"route": route, "target": "Assistant"}, {})
                    self.assertEqual([event[0] for event in phone.events], [observation, "query"])


if __name__ == "__main__":
    unittest.main()
