import contextlib
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
from iphone_use import Runtime, result_content, serve
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA


def payload(result):
    """The model-facing data: one JSON text block, never a second structured copy."""
    assert "structuredContent" not in result, "structuredContent would hide content blocks from the model"
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def exchange(self, requests, url=None):
        payload = "".join(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n" if not isinstance(item, str) else item + "\n" for item in requests)
        command = [sys.executable, str(ROOT / "server" / "iphone_use.py"), "--state-dir", self.directory.name]
        if url:
            command.extend(["--url", url])
        process = subprocess.run(
            command,
            input=payload, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stderr, "")
        return [json.loads(line) for line in process.stdout.splitlines()]

    def runtime(self):
        runtime = Runtime(self.directory.name)
        self.addCleanup(runtime.close)
        client = FakeWDA()
        runtime.client = client
        # Restore a close method for Runtime cleanup; fake transports have no sockets.
        client.close = lambda: None
        runtime.phone = PhoneController(client, self.directory.name)
        return runtime, client

    def serve_ready(self, runtime, arguments):
        request = {"jsonrpc": "2.0", "id": "ready-state", "method": "tools/call", "params": {
            "name": "pua_ready", "arguments": arguments}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(runtime)
        response = json.loads(output.getvalue())
        self.assertEqual(response["id"], "ready-state")
        self.assertNotIn("error", response)
        result = response["result"]
        # Compact separators: no padding bytes in the model context.
        self.assertNotIn('": ', result["content"][0]["text"])
        payload(result)
        return result

    def test_ready_diagnostic_and_pending_states_are_normal_mcp_results(self):
        for pending in (False, True):
            with self.subTest(pending=pending):
                runtime, client = self.runtime()
                manager = Mock()
                manager.mirroring_running.return_value = False
                manager.pending_recovery.return_value = {"job_id": "existing-recovery", "state": "restarting"} if pending else None
                runtime.setup_manager = manager
                client.app = "local.pid.0"
                result = self.serve_ready(runtime, {"screenshot": False, "recover": False})
                self.assertFalse(result["isError"])
                data = payload(result)
                self.assertFalse(data["ready"])
                self.assertEqual(data["state"], "recovering" if pending else "recovery_required")
                self.assertNotIn("error", data)
                self.assertNotIn("proof", data)
                self.assertNotIn("observation", data)
                self.assertEqual([item["type"] for item in result["content"]], ["text"])
                manager.recover.assert_not_called()
                self.assertEqual([path for _, path, _ in client.actions() if path != "/session"], [])

    def test_queued_ready_recovery_is_normal_mcp_progress(self):
        runtime, client = self.runtime()
        manager = Mock()
        manager.pending_recovery.return_value = None
        manager.recover.return_value = {"ok": True, "job_id": "queued-recovery", "recovery": {"state": "queued"}}
        runtime.setup_manager = manager
        client.app = "local.pid.0"
        result = self.serve_ready(runtime, {"screenshot": False, "recover": True})
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertFalse(data["ready"])
        self.assertEqual(data["state"], "recovering")
        self.assertEqual(data["recovery"]["status_arguments"], {"action": "status", "job_id": "queued-recovery"})
        self.assertEqual(data["recovery"]["next_arguments"], {"screenshot": False, "recover": True})
        self.assertNotIn("proof", data)
        self.assertEqual([item["type"] for item in result["content"]], ["text"])
        manager.recover.assert_called_once_with()

    def test_ready_refusal_lock_and_unknown_fault_remain_mcp_errors(self):
        for case, expected in (("owner", "pua_recovery_required"), ("locked", "phone_locked"), ("unknown", "device-fault")):
            with self.subTest(case=case):
                runtime, client = self.runtime()
                manager = Mock()
                manager.pending_recovery.return_value = None
                manager.recover.return_value = {"ok": False, "error": "Service ownership could not be proven"}
                runtime.setup_manager = manager
                if case == "owner":
                    client.app = "local.pid.0"
                elif case == "locked":
                    client.locked = True
                else:
                    client.source = Mock(side_effect=WDAError("device-fault", "Unexpected channel fault"))
                result = self.serve_ready(runtime, {"screenshot": False})
                self.assertTrue(result["isError"])
                self.assertEqual(payload(result)["error"]["code"], expected)
                self.assertEqual([path for _, path, _ in client.actions() if path != "/session"], [])

    def test_ready_success_keeps_mcp_observation_and_proof(self):
        runtime, client = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = self.serve_ready(runtime, {"screenshot": False, "recover": False})
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertTrue(data["ready"])
        self.assertEqual(data["state"], "ready")
        self.assertTrue(data["proof"]["foreground_resolved"])
        self.assertGreater(data["observation"]["total_nodes"], 0)

    def test_stdio_initialization_notifications_ping_and_tool_catalog(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": "ping", "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        ])
        self.assertEqual([response["id"] for response in responses], [1, "ping", 3])
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "iphone-use")
        self.assertEqual(responses[1]["result"], {})
        tools = responses[2]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        self.assertEqual(len(names), 20)
        self.assertEqual(len(names), len(set(names)))
        for name in ("pua_ready", "pua_setup", "pua_observe", "pua_tap", "pua_type_text", "pua_batch", "pua_collect_list", "pua_apps"):
            self.assertIn(name, names)
        for tool in tools:
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        apps = next(tool for tool in tools if tool["name"] == "pua_apps")
        self.assertTrue(apps["annotations"]["readOnlyHint"])
        self.assertFalse(apps["annotations"]["destructiveHint"])
        self.assertTrue(apps["annotations"]["idempotentHint"])
        self.assertEqual(apps["inputSchema"]["required"], ["query"])

    def test_action_schema_exposes_optimistic_defaults_and_explicit_checkpoints(self):
        response = self.exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])[0]
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}
        def resolved(schema, prop):
            value = schema["properties"][prop]
            return schema["$defs"][value["$ref"].split("/")[-1]] if "$ref" in value else value
        for name in ("pua_tap", "pua_swipe", "pua_type_text", "pua_press_button", "pua_launch_app"):
            with self.subTest(name=name):
                schema = tools[name]["inputSchema"]
                self.assertEqual(resolved(schema, "observe")["default"], "none")
                self.assertEqual(set(resolved(schema, "expect")["properties"]), set(resolved(tools["pua_find"]["inputSchema"], "selector")["properties"]))
                if name != "pua_tap":
                    self.assertFalse(schema["properties"]["verify"]["default"])
        for name in ("pua_tap", "pua_swipe"):
            self.assertNotIn("observation_id", tools[name]["inputSchema"].get("required", []))
        batch_steps = tools["pua_batch"]["inputSchema"]["properties"]["steps"]["items"]["oneOf"]
        def contract(value, definitions):
            if isinstance(value, dict):
                if "$ref" in value:return contract(definitions[value["$ref"].split("/")[-1]], definitions)
                return {k:contract(v, definitions) for k,v in value.items() if k not in ("description", "examples", "$defs")}
            if isinstance(value, list):return [contract(v, definitions) for v in value]
            return value
        for step in batch_steps:
            op = step["properties"]["op"]["const"]
            standalone = tools["pua_" + op]["inputSchema"]
            self.assertEqual(contract(step["properties"]["args"], tools["pua_batch"]["inputSchema"]["$defs"]),
                             contract(standalone, standalone.get("$defs", {})))

    def test_stdio_apps_catalog_returns_public_evidence_without_wda_access(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "pua_apps", "arguments": {"query": "招商银行", "source": "catalog"}}},
        ], url="http://127.0.0.1:1")
        result = responses[0]["result"]
        self.assertFalse(result["isError"])
        data = payload(result)
        self.assertTrue(data["ok"])
        self.assertEqual(data["searched_sources"], ["catalog"])
        self.assertEqual(data["candidates"][0]["bundle_id"], "com.cmbchina.MPBBank")
        self.assertFalse(data["candidates"][0]["installed_verified"])

    def test_apps_catalog_is_read_only_even_when_phone_is_locked(self):
        runtime, client = self.runtime()
        client.locked = True
        result = runtime.call("pua_apps", {"query": "招商银行", "source": "catalog"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["candidates"][0]["bundle_id"], "com.cmbchina.MPBBank")
        self.assertEqual(client.calls, [])

    def test_stdio_invalid_arguments_return_tool_error_without_wda_access(self):
        invalid_cases = [
            ("pua_tap", {"x": True, "y": 200, "observation_id": "unused"}),
            ("pua_type_text", {"selector": {"label": "Target"}, "text": "line 1\nline 2"}),
            ("pua_find", {"selector": {"label": "Target", "predicate": "label == 'Target'"}}),
            ("pua_batch", {"steps": [{"op": "tap", "args": {"selector": {"label": "Target"}}}, {"op": "unknown", "args": {}}]}),
            ("wda_missing", {}),
            ("pua_apps", {"query": "招商银行", "source": "guess"}),
            ("pua_apps", {"query": "招商银行", "country": "CHN"}),
            ("pua_apps", {"query": ""}),
        ]
        responses = self.exchange([{"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": args}} for index, (name, args) in enumerate(invalid_cases)])
        self.assertEqual(len(responses), len(invalid_cases))
        for response in responses:
            result = response["result"]
            self.assertTrue(result["isError"])
            content = payload(result)
            self.assertNotIn(content["error"]["code"], ("pua_unreachable", "action_uncertain", "internal_error"))

    def test_stdio_parse_error_does_not_break_following_ping(self):
        responses = self.exchange(["{broken", {"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertEqual(responses[1]["result"], {})

    def test_default_swipe_executes_once_without_progress_or_output_reads(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_swipe", {"direction": "up"})
        self.assertTrue(result["action_executed"])
        self.assertFalse(result["verified"])
        self.assertTrue(result["verification_deferred"])
        self.assertEqual(result["attempts"], 1)
        self.assertNotIn("observation", result)
        self.assertEqual(client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_swipe_schema_accepts_explicit_progress_verification_without_output(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_swipe", {"direction": "up", "observe": "none", "verify": True})
        self.assertEqual(caught.exception.code, "no_scroll_progress")
        self.assertEqual(caught.exception.details["attempts"], 1)
        self.assertTrue(caught.exception.details["action_executed"])
        self.assertEqual(client.swipe_count, 1)
        self.assertIn("image", caught.exception.details["observation"])

    def test_unknown_argument_reports_allowed_fields_without_device_access(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_observe", {"observe": "none"})
        error = caught.exception.as_dict()
        self.assertEqual(error["code"], "invalid_argument")
        self.assertFalse(error["action_executed"])
        self.assertEqual(error["unknown_fields"], ["observe"])
        self.assertIn("mode", error["allowed_fields"])
        self.assertNotIn("observe", error["allowed_fields"])
        self.assertEqual(error["argument_path"], "arguments")
        self.assertEqual(client.calls, [])

    def test_no_progress_error_image_is_mcp_image_content_and_retains_details(self):
        runtime, client = self.runtime()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_swipe", {"observe": "screenshot", "max_attempts": 1, "verify": True})
        result = result_content({"error": caught.exception.as_dict()})
        self.assertTrue(result["isError"])
        self.assertEqual(payload(result)["error"]["code"], "no_scroll_progress")
        self.assertTrue(payload(result)["error"]["action_executed"])
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        import base64
        self.assertEqual(base64.b64decode(result["content"][1]["data"]), client.screenshot)

    def test_scroll_search_returns_model_image_before_a_second_gesture(self):
        runtime, client = self.runtime()
        client.elements.clear()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_scroll_find", {"selector": {"label": "Missing"}, "max_swipes": 10})
        result = result_content({"error": caught.exception.as_dict()})
        self.assertTrue(result["isError"])
        self.assertEqual(payload(result)["error"]["swipes"], 1)
        self.assertTrue(payload(result)["error"]["recovery"]["visual_check_required"])
        self.assertEqual([block["type"] for block in result["content"]], ["text", "image"])
        self.assertEqual(client.swipe_count, 1)

    def test_batch_continues_optimistic_navigation_and_input_without_readback(self):
        runtime, client = self.runtime()
        text = "直接输入完整的长文本，不做试输入。" * 20
        result = runtime.call("pua_batch", {"steps": [
            {"op": "press_button", "args": {"name": "home"}},
            {"op": "launch_app", "args": {"bundle_id": "com.example.phone"}},
            {"op": "tap", "args": {"selector": {"label": "Target"}}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": text}},
            {"op": "swipe", "args": {}},
        ]})
        self.assertTrue(result["complete"])
        self.assertEqual(result["completed_steps"], 5)
        self.assertTrue(all(item["action_executed"] for item in result["results"]))
        self.assertTrue(all(item["verification_deferred"] for item in result["results"]))
        self.assertTrue(all(not item["verified"] for item in result["results"]))
        self.assertEqual(client.elements[0]["value"], text)
        self.assertEqual(client.swipe_count, 1)
        self.assertFalse(any(path.startswith("/source") or path in ("/screenshot", "/wda/activeAppInfo")
                             or path.endswith("/attribute/value") for _, path, _ in client.calls))

    def test_batch_explicit_progress_check_stops_after_no_progress(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_batch", {"steps": [
            {"op": "swipe", "args": {"observe": "none", "verify": True}},
            {"op": "press_button", "args": {"name": "home", "observe": "none"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["error"]["code"], "no_scroll_progress")
        self.assertTrue(result["error"]["action_executed"])
        self.assertEqual(client.swipe_count, 1)
        self.assertEqual([block["type"] for block in result_content(result)["content"]], ["text", "image"])
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in client.actions()))

    def test_batch_stops_after_uncertain_action_and_never_replays_it(self):
        runtime, client = self.runtime()
        client.home_error = WDAError("action_uncertain", "Home response was lost", uncertain=True)
        result = runtime.call("pua_batch", {"steps": [
            {"op": "press_button", "args": {"name": "home"}},
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 0)
        self.assertEqual(result["error"]["code"], "action_uncertain")
        self.assertTrue(result["error"]["uncertain"])
        self.assertEqual([path for _, path, _ in client.actions()], ["/wda/homescreen"])

    def test_batch_stops_after_submission_until_a_deliberate_checkpoint(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_batch", {"steps": [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Submit once", "submit": True}},
            {"op": "press_button", "args": {"name": "home"}},
        ]})
        self.assertFalse(result["complete"])
        self.assertEqual(result["completed_steps"], 1)
        self.assertEqual(result["stop_reason"], "submission_requires_verification")
        self.assertTrue(result["results"][0]["submitted"])
        self.assertEqual(sum(path == "/wda/keys" for _, path, _ in client.actions()), 1)
        self.assertFalse(any(path == "/wda/homescreen" for _, path, _ in client.actions()))

    def test_home_and_text_accept_explicit_expected_next_state(self):
        for name, arguments in (
            ("pua_press_button", {"name": "home", "expect": {"label": "Target"}}),
            ("pua_type_text", {"selector": {"label": "Target"}, "text": "Complete text", "expect": {"label": "Target"}}),
        ):
            with self.subTest(name=name):
                runtime, _ = self.runtime()
                result = runtime.call(name, arguments)
                self.assertTrue(result["verified"])
                self.assertFalse(result["verification_deferred"])
                self.assertIn("postcondition", result)

    def test_exact_enabled_tree_string_survives_protocol_schema(self):
        runtime, client = self.runtime()
        result = runtime.call("pua_find", {"selector": {"label": "Target", "enabled": "true"}})
        self.assertEqual(result["matches"], 1)
        query = next(body["value"] for _, path, body in client.calls if path == "/elements")
        self.assertIn("enabled == true", query)

    def test_stdio_unknown_method_and_invalid_params_are_rpc_errors(self):
        responses = self.exchange([
            {"jsonrpc": "2.0", "id": 1, "method": "not/a/method"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": []},
        ])
        self.assertEqual(responses[0]["error"]["code"], -32601)
        self.assertEqual(responses[1]["error"]["code"], -32602)

    def test_nested_batch_schema_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "tap", "args": {"selector": {"label": "Target"}, "extra": True}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_argument")
        details = caught.exception.as_dict()
        self.assertEqual(details["argument_path"], "arguments.steps[1].args")
        self.assertEqual(details["unknown_fields"], ["extra"])
        self.assertIn("observe", details["allowed_fields"])
        self.assertFalse(details["action_executed"])
        self.assertEqual(client.calls, [])

    def test_malformed_later_selector_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "tap", "args": {"selector": {"label": "Target", "predicate": "label == 'Target'"}, "observe": "none"}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_selector")
        self.assertEqual(client.calls, [])

    def test_later_invalid_bundle_id_is_checked_before_any_phone_request(self):
        runtime, client = self.runtime()
        steps = [
            {"op": "type_text", "args": {"selector": {"label": "Target"}, "text": "Do not enter", "observe": "none"}},
            {"op": "launch_app", "args": {"bundle_id": "not-a-bundle", "observe": "none"}},
        ]
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_batch", {"steps": steps})
        self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(client.calls, [])

    def test_coordinates_and_custom_region_accept_no_observation_id(self):
        for name, arguments, expected_path in (
            ("pua_tap", {"x": 10, "y": 200}, "/wda/tap"),
            ("pua_swipe", {"region": {"x": 50, "y": 200, "width": 200, "height": 200}}, "/wda/dragfromtoforduration"),
        ):
            with self.subTest(name=name):
                runtime, client = self.runtime()
                result = runtime.call(name, arguments)
                self.assertTrue(result["action_executed"])
                self.assertTrue(result["verification_deferred"])
                self.assertEqual(client.actions()[-1][1], expected_path)
                self.assertFalse(any(path.startswith("/source") or path == "/screenshot" for _, path, _ in client.calls))

    def test_incomplete_or_mixed_coordinates_fail_before_phone_access(self):
        runtime, client = self.runtime()
        invalid = [
            ("pua_tap", {"x": 10}),
            ("pua_tap", {"y": 200}),
            ("pua_tap", {"selector": {"label": "Target"}, "x": 10}),
        ]
        for name, arguments in invalid:
            with self.subTest(name=name, arguments=arguments), self.assertRaises(WDAError) as caught:
                runtime.call(name, arguments)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual(client.calls, [])

    def test_unbound_coordinates_and_region_still_check_viewport_before_mutation(self):
        for name, arguments in (
            ("pua_tap", {"x": 390, "y": 200}),
            ("pua_swipe", {"region": {"x": 300, "y": 200, "width": 200, "height": 200}}),
        ):
            with self.subTest(name=name):
                runtime, client = self.runtime()
                with self.assertRaises(WDAError) as caught:
                    runtime.call(name, arguments)
                self.assertEqual(caught.exception.code, "invalid_argument")
                self.assertFalse(caught.exception.as_dict().get("action_executed", False))
                self.assertEqual(client.actions(), [])

    def test_other_process_holding_operation_lock_refuses_all_phone_requests(self):
        runtime, client = self.runtime()
        script = "import fcntl,sys; lock=open(sys.argv[1], 'a'); fcntl.flock(lock, fcntl.LOCK_EX); print('locked', flush=True); sys.stdin.read(); lock.close()"
        worker = subprocess.Popen(
            [sys.executable, "-c", script, str(Path(self.directory.name) / "operation.lock")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(worker.stdout.readline().strip(), "locked")
            with self.assertRaises(WDAError) as caught:
                runtime.call("pua_tap", {"selector": {"label": "Target"}, "observe": "none"})
            self.assertEqual(caught.exception.code, "device_busy")
            self.assertEqual(client.calls, [])
        finally:
            try:
                worker.communicate(input="release", timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.communicate()
        self.assertEqual(worker.returncode, 0)

    def test_independent_runtimes_share_usable_session_without_new_phone_request(self):
        first, first_client = self.runtime()
        second, second_client = self.runtime()
        first_client.session_id = "shared-session-123"
        with patch.object(first, "_call", side_effect=lambda name, args: {"session_id": first.client.session_id}):
            self.assertEqual(first.call("pua_metrics", {}), {"session_id": "shared-session-123"})
        self.assertIsNone(second_client.session_id)
        with patch.object(second, "_call", side_effect=lambda name, args: {"session_id": second.client.session_id}):
            self.assertEqual(second.call("pua_metrics", {}), {"session_id": "shared-session-123"})
        self.assertEqual(first_client.calls + second_client.calls, [])
        cache = Path(self.directory.name) / "session.json"
        self.assertEqual(json.loads(cache.read_text()), {"url": first.base_url, "session_id": "shared-session-123"})
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)

    def test_locked_phone_blocks_mutations_before_semantic_lookup(self):
        runtime, client = self.runtime()
        client.locked = True
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_tap", {"selector": {"label": "Target"}, "observe": "none"})
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(client.calls, [("GET", "/wda/locked", None)])
        self.assertEqual(client.actions(), [])

    def test_ready_requires_unlocked_phone_before_session_or_observation(self):
        runtime, client = self.runtime()
        client.locked = True
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(caught.exception.code, "phone_locked")
        self.assertEqual(client.calls, [("GET", "/status", None), ("GET", "/wda/locked", None)])
        self.assertIsNone(client.session_id)

    def test_ready_empty_tree_with_mirroring_running_reports_conflict(self):
        runtime, client = self.runtime()
        client.nodes = []
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=True), self.assertRaises(WDAError) as caught:
            runtime.call("pua_ready", {"screenshot": False})
        self.assertEqual(caught.exception.code, "mirroring_conflict")
        self.assertFalse(any(path in ("/wda/tap", "/wda/keys") for _, path, _ in client.actions()))

    def test_ready_nonempty_tree_proves_unlocked_usable_phone(self):
        runtime, client = self.runtime()
        with patch.object(runtime.setup_manager, "mirroring_running", return_value=False):
            result = runtime.call("pua_ready", {"screenshot": False})
        self.assertTrue(result["ready"])
        self.assertEqual(result["state"], "ready")
        self.assertTrue(result["proof"]["phone_unlocked"])
        self.assertTrue(result["proof"]["session_usable"])
        self.assertGreater(result["observation"]["total_nodes"], 0)


if __name__ == "__main__":
    unittest.main()
