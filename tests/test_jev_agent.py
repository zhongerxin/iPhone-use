"""The Jev loop reduces model turns without replaying native mutations."""
import copy
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime
from jev_agent import JevAgent
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA, node


def choice(selected, question):
    return {"type": "choice", "choice": selected, "confidence": 1,
            "probabilities": {key: int(key == selected) for key in question["criteria"]}}


class FakeJev:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.requests = []

    def classify(self, state, questions, timeout=None):
        self.requests.append({"state": copy.deepcopy(state), "questions": copy.deepcopy(questions), "timeout": timeout})
        decision = self.decisions.pop(0)
        if isinstance(decision, Exception):
            raise decision
        operation, target = decision
        answers = {"operation": choice(operation, questions["operation"])}
        if target is not None:
            name = operation.lower() + "_target"
            answers[name] = choice(target, questions[name])
        return {"answers": answers, "model": "jev-test", "usage": {}, "latency_ms": 1}

    def close(self):
        pass


class JevAgentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runtime = Runtime(self.directory.name)
        self.addCleanup(self.runtime.close)
        self.wda = FakeWDA()
        self.wda.close = lambda: None
        self.wda.rich_elements = True
        self.wda.source_root = {"bundleId": self.wda.app, "width": 390, "height": 844}
        self.wda.nodes = [node("Target", kind="TextField", x=70)]
        self.runtime.client = self.wda
        self.runtime.phone = PhoneController(self.wda, self.directory.name)
        self.runtime.phone.settle_seconds = 0.8
        # Calling Runtime.call from inside the operation would reacquire flock.
        self.runtime.call = lambda *args, **kwargs: self.fail("Jev must use internal dispatch, never Runtime.call")

    def run_agent(self, decisions, **arguments):
        client = FakeJev(decisions)
        agent = JevAgent(self.runtime, client)
        result = agent.run(arguments.pop("goal", "Open the intended page"), **arguments)
        return result, client

    def paths(self):
        return [path for _, path, _ in self.wda.calls]

    def mutations(self):
        return [entry for entry in self.wda.actions() if entry[1] != "/session"]

    def transition_on_click(self):
        original = self.wda.session

        def session(method, path, payload=None, timeout=None):
            result = original(method, path, payload, timeout)
            if path == "/element/target/click":
                self.wda.nodes = [node("Next page", kind="StaticText")]
            return result

        self.wda.session = session

    def test_dry_run_selects_observed_fields_in_one_request_without_mutating(self):
        result, client = self.run_agent([("TAP", "0")], dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(result["complete"])
        self.assertEqual(result["decision"]["selector"], {"type": "TextField", "label": "Target"})
        self.assertEqual(self.mutations(), [])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(set(client.requests[0]["questions"]), {"operation", "tap_target", "type_text_target"})
        self.assertEqual(client.requests[0]["questions"]["tap_target"]["criteria"]["0"]["element"]["label"], "Target")
        self.assertNotIn("selector", client.requests[0]["questions"]["tap_target"]["criteria"]["0"])
        self.assertEqual(self.runtime.phone.settle_seconds, 0.8)

    def test_navigation_reuses_action_observation_and_done_is_unverified(self):
        self.transition_on_click()
        result, client = self.run_agent([("TAP", "0"), ("DONE", None)])
        self.assertEqual(result["status"], "done_unverified")
        self.assertFalse(result["complete"])
        self.assertEqual(result["steps"], 1)
        self.assertEqual(len([path for path in self.paths() if path.startswith("/source")]), 2)
        self.assertEqual(client.requests[1]["state"]["nodes"][0]["label"], "Next page")
        self.assertIn("/wda/activeAppInfo", self.paths())  # selected-observation context guard
        self.assertIn("/element/target/attribute/hittable", self.paths())
        self.assertEqual(sum(path == "/element/target/click" for path in self.paths()), 1)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertNotIn("nodes", result["trace"][0])
        self.assertNotIn("observation", result["results"][0])

    def test_transitional_geometry_refreshes_locally_before_next_model_decision(self):
        original_source = self.wda.source
        post_reads = 0

        def source():
            nonlocal post_reads
            if any(path == "/element/target/click" for path in self.paths()):
                post_reads += 1
                if post_reads == 1:
                    self.wda.nodes = [node("Target", kind="TextField", x=71, y=203)]
                else:
                    self.wda.nodes = [node("Next page", kind="StaticText")]
            return original_source()

        self.wda.source = source
        result, client = self.run_agent([("TAP", "0"), ("DONE", None)])
        self.assertEqual(result["status"], "done_unverified")
        self.assertEqual(client.requests[1]["state"]["nodes"][0]["label"], "Next page")
        self.assertEqual(len([path for path in self.paths() if path.startswith("/source")]), 3)
        self.assertEqual(sum(path == "/element/target/click" for path in self.paths()), 1)

    def test_geometry_jitter_is_not_progress_after_bounded_local_refreshes(self):
        original_source = self.wda.source

        def source():
            self.wda.nodes[0]["x"] += 1
            return original_source()

        self.wda.source = source
        result, client = self.run_agent([("TAP", "0"), ("TAP", "0")])
        self.assertEqual(result["status"], "no_progress")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(len([path for path in self.paths() if path.startswith("/source")]), 4)
        self.assertEqual(sum(path == "/element/target/click" for path in self.paths()), 1)

    def test_literal_text_is_matched_locally_and_never_generated(self):
        original_source = self.wda.source

        def source():
            self.wda.nodes[0]["value"] = self.wda.elements[0]["value"]
            return original_source()

        self.wda.source = source
        result, client = self.run_agent([("TYPE_TEXT", "0"), ("DONE", None)],
            texts=[{"selector": {"label": "Target", "type": "TextField"}, "text": "完全按原文输入"}])
        self.assertEqual(result["status"], "done_unverified")
        self.assertEqual(self.wda.elements[0]["value"], "完全按原文输入")
        self.assertTrue(result["results"][0]["verified"])
        self.assertFalse(result["results"][0]["submitted"])
        self.assertEqual(client.requests[0]["state"]["provided_text_fields"], [{"label": "Target", "type": "TextField"}])
        self.assertNotIn("完全按原文输入", str(client.requests[0]))

    def test_missing_or_ambiguous_literal_text_stops_before_focus(self):
        for texts in ([], [{"selector": {"label": "Other"}, "text": "x"}],
                      [{"selector": {"label": "Target"}, "text": "x"}, {"selector": {"type": "TextField"}, "text": "y"}]):
            with self.subTest(texts=texts):
                self.wda.calls.clear()
                result, _ = self.run_agent([("TYPE_TEXT", "0")], texts=texts)
                self.assertEqual(result["status"], "needs_text")
                self.assertEqual(self.mutations(), [])

    def test_duplicate_observed_selectors_are_not_offered(self):
        self.wda.nodes.append(node("Target", kind="TextField", y=350))
        result, client = self.run_agent([("BLOCKED", None)])
        self.assertEqual(result["status"], "needs_host")
        self.assertNotIn("tap_target", client.requests[0]["questions"])
        self.assertNotIn("TAP", client.requests[0]["questions"]["operation"]["criteria"])
        self.assertEqual(self.mutations(), [])

    def test_header_clock_and_containers_are_context_only(self):
        self.wda.nodes = [
            node("General", kind="NavigationBar", x=0, y=0, width=390, height=100),
            node("General", kind="StaticText"), node("12:34", kind="StaticText"),
            node("Status", kind="StatusBar"), node("Settings list", kind="ScrollView"),
            node("Wi-Fi", kind="Switch"), node("Background", kind="Other", x=0, y=0, width=390, height=844),
            node("Open General", kind="Cell", y=300), node("Search", kind="SearchField", y=350),
            node("Custom action", kind="Other", width=100, height=44),
        ]
        result, client = self.run_agent([("TAP", "7")], dry_run=True)
        criteria = client.requests[0]["questions"]["tap_target"]["criteria"]
        self.assertEqual(set(criteria), {"7", "8", "9"})
        self.assertEqual(result["decision"]["selector"], {"type": "Cell", "label": "Open General"})
        self.assertEqual(set(client.requests[0]["questions"]["type_text_target"]["criteria"]), {"8"})

    def test_secure_fields_are_not_sent_to_cloud(self):
        self.wda.nodes = [node("Password", kind="SecureTextField")]
        result, client = self.run_agent([])
        self.assertEqual((result["status"], result["reason"]), ("needs_auth", "secure_field"))
        self.assertEqual(client.requests, [])
        self.assertEqual(self.mutations(), [])

    def test_authentication_alert_is_handed_back(self):
        self.wda.nodes = [node("输入密码以验证身份", kind="Alert")]
        result, client = self.run_agent([])
        self.assertEqual(result["status"], "needs_auth")
        self.assertEqual(client.requests, [])

    def test_springboard_notifications_and_appswitcher_are_not_sent_to_cloud(self):
        for overlay in ("NotificationShortLookView", "AppSwitcherContentView"):
            with self.subTest(overlay=overlay):
                self.wda.app = "com.apple.springboard"
                self.wda.source_root["bundleId"] = self.wda.app
                self.wda.nodes = [node("Private incoming message", kind="Other", name=overlay)]
                result, client = self.run_agent([])
                self.assertEqual((result["status"], result["reason"]), ("needs_host", "system_overlay"))
                self.assertEqual(client.requests, [])
                self.assertEqual(self.mutations(), [])

    def test_foreground_switch_after_action_is_not_sent_to_cloud(self):
        original_session = self.wda.session

        def session(method, path, payload=None, timeout=None):
            result = original_session(method, path, payload, timeout)
            if path == "/element/target/click":
                self.wda.app = "com.example.other"
                self.wda.source_root["bundleId"] = self.wda.app
                self.wda.nodes = [node("Private other app", kind="StaticText")]
            return result

        self.wda.session = session
        result, client = self.run_agent([("TAP", "0"), ("DONE", None)])
        self.assertEqual((result["status"], result["reason"]), ("needs_host", "foreground_changed"))
        self.assertEqual(len(client.requests), 1)

    def test_post_action_delay_precedes_tree_and_is_counted_as_action_time(self):
        self.transition_on_click()
        sleeps = []
        original_sleep = time.sleep

        def sleep(seconds):
            sleeps.append(seconds)
            return original_sleep(seconds)

        with patch("jev_agent.time.sleep", side_effect=sleep):
            result, _ = self.run_agent([("TAP", "0"), ("DONE", None)])
        self.assertEqual(sleeps, [0.25])
        self.assertGreaterEqual(result["metrics"]["action_ms"], 240)

    def test_irreversible_target_is_returned_as_a_host_decision(self):
        self.wda.nodes = [node("发送", kind="Button")]
        result, _ = self.run_agent([("TAP", "0")])
        self.assertEqual(result["reason"], "irreversible_action")
        self.assertEqual(result["decision"]["selector"], {"type": "Button", "label": "发送"})
        self.assertFalse(result["complete"])
        self.assertEqual(self.mutations(), [])

    def test_uncertain_mutation_is_executed_once_and_never_replayed(self):
        self.wda.click_error = WDAError("action_uncertain", "Native action timed out", uncertain=True)
        result, client = self.run_agent([("TAP", "0"), ("TAP", "0")])
        self.assertEqual(result["status"], "uncertain")
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error"]["code"], "action_uncertain")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(sum(path == "/element/target/click" for path in self.paths()), 1)
        self.assertIsNone(result["trace"][0]["action_executed"])
        self.assertEqual(self.runtime.phone.settle_seconds, 0.8)
        self.assertIsNone(self.runtime.phone._deadline)

    def test_unchanged_page_stops_before_another_decision_or_click(self):
        result, client = self.run_agent([("TAP", "0"), ("TAP", "0")])
        self.assertEqual(result["status"], "no_progress")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result["steps"], 1)
        self.assertEqual(sum(path == "/element/target/click" for path in self.paths()), 1)
        self.assertIn("image", result["observation"])
        self.assertEqual(sum(path == "/screenshot" for path in self.paths()), 1)
        self.assertNotIn("image", client.requests[0]["state"])

    def test_no_progress_capture_failure_preserves_action_and_status(self):
        original_request = self.wda.request

        def request(method, path, payload=None, timeout=None):
            if path == "/screenshot":
                raise WDAError("pua_unreachable", "Screenshot capture unavailable")
            return original_request(method, path, payload, timeout)

        self.wda.request = request
        result, client = self.run_agent([("TAP", "0")])
        self.assertEqual(result["status"], "no_progress")
        self.assertEqual(result["observation_error"]["code"], "pua_unreachable")
        self.assertTrue(result["results"][0]["action_executed"])
        self.assertEqual(len(client.requests), 1)

    def test_native_hittability_failure_returns_visual_fallback_and_stops(self):
        self.wda.elements[0]["hittable"] = False
        result, client = self.run_agent([("TAP", "0"), ("TAP", "0")])
        self.assertEqual(result["error"]["code"], "occluded_target")
        self.assertTrue(result["error"]["recovery"]["visual_check_required"])
        self.assertIn("image", result["observation"])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.mutations(), [])

    def test_changed_foreground_is_rejected_before_mutation(self):
        self.wda.foreground_sequence = ["com.example.other"]
        result, _ = self.run_agent([("TAP", "0")])
        self.assertEqual(result["error"]["code"], "stale_observation")
        self.assertEqual(self.mutations(), [])

    def test_done_requires_explicit_expect_to_report_completion(self):
        result, client = self.run_agent([], expect={"label": "Target"})
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["complete"])
        self.assertTrue(result["verification"]["verified"])
        self.assertEqual(result["verification"]["source"], "fresh_observation")
        self.assertEqual(client.requests, [])
        self.assertEqual(self.mutations(), [])

    def test_expected_post_observation_skips_terminal_model_and_native_query(self):
        self.transition_on_click()
        result, client = self.run_agent([("TAP", "0")], expect={"label": "Next page"}, max_steps=1)
        self.assertTrue(result["complete"])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result["model"], "jev-test")
        self.assertEqual(result["trace"][0]["model"], "jev-test")
        self.assertEqual(result["verification"]["source"], "fresh_observation")
        self.assertEqual(sum(path == "/elements" for path in self.paths()), 1)

    def test_authentication_post_observation_prevents_terminal_expect_success(self):
        original_session = self.wda.session

        def session(method, path, payload=None, timeout=None):
            result = original_session(method, path, payload, timeout)
            if path == "/element/target/click":
                self.wda.nodes = [node("Next page", kind="StaticText"), node("输入密码", kind="Alert")]
            return result

        self.wda.session = session
        result, client = self.run_agent([("TAP", "0")], expect={"label": "Next page"})
        self.assertEqual(result["status"], "needs_auth")
        self.assertFalse(result["complete"])
        self.assertEqual(len(client.requests), 1)

    def test_dry_run_still_classifies_when_expected_control_is_already_present(self):
        result, client = self.run_agent([("TAP", "0")], expect={"label": "Target"}, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(client.requests), 1)

    def test_predicate_expect_uses_explicit_native_check(self):
        result, client = self.run_agent([("DONE", None)], expect={"predicate": "label == 'Target'"})
        self.assertTrue(result["complete"])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result["verification"]["criterion"], "selector exists; presence alone does not prove business success")

    def test_failed_terminal_expect_does_not_restart_steps(self):
        with patch.object(self.runtime.phone, "wait", side_effect=WDAError("postcondition_failed", "Expected page is absent")):
            result, client = self.run_agent([("DONE", None)], expect={"label": "Missing"})
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"]["code"], "postcondition_failed")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(self.mutations(), [])

    def test_partial_typing_returns_token_and_never_resends_text(self):
        self.runtime.phone.call_budget = 0
        result, client = self.run_agent([("TYPE_TEXT", "0"), ("TYPE_TEXT", "0")],
            texts=[{"selector": {"label": "Target"}, "text": "字" * 300}])
        self.assertEqual(result["status"], "input_continues")
        self.assertEqual(len(self.wda.elements[0]["value"]), 200)
        self.assertEqual(result["continue_token"], result["results"][0]["continue_token"])
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(sum(path == "/element/target/clear" for path in self.paths()), 1)

    def test_step_budget_returns_last_state_and_trace(self):
        self.transition_on_click()
        result, client = self.run_agent([("TAP", "0")], max_steps=1)
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(result["observation"]["nodes"][0]["label"], "Next page")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(result["steps"], 1)

    def test_model_that_consumes_budget_cannot_execute_its_late_decision(self):
        client = FakeJev([("TAP", "0")])
        original = client.classify

        def classify(*args, **kwargs):
            time.sleep(1.02)
            return original(*args, **kwargs)

        client.classify = classify
        result = JevAgent(self.runtime, client).run("Open the page", timeout_seconds=1)
        self.assertEqual(result["status"], "time_budget")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.runtime.phone.settle_seconds, 0.8)

    def test_model_errors_and_invalid_choices_stop_without_native_mutation(self):
        for decisions in ([WDAError("jev_invalid_response", "Jev returned invalid JSON")], [("ARBITRARY_CODE", None)]):
            with self.subTest(decisions=decisions):
                result, client = self.run_agent(decisions)
                self.assertEqual(result["status"], "needs_host")
                self.assertEqual(result["error"]["code"], "jev_invalid_response")
                self.assertEqual(len(client.requests), 1)
                self.assertEqual(self.mutations(), [])

    def test_empty_and_truncated_states_stop_before_model_request(self):
        for nodes in ([], [node(str(index), kind="StaticText") for index in range(201)]):
            with self.subTest(count=len(nodes)):
                self.wda.nodes = nodes
                result, client = self.run_agent([])
                self.assertEqual(result["status"], "needs_host")
                self.assertIn(result["reason"], ("empty_tree", "truncated_tree"))
                self.assertEqual(client.requests, [])

    def test_transport_deadlines_and_wrappers_are_restored(self):
        original_request, original_session, original_observe = self.wda.request, self.wda.session, self.runtime.phone.observe
        result, client = self.run_agent([("TAP", "0")], dry_run=True, timeout_seconds=2)
        self.assertEqual(self.wda.request, original_request)
        self.assertEqual(self.wda.session, original_session)
        self.assertEqual(self.runtime.phone.observe, original_observe)
        self.assertLessEqual(client.requests[0]["timeout"], 2)
        self.assertTrue(all(timeout <= 2 for _, _, timeout in self.wda.timeouts))
        self.assertGreaterEqual(result["metrics"]["observation_ms"], 0)

    def test_post_observation_keeps_same_200_node_cap_as_initial_state(self):
        self.transition_on_click()
        original_source = self.wda.source

        def source():
            if self.wda.nodes[0]["label"] == "Next page":
                self.wda.nodes = [node("Next page", kind="StaticText"), *[
                    node(f"Extra {index}", kind="StaticText", y=100 + index) for index in range(150)]]
            return original_source()

        self.wda.source = source
        result, client = self.run_agent([("TAP", "0"), ("DONE", None)])
        self.assertEqual(result["status"], "done_unverified")
        self.assertEqual(len(result["observation"]["nodes"]), 151)
        self.assertFalse(result["observation"]["truncated"])
        self.assertEqual(len(client.requests), 2)

    def test_accepted_action_before_failed_post_read_is_retained(self):
        original_source = self.wda.source

        def source():
            if any(path == "/element/target/click" for path in self.paths()):
                raise WDAError("pua_unreachable", "Post-action read failed")
            return original_source()

        self.wda.source = source
        result, client = self.run_agent([("TAP", "0"), ("TAP", "0")])
        self.assertEqual(result["steps"], 1)
        self.assertTrue(result["trace"][0]["action_executed"])
        self.assertFalse(result["trace"][0]["action_complete"])
        self.assertTrue(result["results"][0]["action_executed"])
        self.assertEqual(result["results"][0]["error"], "pua_unreachable")
        self.assertEqual(len(client.requests), 1)

    def test_late_done_decision_is_a_budget_stop(self):
        client = FakeJev([("DONE", None)])
        original = client.classify
        clock = {"now": 0}

        def classify(*args, **kwargs):
            result = original(*args, **kwargs)
            clock["now"] = 2
            return result

        client.classify = classify
        # Simulate a model consuming the outer deadline without a wall-clock wait.
        with patch("jev_agent.time.monotonic", side_effect=lambda: clock["now"]):
            result = JevAgent(self.runtime, client).run("Open the page", timeout_seconds=1)
        self.assertEqual(result["status"], "time_budget")
        self.assertEqual(len(client.requests), 1)

    def test_arguments_are_validated_before_observing(self):
        client = FakeJev([])
        agent = JevAgent(self.runtime, client)
        for arguments in ({"goal": ""}, {"goal": "x", "max_steps": True}, {"goal": "x", "timeout_seconds": float("nan")},
                          {"goal": "x", "texts": [{"selector": {"label": "Target"}, "text": "send\n"}]}):
            with self.subTest(arguments=arguments), self.assertRaises(WDAError):
                agent.run(**arguments)
        self.assertEqual(self.wda.calls, [])
        self.assertEqual(client.requests, [])


if __name__ == "__main__":
    unittest.main()
