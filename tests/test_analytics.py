import concurrent.futures
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
from analytics import Analytics, anonymous_id, project_token, safe_properties, send_batch
from iphone_use import Runtime, VERSION
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
        analytics = Analytics(self.directory, VERSION, sender or collect, token=token)
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
                          app_version="private-override", session_id="private-session", environment="production")
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
        self.assertEqual(safe_properties({"duration_ms": -10, "batch_size": True}), {"duration_ms": 0})

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
        self.assertEqual(sum(e["event"] == "iphone_use_session_started" for e in self.events), 1)
        tools = [e["properties"] for e in self.events if e["event"] == "iphone_use_tool_called"]
        self.assertEqual([e["outcome"] for e in tools], ["success", "pending", "error", "pending", "error", "success"])
        self.assertEqual(tools[2]["error_code"], "other")
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

    def test_midscene_telemetry_is_bounded_and_records_failures(self):
        analytics = self.analytics()
        for code in (None, 'midscene_node_unsupported', 'midscene_failed', 'phone_locked', 'wait_timeout'):
            analytics.tool_result('pua_midscene', {'text': 'private instruction'},
                                  {'error': code, 'report': '/private/file'}, 42)
        analytics.close()
        events = [e['properties'] for e in self.events if e['event'] == 'iphone_use_tool_called']
        self.assertEqual(len(events), 5)
        self.assertEqual([e['error_code'] for e in events], ['none', 'midscene_node_unsupported', 'midscene_failed', 'phone_locked', 'wait_timeout'])
        self.assertNotIn('private', json.dumps(events))

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


if __name__ == "__main__":
    unittest.main()
