import concurrent.futures
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from analytics import Analytics, anonymous_id, project_token, safe_properties, send_batch, ga4_config, ga4_payload, send_ga4
from iphone_use import Runtime, VERSION, serve
from wda_client import WDAError


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        patcher = patch.dict(os.environ, {"IPHONE_USE_ANALYTICS": "1", "DO_NOT_TRACK": "0",
                                         "IPHONE_USE_ANALYTICS_ENV": "test"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events = []

    def analytics(self, sender=None, token="test-public-token"):
        def collect(token, events):
            self.events.extend(events)
            return 200
        analytics = Analytics(self.directory, VERSION, sender or collect, token=token, ga4={})
        self.addCleanup(analytics.close)
        return analytics

    def test_random_identity_survives_processes_and_concurrency(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda _: anonymous_id(self.directory), range(20)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(anonymous_id(self.directory), ids[0])
        self.assertEqual((self.directory / "analytics-id").stat().st_mode & 0o777, 0o600)
        (self.directory / "analytics-id").write_text("private-device-id")
        self.assertNotEqual(anonymous_id(self.directory), "private-device-id")

    def test_opt_out_and_missing_token_do_not_create_identity_or_worker(self):
        for setting in ({"IPHONE_USE_ANALYTICS": "0"}, {"DO_NOT_TRACK": "1"},
                        {"IPHONE_USE_ANALYTICS": "false"}):
            with patch.dict(os.environ, setting):
                analytics = self.analytics()
                self.assertFalse(analytics.capture("iphone_use_session_started"))
                self.assertIsNone(analytics.worker)
                self.assertFalse((self.directory / "analytics-id").exists())
        analytics = self.analytics(token="")
        self.assertFalse(analytics.capture("iphone_use_session_started"))
        self.assertIsNone(analytics.worker)

    def test_payload_filters_free_text_and_bounds_numbers(self):
        analytics = self.analytics()
        analytics.capture("iphone_use_tool_called", tool_name="pua_type_text", outcome="success",
                          duration_ms=float("nan"), batch_size=9999,
                          text="private-input", selector={"label": "private-label"}, udid="device-id",
                          screenshot="pixels", error_code="private-error", distinct_id="email",
                          app_version="private-override", session_id="private-session", environment="production",
                          analytics_schema_version=999)
        self.assertFalse(analytics.capture("private-event"))
        analytics.close()
        self.assertEqual(len(self.events), 1)
        event = self.events[0]
        wire = json.dumps(event)
        self.assertNotIn("private", wire)
        self.assertNotIn("device-id", wire)
        self.assertNotIn("email", wire)
        self.assertNotIn("duration_ms", event["properties"])
        self.assertEqual(event["properties"]["batch_size"], 20)
        self.assertFalse(event["properties"]["$process_person_profile"])
        self.assertTrue(event["properties"]["$geoip_disable"])
        self.assertEqual(event["properties"]["environment"], "test")
        self.assertEqual(event["properties"]["analytics_schema_version"], 2)
        self.assertEqual(safe_properties({"duration_ms": -10, "batch_size": True}), {"duration_ms": 0})

    def test_source_names_the_host_and_defaults_to_codex(self):
        for host, expected in ((None, "codex_plugin_mcp"), ("claude", "claude_plugin_mcp"), ("private-host", "codex_plugin_mcp")):
            self.events.clear()
            environment = {"IPHONE_USE_HOST": host} if host else {}
            with patch.dict(os.environ, environment):
                if not host:
                    os.environ.pop("IPHONE_USE_HOST", None)
                analytics = self.analytics()
                analytics.capture("iphone_use_session_started")
                analytics.close()
            self.assertEqual(self.events[0]["properties"]["source"], expected, host)

    def test_retries_share_event_uuid_and_nonretryable_errors_stop(self):
        deliveries = []
        def transient(token, batch):
            deliveries.append(batch)
            return 503 if len(deliveries) == 1 else 200
        analytics = self.analytics(transient)
        analytics.capture("iphone_use_session_started")
        analytics.close()
        self.assertEqual(len(deliveries), 2)
        self.assertEqual(deliveries[0], deliveries[1])
        sender = Mock(return_value=400)
        analytics = self.analytics(sender)
        analytics.capture("iphone_use_session_started")
        analytics.close()
        sender.assert_called_once()

    def test_tool_outcomes_and_preview_poll_exclusion(self):
        analytics = self.analytics()
        analytics.tool_result("pua_screen_frame", {}, {}, 1)
        self.assertIsNone(analytics.worker)
        analytics.tool_result("pua_ready", {}, {"ready": True, "udid": "private"}, 12)
        analytics.tool_result("pua_ready", {}, {"ready": False, "state": "recovering"}, 2)
        analytics.tool_result("pua_tap", {"selector": {"label": "private"}}, None, 3, "private-message")
        analytics.tool_result("pua_batch", {"steps": [{}] * 2}, {"complete": False}, 5)
        analytics.tool_result("pua_setup", {"action": "build", "team_id": "private"}, {"ok": False, "error": "private"}, 3)
        analytics.tool_result("pua_screen", {"action": "pause"}, {}, 3)
        analytics.close()
        self.assertEqual(len(self.events), 7)  # One usage start and exactly six calls.
        self.assertEqual(sum(e["event"] == "iphone_use_session_started" for e in self.events), 1)
        tools = [e["properties"] for e in self.events if e["event"] == "iphone_use_tool_called"]
        self.assertEqual([e["outcome"] for e in tools], ["success", "pending", "error", "pending", "error", "success"])
        self.assertEqual(tools[2]["error_code"], "other")
        self.assertEqual([tools[0]["ready_state"], tools[1]["ready_state"]], ["ready", "recovering"])
        self.assertEqual((tools[4]["setup_action"], tools[4]["setup_state"]), ("build", "error"))
        self.assertEqual(tools[5]["screen_action"], "pause")
        self.assertNotIn("private", json.dumps(self.events))

    def test_idle_mcp_handshakes_create_no_events_or_identity(self):
        # Exercise the actual MCP handshake/catalog path across independent runtimes.
        for _ in range(3):
            runtime = Runtime(self.directory)
            runtime.analytics = self.analytics()
            requests = [{"jsonrpc": "2.0", "id": i, "method": method, "params": {}}
                        for i, method in enumerate(("initialize", "initialize", "ping", "tools/list", "resources/list"))]
            output = io.StringIO()
            with patch("sys.stdin", io.StringIO("".join(json.dumps(r) + "\n" for r in requests))), contextlib.redirect_stdout(output):
                serve(runtime)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(len(replies), len(requests))
            self.assertTrue(all("result" in reply for reply in replies))
            self.assertIsNone(runtime.analytics.worker)
            runtime.close()
        self.assertEqual(self.events, [])
        self.assertFalse((self.directory / "analytics-id").exists())

    def test_viewport_polling_never_starts_usage_or_emits_events(self):
        runtime = Runtime(self.directory)
        runtime.analytics = self.analytics()
        self.addCleanup(runtime.close)
        with patch.object(runtime, "_dispatch", return_value={"viewport": {"width": 390, "height": 844}}):
            for _ in range(1000):
                runtime.call("pua_screen_action", {"action": "viewport"})
        with patch.object(runtime, "_dispatch", side_effect=WDAError("pua_unreachable", "private")):
            with self.assertRaises(WDAError):
                runtime.call("pua_screen_action", {"action": "viewport"})
        self.assertIsNone(runtime.analytics.worker)
        self.assertFalse((self.directory / "analytics-id").exists())
        # A real call starts usage exactly once, including a failed first call.
        with patch.object(runtime, "_dispatch", side_effect=WDAError("not_ready", "private")):
            with self.assertRaises(WDAError):
                runtime.call("pua_ready", {})
        with patch.object(runtime, "_dispatch", return_value={"ok": True}):
            for action in ("tap", "drag"):
                runtime.call("pua_screen_action", {"action": action})
        runtime.analytics.close()
        self.assertEqual([e["event"] for e in self.events], ["iphone_use_session_started"] + ["iphone_use_tool_called"] * 3)
        self.assertEqual(self.events[1]["properties"]["ready_state"], "error")
        self.assertEqual([e["properties"]["screen_action"] for e in self.events[2:]], ["tap", "drag"])

    def test_transport_error_categories_are_preserved_without_user_content(self):
        analytics = self.analytics()
        for code in ("pua_timeout", "pua_transport_error", "pua_unreachable"):
            analytics.tool_result("pua_observe", {"selector": {"label": "private"}}, None, 3, code)
        analytics.close()
        errors = [event["properties"]["error_code"] for event in self.events if event["event"] == "iphone_use_tool_called"]
        self.assertEqual(errors, ["pua_timeout", "pua_transport_error", "pua_unreachable"])
        self.assertNotIn("private", json.dumps(self.events))

    def test_runtime_and_delivery_failures_preserve_phone_results(self):
        runtime = Runtime(self.directory)
        runtime.analytics = self.analytics(Mock(side_effect=OSError("offline")))
        self.addCleanup(runtime.close)
        value = {"input_complete": True, "secret": "private"}
        with patch.object(runtime, "_dispatch", return_value=value):
            self.assertIs(runtime.call("pua_type_text", {"text": "private"}), value)
        with patch.object(runtime, "_dispatch", side_effect=WDAError("phone_locked", "private")):
            with self.assertRaises(WDAError):
                runtime.call("pua_tap", {})
        # A telemetry bug must also leave successful operations unchanged.
        with patch.object(runtime, "_dispatch", return_value=value), patch.object(runtime.analytics, "tool_result", side_effect=RuntimeError):
            self.assertIs(runtime.call("pua_type_text", {}), value)

    def test_slow_network_and_full_queue_do_not_block_tools_or_close(self):
        waiting, release = threading.Event(), threading.Event()
        def slow(token, events):
            waiting.set()
            release.wait(3)
            return 200
        analytics = self.analytics(slow)
        analytics.capture("iphone_use_session_started")
        self.assertTrue(waiting.wait(1))
        start = time.monotonic()
        for _ in range(400):
            analytics.tool_result("pua_tap", {}, {}, 5)
        self.assertLess(time.monotonic() - start, 1)
        self.assertLessEqual(analytics.queue.qsize(), 256)
        start = time.monotonic()
        analytics.close(timeout=0.01)
        self.assertLess(time.monotonic() - start, 0.1)
        release.set()
        analytics.close()

    def test_token_priority_and_standard_library_batch_request(self):
        with patch.dict(os.environ, {"IPHONE_USE_POSTHOG_PROJECT_TOKEN": " override "}):
            self.assertEqual(project_token(), "override")
        with patch.dict(os.environ, {"IPHONE_USE_POSTHOG_PROJECT_TOKEN": ""}):
            self.assertEqual(project_token(), "")
        connection = Mock()
        connection.getresponse.return_value.status = 200
        with patch("analytics.http.client.HTTPSConnection", return_value=connection):
            self.assertEqual(send_batch("public-token", [{"event": "example"}]), 200)
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/batch/"))
        body = json.loads(call.kwargs["body"])
        self.assertEqual(body["api_key"], "public-token")
        self.assertEqual(body["batch"], [{"event": "example"}])
        connection.close.assert_called_once()

    def test_ga4_payload_matches_usage_and_excludes_private_fields(self):
        analytics = self.analytics()
        analytics.tool_result("pua_ready", {}, {"ready": True}, 12)
        analytics.tool_result("pua_tap", {"selector": {"label": "private"}}, {}, 42)
        analytics.close()
        payload = ga4_payload(self.events)
        self.assertRegex(payload["client_id"], r"^\d+\.\d+$")
        self.assertEqual(payload["consent"]["ad_personalization"], "DENIED")
        self.assertEqual(len(payload["events"]), 3)
        self.assertEqual([e["params"]["active_use"] for e in payload["events"]], ["no", "yes", "yes"])
        sessions = {e["params"]["session_id"] for e in payload["events"]}
        self.assertEqual(len(sessions), 1)
        self.assertTrue(0 < next(iter(sessions)) <= 2**53 - 1)
        self.assertNotIn("private", json.dumps(payload))
        for event in payload["events"]:
            self.assertLessEqual(len(event["params"]), 25)
            self.assertNotIn("debug_mode", event["params"])
            self.assertNotIn("$process_person_profile", event["params"])
            self.assertGreater(event["params"]["engagement_time_msec"], 0)
        self.assertEqual(ga4_payload(self.events)["client_id"], payload["client_id"])

    def test_launch_targets_are_sanitized_and_names_come_from_public_catalog(self):
        analytics = self.analytics()
        analytics.tool_result("pua_launch_app", {"bundle_id": "com.tencent.xin", "text": "private"},
                              {"foreground_verified": True, "tree": "private"}, 12)
        analytics.tool_result("pua_launch_app", {"bundle_id": "com.example.Unlisted"},
                              {"foreground_verified": False}, 13)
        analytics.tool_result("pua_launch_app", {"bundle_id": "com.apple.Preferences"}, None, 14, "http_error")
        analytics.close()
        launches = [e for e in self.events if e["event"] == "iphone_use_app_launched"]
        self.assertEqual(len(launches), 2)
        self.assertEqual(launches[0]["properties"]["target_app_name"], "微信")
        self.assertEqual(launches[0]["properties"]["foreground_verified"], "yes")
        self.assertEqual(launches[1]["properties"]["target_app_name"], "com.example.Unlisted")
        self.assertEqual(launches[1]["properties"]["foreground_verified"], "no")
        self.assertEqual({e["properties"]["launch_source"] for e in launches}, {"direct"})
        self.assertNotIn("private", json.dumps(self.events))
        for invalid in (None, 12, "private text", "com.example\n", "com." + "x" * 100):
            self.assertEqual(safe_properties({"target_app_bundle_id": invalid,
                                             "target_app_name": "private"}), {})
        clean = safe_properties({"target_app_bundle_id": "com.tencent.xin", "target_app_name": "private"})
        self.assertEqual(clean["target_app_name"], "微信")
        payload = ga4_payload(self.events)
        self.assertEqual(len(payload["events"]), len(self.events))
        for event in payload["events"]:
            self.assertLessEqual(len(event["params"]), 25)
        self.assertEqual([e["params"]["active_use"] for e in payload["events"]
                          if e["name"] == "iphone_use_app_launched"], ["no", "no"])

    def test_batch_launches_count_only_returned_successful_steps(self):
        analytics = self.analytics()
        steps = [{"op": "launch_app", "args": {"bundle_id": "com.tencent.xin"}},
                 {"op": "tap", "args": {"text": "private"}},
                 {"op": "launch_app", "args": {"bundle_id": "com.tencent.xin"}},
                 {"op": "launch_app", "args": {"bundle_id": "com.example.unexecuted"}}]
        # Repeated launches are separate operations; a later failed launch is
        # absent from results and must never contribute to the distribution.
        analytics.tool_result("pua_batch", {"steps": steps},
                              {"results": [{"foreground_verified": True}, {}, {"foreground_verified": False}],
                               "completed_steps": 3, "complete": False, "error": {"code": "http_error"}}, 45)
        analytics.tool_result("pua_batch", {"steps": steps}, None, 1, "invalid_argument")
        analytics.close()
        launches = [e["properties"] for e in self.events if e["event"] == "iphone_use_app_launched"]
        self.assertEqual(len(launches), 2)
        self.assertEqual([e["foreground_verified"] for e in launches], ["yes", "no"])
        self.assertEqual({e["launch_source"] for e in launches}, {"batch"})
        self.assertNotIn("unexecuted", json.dumps(self.events))
        self.assertEqual(sum(e["event"] == "iphone_use_tool_called" for e in self.events), 2)

    def test_launch_event_delivery_to_both_providers_and_optout(self):
        ga4_events = []
        analytics = Analytics(self.directory, VERSION, sender=lambda token, events: (self.events.extend(events) or 200),
                              token="test", ga4={"measurementId": "G-TEST", "apiSecret": "test"},
                              ga4_sender=lambda config, events: (ga4_events.extend(events) or 204))
        self.addCleanup(analytics.close)
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            analytics.tool_result("pua_launch_app", {"bundle_id": "com.tencent.xin"}, {}, 1)
        self.assertFalse(self.events)
        self.assertFalse(ga4_events)
        self.assertFalse((self.directory / "analytics-id").exists())
        analytics.tool_result("pua_launch_app", {"bundle_id": "com.tencent.xin"}, {}, 1)
        analytics.close()
        self.assertEqual([e["uuid"] for e in self.events], [e["uuid"] for e in ga4_events])
        self.assertEqual(sum(e["event"] == "iphone_use_app_launched" for e in ga4_events), 1)

    def test_ga4_independent_delivery_has_no_retries_or_idle_events(self):
        release, waiting = threading.Event(), threading.Event()
        def posthog(token, events):
            waiting.set()
            release.wait(2)
            return 503
        ga4_events = []
        def ga4(config, events):
            ga4_events.extend(events)
            return 503
        analytics = Analytics(self.directory, VERSION, sender=posthog, token="test",
                              ga4={"measurementId": "G-TEST", "apiSecret": "test"}, ga4_sender=ga4)
        self.addCleanup(analytics.close)
        analytics.tool_result("pua_screen_frame", {}, {}, 1)
        analytics.tool_result("pua_screen_action", {"action": "viewport"}, {}, 1)
        self.assertIsNone(analytics.ga4_worker)
        analytics.tool_result("pua_ready", {}, {"ready": True}, 10)
        self.assertTrue(waiting.wait(1))
        analytics.ga4_queue.join()
        self.assertEqual([e["event"] for e in ga4_events],
                         ["iphone_use_session_started", "iphone_use_tool_called"])
        release.set()
        analytics.close()

    def test_ga4_works_without_posthog_and_respects_global_optout(self):
        sender = Mock(return_value=204)
        analytics = Analytics(self.directory, VERSION, token="", ga4={"measurementId": "G-TEST", "apiSecret": "test"}, ga4_sender=sender)
        self.addCleanup(analytics.close)
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            analytics.tool_result("pua_ready", {}, {"ready": True}, 1)
        self.assertIsNone(analytics.ga4_worker)
        self.assertFalse((self.directory / "analytics-id").exists())
        analytics.capture("iphone_use_tool_called", tool_name="pua_ready", outcome="success")
        analytics.close()
        sender.assert_called_once()
        self.assertIsNone(analytics.worker)

    def test_ga4_config_overrides_and_https_request(self):
        with patch.dict(os.environ, {"IPHONE_USE_GA4_MEASUREMENT_ID": "G-TEST", "IPHONE_USE_GA4_API_SECRET": " test "}):
            self.assertEqual(ga4_config(), {"measurementId": "G-TEST", "apiSecret": "test"})
        with patch.dict(os.environ, {"IPHONE_USE_GA4_API_SECRET": ""}):
            self.assertEqual(ga4_config(), {})
        analytics = self.analytics()
        analytics.tool_result("pua_ready", {}, {"ready": True}, 1)
        analytics.close()
        connection = Mock()
        connection.getresponse.return_value.status = 204
        with patch("analytics.http.client.HTTPSConnection", return_value=connection) as factory:
            self.assertEqual(send_ga4({"measurementId": "G-TEST", "apiSecret": "test secret"}, self.events), 204)
        factory.assert_called_once_with("www.google-analytics.com", timeout=3)
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/mp/collect?measurement_id=G-TEST&api_secret=test+secret"))
        self.assertEqual(json.loads(call.kwargs["body"]), ga4_payload(self.events))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
