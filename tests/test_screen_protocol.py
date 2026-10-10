import contextlib
import fcntl
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from iphone_use import Runtime, SCREEN_URI, serve
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


class MockScreen:
    """Memory-only preview double: never captures or connects to a phone."""
    def __init__(self):
        self.events = []
        self.is_paused = False
        self.pause_reason = None
        self.device = None
        self.closed = False
        self.frame_result = {"frame": {"seq": 7, "data": "preview-only",
                                      "mimeType": "image/jpeg", "width": 390, "height": 844},
                             "events": [], "busy": False, "paused": False}

    def frame(self, **args):
        self.events.append(("frame", args))
        return self.frame_result

    def start(self):
        self.events.append(("start", {}))
        return {"frame": None, "events": [], "busy": False, "paused": self.is_paused}

    def set_paused(self, paused, reason="authentication"):
        self.is_paused = paused
        self.pause_reason = reason if paused else None
        self.events.append(("pause", {"paused": paused}))

    def pause_status(self):
        return {"paused": self.is_paused, "pause_reason": self.pause_reason}

    def locked_pause_id(self):
        return "test-lock" if self.is_paused and self.pause_reason == "device_locked" else None

    def reconnect_pause_id(self):
        return "test-pause" if self.is_paused else None

    def resume_after_unlock(self, expected_id, explicit=False):
        if expected_id and (self.locked_pause_id() == expected_id or explicit):
            self.is_paused = False
            self.pause_reason = None
            return True
        return False

    def paused(self):
        return self.is_paused

    def restart(self):
        self.events.append(("restart", {}))
        return {"frame": None, "events": [], "busy": False, "paused": self.is_paused, "stream_id": "restarted"}

    def set_viewport(self, viewport):
        self.events.append(("viewport", dict(viewport)))

    def begin(self, op):
        self.events.append(("begin", {"op": op}))
        return "activity-token"

    def end(self, token):
        self.events.append(("end", {"token": token}))

    def gesture(self, kind, **args):
        self.events.append((kind, args))

    def close(self):
        self.closed = True


class ScreenProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def runtime(self):
        runtime = Runtime(self.directory.name)
        # Close the unused real preview before replacing it; no frame poll occurs.
        runtime.screen.close()
        client = FakeWDA()
        client.close = Mock()
        runtime.client = client
        runtime.phone = PhoneController(client, self.directory.name)
        screen = MockScreen()
        runtime.screen = screen
        runtime.phone.screen = screen
        self.addCleanup(runtime.close)
        return runtime, client, screen

    def exchange(self, requests):
        payload = "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n"
                          for item in requests)
        result = subprocess.run([sys.executable, str(ROOT / "server/iphone_use.py"),
                                 "--state-dir", self.directory.name,
                                 "--url", "http://127.0.0.1:1"],
                                input=payload, capture_output=True, text=True,
                                encoding="utf-8", timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return [json.loads(line) for line in result.stdout.splitlines()]

    def call_over_stdio(self, runtime, name, arguments):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(runtime)
        return json.loads(output.getvalue())["result"]

    def test_catalog_opens_ready_widget_and_keeps_frame_poll_app_only(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertIn("resources", responses[0]["result"]["capabilities"])
        tools = {item["name"]: item for item in responses[1]["result"]["tools"]}
        self.assertEqual(len(tools), 20)
        for name in ("pua_ready", "pua_screen"):
            self.assertEqual(tools[name]["_meta"]["ui"]["resourceUri"], SCREEN_URI)
        self.assertEqual(tools["pua_screen"]["_meta"]["openai/ui"]["entrypoints"], [{"type": "thread"}])
        for name in ("pua_screen_frame", "pua_screen_action"):
            self.assertEqual(tools[name]["_meta"]["ui"]["visibility"], ["app"])
        self.assertEqual(tools["pua_screen_action"]["inputSchema"]["properties"]["action"]["enum"], ["refresh", "home", "screenshot"])
        self.assertFalse(tools["pua_screen_action"]["annotations"]["destructiveHint"])
        visible = [item for item in tools.values()
                   if item.get("_meta", {}).get("ui", {}).get("visibility") != ["app"]]
        self.assertEqual(len(visible), 18)
        self.assertTrue(tools["pua_screen_frame"]["annotations"]["readOnlyHint"])

    def test_stdio_serves_packaged_widget_without_connecting_to_wda(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
            {"jsonrpc": "2.0", "id": 2, "method": "resources/read", "params": {"uri": SCREEN_URI}},
        ])
        resource = responses[0]["result"]["resources"][0]
        self.assertEqual(resource["uri"], SCREEN_URI)
        content = responses[1]["result"]["contents"][0]
        self.assertEqual(content["uri"], SCREEN_URI)
        self.assertEqual(content["mimeType"], "text/html;profile=mcp-app")
        self.assertEqual(content["text"], (ROOT / "assets/phone-screen.html").read_text())
        self.assertFalse(content["_meta"]["ui"]["prefersBorder"])
        self.assertEqual(content["_meta"]["ui"]["csp"], {"connectDomains": [], "resourceDomains": []})
        self.assertEqual(content["_meta"]["openai/ui"]["availableDisplayModes"], ["fullscreen"])

    def test_invalid_resource_uri_cannot_read_files_and_following_ping_survives(self):
        invalid = ["file:///etc/passwd", "ui://iphone-use/../config.json", "", None, [], {}]
        requests = [{"jsonrpc": "2.0", "id": index, "method": "resources/read", "params": {"uri": uri}}
                    for index, uri in enumerate(invalid)]
        requests.append({"jsonrpc": "2.0", "id": "still-alive", "method": "ping"})
        responses = self.exchange(requests)
        for response in responses[:-1]:
            self.assertEqual(response["error"]["code"], -32602)
            self.assertNotIn("result", response)
        self.assertEqual(responses[-1], {"jsonrpc": "2.0", "id": "still-alive", "result": {}})

    def test_frame_poll_bypasses_phone_operation_lock_session_and_http(self):
        runtime, client, screen = self.runtime()
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = runtime.call("pua_screen_frame", {"after_seq": 6, "last_event_id": 2})
        self.assertIs(result, screen.frame_result)
        self.assertEqual(screen.events, [("frame", {"after_seq": 6, "last_event_id": 2})])
        self.assertEqual(client.calls, [])
        self.assertIsNone(client.session_id)
        self.assertFalse((Path(self.directory.name) / "session.json").exists())

    def test_frame_payload_is_structured_only_without_image_or_text_tokens(self):
        runtime, client, screen = self.runtime()
        result = self.call_over_stdio(runtime, "pua_screen_frame", {"after_seq": 0})
        self.assertEqual(result["content"], [])
        self.assertIs(result["isError"], False)
        self.assertEqual(result["structuredContent"], screen.frame_result)
        self.assertEqual(client.calls, [])

    def test_open_pause_resume_bypass_operation_lock_and_never_control_phone(self):
        runtime, client, screen = self.runtime()
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime.call("pua_screen", {})
            paused = runtime.call("pua_screen", {"action": "pause"})
            resumed = runtime.call("pua_screen", {"action": "resume"})
        self.assertTrue(paused["paused"])
        self.assertFalse(resumed["paused"])
        self.assertEqual(client.calls, [])
        self.assertEqual([kind for kind, _ in screen.events], ["start", "pause", "start", "pause", "start"])

    def test_ready_open_and_authentication_calls_share_one_host_widget_session(self):
        runtime, client, screen = self.runtime()
        results = []
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            for name, arguments in (("pua_ready", {"screenshot": False, "recover": False}),
                                    ("pua_screen", {}), ("pua_screen", {"action": "pause"}),
                                    ("pua_ready", {"screenshot": False, "recover": False}),
                                    ("pua_screen", {"action": "resume"})):
                results.append(self.call_over_stdio(runtime, name, arguments))
        identifiers = [result["_meta"]["openai/widgetSessionId"] for result in results]
        self.assertTrue(identifiers[0])
        self.assertEqual(len(set(identifiers)), 1)
        self.assertTrue(json.loads(results[0]["content"][0]["text"])["ready"])
        self.assertTrue(results[2]["structuredContent"]["paused"])
        self.assertTrue(json.loads(results[3]["content"][0]["text"])["ready"])
        self.assertFalse(results[4]["structuredContent"]["paused"])
        # Session identity is host metadata only: it must not duplicate model data
        # or couple the display lifecycle to a phone session or user action.
        for result in results:
            self.assertNotIn("widgetSessionId", result["content"][0]["text"])
        self.assertNotIn("structuredContent", results[0])
        self.assertFalse(any(path == "/screenshot" for _, path, _ in client.calls))
        self.assertEqual([path for _, path, _ in client.actions()], ["/session"])

    def test_widget_session_survives_repeated_opens_and_mcp_process_reconnect(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "pua_screen", "arguments": {}}}
        first, repeated = self.exchange([request, {**request, "id": 2}])
        reconnected, = self.exchange([request])
        results = [reply["result"] for reply in (first, repeated, reconnected)]
        identifiers = [result["_meta"]["openai/widgetSessionId"] for result in results]
        self.assertTrue(identifiers[0])
        self.assertEqual(len(set(identifiers)), 1)
        self.assertTrue(all(result["isError"] is False for result in results))

    def test_setup_status_and_failed_ready_do_not_create_a_widget_session(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "setup", return_value={"configured": True}):
            status = self.call_over_stdio(runtime, "pua_setup", {"action": "status"})
        client.locked = True
        failed = self.call_over_stdio(runtime, "pua_ready", {"screenshot": False, "recover": False})
        self.assertNotIn("_meta", status)
        self.assertNotIn("_meta", failed)
        self.assertTrue(failed["isError"])
        self.assertEqual(json.loads(failed["content"][0]["text"])["error"]["code"], "phone_locked")
        self.assertTrue(screen.is_paused)
        self.assertEqual(client.actions(), [])

    def test_invalid_preview_arguments_rejected_before_backend_or_phone(self):
        cases = [("pua_screen", {"action": "click"}), ("pua_screen", {"x": 100}),
                 ("pua_screen_frame", {"after_seq": True}), ("pua_screen_frame", {"after_seq": -1}),
                 ("pua_screen_frame", {"last_event_id": 1.5}), ("pua_screen_frame", {"observe": "tree"})]
        runtime, client, screen = self.runtime()
        for name, args in cases:
            with self.subTest(name=name, args=args), self.assertRaises(WDAError) as caught:
                runtime.call(name, args)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(screen.events, [])
        self.assertEqual(client.calls, [])

    def test_ready_opens_preview_by_default_without_a_frame_poll_or_added_screenshot(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = runtime.call("pua_ready", {"screenshot": False, "recover": False})
        self.assertTrue(result["ready"])
        self.assertEqual(sum(kind == "start" for kind, _ in screen.events), 1)
        self.assertFalse(any(kind == "frame" for kind, _ in screen.events))
        self.assertFalse(any(path == "/screenshot" for _, path, _ in client.calls))
        self.assertEqual(screen.events[0], ("begin", {"op": "ready"}))
        self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_coordinate_tap_emits_actual_point_before_single_action_without_extra_reads(self):
        runtime, client, screen = self.runtime()
        original = client.session
        at_action = []
        def request(method, path, payload=None, timeout=None):
            if path == "/wda/tap":
                at_action.extend(screen.events)
            return original(method, path, payload, timeout)
        client.session = request
        result = runtime.call("pua_tap", {"x": 120, "y": 240})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/tap"])
        self.assertEqual([event for event in at_action if event[0] == "tap"],
                         [("tap", {"point": {"x": 120, "y": 240}, "viewport": client.size})])
        self.assertEqual(screen.events[0], ("begin", {"op": "tap"}))
        self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_semantic_tap_cursor_uses_validated_rect_center(self):
        runtime, client, screen = self.runtime()
        result = runtime.call("pua_tap", {"selector": {"label": "Target"}})
        self.assertTrue(result["action_executed"])
        self.assertEqual([event for event in screen.events if event[0] == "tap"],
                         [("tap", {"point": {"x": 195, "y": 222}, "viewport": client.size})])
        self.assertEqual([path for _, path, _ in client.actions()], ["/element/target/click"])
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_swipe_cursor_tracks_the_executed_drag_endpoints(self):
        runtime, client, screen = self.runtime()
        result = runtime.call("pua_swipe", {"direction": "up", "region": {"x": 40, "y": 100, "width": 300, "height": 600}})
        self.assertTrue(result["action_executed"])
        action = next(body for method, path, body in client.calls if method == "POST" and path == "/wda/dragfromtoforduration")
        events = [args for kind, args in screen.events if kind == "drag"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_point"], {"x": action["fromX"], "y": action["fromY"]})
        self.assertEqual(events[0]["to_point"], {"x": action["toX"], "y": action["toY"]})
        self.assertEqual(events[0]["viewport"], client.size)
        self.assertEqual(client.swipe_count, 1)
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/dragfromtoforduration"])

    def test_preview_telemetry_never_contains_input_text_or_selector(self):
        runtime, client, screen = self.runtime()
        secret = "Long private text 绝不传给预览 " * 15
        client.elements[0]["label"] = "Private editor name"
        result = runtime.call("pua_type_text", {"selector": {"label": "Private editor name"}, "text": secret})
        self.assertTrue(result["action_executed"])
        encoded = json.dumps(screen.events, ensure_ascii=False)
        self.assertNotIn(secret, encoded)
        self.assertNotIn("Private editor name", encoded)
        self.assertNotIn("selector", encoded)
        self.assertEqual([kind for kind, _ in screen.events], ["begin", "viewport", "tap", "end"])
        self.assertEqual(client.elements[0]["value"], secret)

    def test_authentication_takeover_errors_pause_preview_without_input(self):
        for secure_field in (False, True):
            with self.subTest(secure_field=secure_field):
                runtime, client, screen = self.runtime()
                if secure_field:
                    client.elements[0]["kind"] = "XCUIElementTypeSecureTextField"
                    name, args, code = "pua_type_text", {"selector": {"label": "Target"}, "text": "never typed"}, "not_editable"
                else:
                    client.locked = True
                    name, args, code = "pua_tap", {"x": 100, "y": 220}, "phone_locked"
                with self.assertRaises(WDAError) as caught:
                    runtime.call(name, args)
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(screen.is_paused)
                self.assertEqual(client.actions(), [])
                self.assertEqual(screen.events[-1], ("end", {"token": "activity-token"}))

    def test_preview_telemetry_failure_cannot_change_or_repeat_a_phone_action(self):
        runtime, client, screen = self.runtime()
        for method in ("begin", "end", "gesture", "set_viewport"):
            setattr(screen, method, Mock(side_effect=OSError("display unavailable")))
        result = runtime.call("pua_tap", {"x": 120, "y": 240})
        self.assertTrue(result["action_executed"])
        self.assertEqual([path for _, path, _ in client.calls], ["/wda/locked", "/window/size", "/wda/tap"])
        self.assertEqual([path for _, path, _ in client.actions()], ["/wda/tap"])

    def test_ready_preview_failure_cannot_break_channel_readiness(self):
        runtime, client, screen = self.runtime()
        screen.start=Mock(side_effect=OSError("display unavailable"))
        with patch.object(runtime.setup_manager,"mirroring_running",return_value=False):
            self.assertTrue(runtime.call("pua_ready",{"screenshot":False,"recover":False})["ready"])

    def test_nonsecure_noneditable_target_does_not_pause_the_preview(self):
        runtime, client, screen = self.runtime()
        client.elements[0]["kind"]="XCUIElementTypeButton"
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_type_text",{"selector":{"label":"Target"},"text":"never typed"})
        self.assertEqual(caught.exception.code,"not_editable")
        self.assertFalse(screen.is_paused)
        self.assertEqual(client.actions(),[])

    def toolbar(self, runtime):
        """Route the toolbar's own short-lived connection to a synthetic phone."""
        phone = FakeWDA()
        phone.close = Mock()
        patcher = patch("iphone_use.WDAClient", return_value=phone)
        patcher.start()
        self.addCleanup(patcher.stop)
        return phone

    def test_toolbar_home_is_one_sessionless_request_under_the_operation_lock(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        runtime.phone.pending_input = {"token": "t"}
        before = runtime.phone.accepted_actions
        result = runtime.call("pua_screen_action", {"action": "home"})
        self.assertEqual(result, {"ok": True, "action": "home"})
        self.assertEqual(phone.calls, [("POST", "/wda/homescreen", {})])
        phone.close.assert_called_once_with()
        # The model's own connection and session are untouched; its stale focus is forgotten.
        self.assertEqual(client.calls, [])
        self.assertIsNone(client.session_id)
        self.assertEqual(runtime.phone.accepted_actions, before + 1)
        self.assertIsNone(runtime.phone.pending_input)

    def test_toolbar_home_reports_busy_instead_of_interleaving_with_a_running_tool(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        with (Path(self.directory.name) / "operation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_screen_action", {"action": "home"})
            # Refreshing the preview never needs the phone lock.
            self.assertTrue(runtime.call("pua_screen_action", {"action": "refresh"})["ok"])
        self.assertEqual(caught.exception.code, "device_busy")
        self.assertFalse(caught.exception.details["action_executed"])
        self.assertEqual(phone.actions(), [])

    def test_toolbar_refresh_restarts_only_the_preview_and_reports_service_state(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        result = runtime.call("pua_screen_action", {"action": "refresh"})
        self.assertEqual((result["ok"], result["service_ready"], result["stream_id"]), (True, True, "restarted"))
        self.assertEqual([kind for kind, _ in screen.events], ["restart"])
        self.assertEqual(phone.calls, [("GET", "/status", None), ("GET", "/wda/locked", None)])
        self.assertEqual(client.calls, [])
        phone.status_ready = False
        self.assertFalse(runtime.call("pua_screen_action", {"action": "refresh"})["service_ready"])

    def test_toolbar_screenshot_copies_the_native_capture_and_leaves_no_file(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        copied = []

        def run(command, **options):
            copied.append((command, Path(command[-1]).read_bytes(), Path(command[-1]).stat().st_mode & 0o777))
            return Mock(returncode=0)

        with patch("iphone_use.subprocess.run", side_effect=run):
            result = runtime.call("pua_screen_action", {"action": "screenshot"})
        self.assertEqual(result, {"ok": True, "action": "screenshot", "copied": True, "width": 1, "height": 1})
        command, data, mode = copied[0]
        self.assertEqual((data, mode), (phone.screenshot, 0o600))
        self.assertEqual(command[0], "/usr/bin/osascript")
        # The file path is an argument of the script, not part of its text.
        self.assertFalse(any(self.directory.name in part for part in command[:-1]))
        self.assertFalse(Path(command[-1]).exists())
        self.assertEqual(phone.calls, [("GET", "/screenshot", None)])
        self.assertFalse(list((Path(self.directory.name)).glob("**/*.png")))
        with patch("iphone_use.subprocess.run", return_value=Mock(returncode=1)), self.assertRaises(WDAError) as caught:
            runtime.call("pua_screen_action", {"action": "screenshot"})
        self.assertEqual(caught.exception.code, "clipboard_unavailable")
        self.assertFalse(list((Path(self.directory.name)).glob(".clipboard-*")))

    def test_toolbar_blocks_actions_until_user_explicitly_reconnects(self):
        runtime, client, screen = self.runtime()
        phone = self.toolbar(runtime)
        screen.is_paused = True
        for action in ("home", "screenshot"):
            with self.subTest(action=action), self.assertRaises(WDAError) as caught:
                runtime.call("pua_screen_action", {"action": action})
            self.assertEqual(caught.exception.code, "preview_paused")
        self.assertEqual(phone.calls, [])
        self.assertFalse(runtime.call("pua_screen_action", {"action": "refresh"})["paused"])
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_screen_action", {"action": "tap"})
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_toolbar_results_are_structured_for_the_app_and_answered_off_the_tool_queue(self):
        runtime, client, screen = self.runtime()
        self.toolbar(runtime)
        result = self.call_over_stdio(runtime, "pua_screen_action", {"action": "home"})
        self.assertEqual(result["structuredContent"], {"ok": True, "action": "home"})
        screen.is_paused = True
        refused = self.call_over_stdio(runtime, "pua_screen_action", {"action": "home"})
        self.assertTrue(refused["isError"])
        self.assertEqual(refused["structuredContent"]["error"]["code"], "preview_paused")
        self.assertEqual(len(runtime.responses), 0)

    def test_header_gets_only_the_model_name_looked_up_once(self):
        runtime, client, screen = self.runtime()
        (Path(self.directory.name) / "config.json").write_text(json.dumps({"udid": "00008150-TESTTESTTEST", "team_id": "TEAM"}))
        found = {"ok": True, "devices": [{"udid": "other", "name": "Someone's iPad", "model": "iPad Air"},
                                         {"udid": "00008150-TESTTESTTEST", "name": "Private Phone Name", "model": "iPhone 17 Pro Max"}]}
        with patch.object(runtime.setup_manager, "discover", return_value=found) as discover:
            runtime.call("pua_screen", {})
            runtime._device_lookup.join(timeout=5)
            runtime.call("pua_screen_frame", {})
        self.assertEqual(screen.device, {"model": "iPhone 17 Pro Max"})
        discover.assert_called_once_with()
        cached = json.loads((Path(self.directory.name) / "device.json").read_text())
        self.assertEqual(cached, {"udid": "00008150-TESTTESTTEST", "model": "iPhone 17 Pro Max"})
        self.assertEqual((Path(self.directory.name) / "device.json").stat().st_mode & 0o777, 0o600)
        again, _, later = self.runtime()
        with patch.object(again.setup_manager, "discover") as discover:
            again.call("pua_screen", {})
            again._device_lookup.join(timeout=5)
        discover.assert_not_called()
        self.assertEqual(later.device, {"model": "iPhone 17 Pro Max"})

    def test_unconfigured_runtime_never_looks_for_a_device(self):
        runtime, client, screen = self.runtime()
        with patch.object(runtime.setup_manager, "discover") as discover:
            runtime.call("pua_screen_frame", {})
            runtime._device_lookup.join(timeout=5)
        discover.assert_not_called()
        self.assertIsNone(screen.device)

    def test_runtime_close_releases_preview_and_transport(self):
        runtime, client, screen = self.runtime()
        runtime.close()
        self.assertTrue(screen.closed)
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
