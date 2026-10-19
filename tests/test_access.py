"""Authentication is enforced at MCP, transport and final response boundaries."""
import contextlib
import http.server
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime, serve, tool_result
from wda_access import AccessGate
from wda_client import WDAClient, WDAError
from wda_controller import PhoneController
from wda_screen import ScreenHub
from test_controller import FakeWDA


class AccessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Runtime(directory.name)
        self.addCleanup(self.runtime.close)
        self.other = ScreenHub(directory.name)
        self.addCleanup(self.other.close)
        self.client = FakeWDA()
        self.client.close = Mock()
        self.runtime.client = self.client
        self.runtime.phone = PhoneController(self.client, directory.name)
        self.runtime.phone.screen = self.runtime.screen

    def denied(self, name, args):
        with self.assertRaises(WDAError) as caught:
            self.runtime.call(name, args)
        self.assertEqual(caught.exception.code, "access_paused")
        return caught.exception

    def test_pause_denies_all_read_action_and_bypass_entries_without_phone_requests(self):
        self.runtime.call("pua_screen", {"action": "pause"})
        cases = [("observe", {"mode": mode}) for mode in ("tree", "screenshot", "both")]
        cases += [("tap", {"x": 1, "y": 1}), ("type_text", {"text": "secret"}),
                  ("batch", {"steps": [{"op": "observe", "args": {}}]}),
                  ("apps", {"query": "app"}), ("ready", {"screenshot": False}),
                  ("setup", {"action": "start"}), ("setup", {"action": "configure"})]
        cases += [("screen_action", {"action": a}) for a in ("refresh", "home", "screenshot")]
        self.runtime.apps.lookup = Mock()
        self.runtime.setup_manager.setup = Mock()
        for op, args in cases:
            with self.subTest(op=op, args=args):
                self.denied("pua_" + op, args)
        self.assertEqual(self.client.calls, [])
        self.runtime.apps.lookup.assert_not_called()
        self.runtime.setup_manager.setup.assert_not_called()
        self.assertTrue(self.runtime.call("pua_screen", {})["paused"])
        frame = self.runtime.call("pua_screen_frame", {})
        self.assertIsNone(frame["frame"])

    def test_manual_resume_preserves_upstream_semantics_without_a_dialog(self):
        self.runtime.identify_device = Mock()
        self.other.set_paused(True)
        epoch = self.other._read_state()["access_epoch"]
        with patch("subprocess.run") as process:
            result = self.runtime.call("pua_screen", {"action": "resume"})
        self.assertFalse(result["paused"])
        self.assertGreater(self.other._read_state()["access_epoch"], epoch)
        process.assert_not_called()
        self.assertEqual(self.client.calls, [])

    def test_queued_operation_and_late_result_rejected_even_after_pause_resume(self):
        epoch = self.runtime.access.capture_epoch()
        self.other.set_paused(True)
        self.other.set_paused(False)
        with self.assertRaises(WDAError):
            self.runtime.call("pua_tap", {"x": 1, "y": 1}, received_epoch=epoch)
        self.assertEqual(self.client.calls, [])
        self.runtime.access.begin("pua_observe", {})
        token = dict(self.runtime.access.token())
        self.other.set_paused(True)
        self.other.set_paused(False)
        with self.assertRaises(WDAError):
            with self.runtime.access.delivery(token):
                self.fail("Old response reached output")

    def test_batch_stops_at_pause_and_discards_previously_collected_results(self):
        def first(**_):
            self.other.set_paused(True)
            return {"nodes": [{"label": "private-before-pause"}]}
        self.runtime.phone.observe = Mock(side_effect=first)
        self.runtime.phone.tap = Mock()
        data = tool_result(self.runtime, {"name": "pua_batch", "arguments": {"steps": [
            {"op": "observe", "args": {}}, {"op": "tap", "args": {"x": 1, "y": 1}}]}})
        self.assertNotIn("private-before-pause", json.dumps(data))
        self.assertIn("access_paused", json.dumps(data))
        self.runtime.phone.tap.assert_not_called()

    def test_stdio_rejects_work_queued_before_a_pause_even_after_resume(self):
        entered, release = threading.Event(), threading.Event()
        def observe(**_):
            entered.set()
            release.wait(2)
            return {"nodes": [{"label": "old-queued-context"}]}
        self.runtime.phone.observe = Mock(side_effect=observe)
        self.runtime.phone.tap = Mock()
        def lines():
            yield json.dumps({"jsonrpc":"2.0","id":1,"method":"tools/call", "params":{"name":"pua_observe","arguments":{}}}) + "\n"
            self.assertTrue(entered.wait(2))
            yield json.dumps({"jsonrpc":"2.0","id":2,"method":"tools/call", "params":{"name":"pua_tap","arguments":{"x":1,"y":1}}}) + "\n"
            self.other.set_paused(True)
            self.other.set_paused(False)
            release.set()
        output = io.StringIO()
        with patch.object(sys, "stdin", lines()), contextlib.redirect_stdout(output):
            serve(self.runtime)
        self.runtime.phone.tap.assert_not_called()
        self.assertNotIn("old-queued-context", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 2)
        for line in output.getvalue().splitlines():self.assertIn("access_paused", line)

    def test_corrupt_state_is_paused_across_a_new_runtime(self):
        for content in ("broken JSON", "[]", '{"paused":"false"}', '{"access_epoch":null}'):
            with self.subTest(content=content):
                self.other._state_path.write_text(content)
                runtime = Runtime(self.other.state_dir)
                try:
                    with self.assertRaises(WDAError):runtime.call("pua_observe", {})
                    self.assertTrue(runtime.screen.paused())
                finally:runtime.close()

    def test_stdio_rechecks_result_immediately_before_output(self):
        original = self.runtime.note_response
        def pause_after_result(*args):
            original(*args)
            self.other.set_paused(True)
        self.runtime.phone.observe = Mock(return_value={"nodes": [{"label": "private-late-output"}]})
        self.runtime.note_response = pause_after_result
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "pua_observe", "arguments": {}}}
        output = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(json.dumps(request) + "\n")), contextlib.redirect_stdout(output):
            serve(self.runtime)
        wire = output.getvalue()
        self.assertNotIn("private-late-output", wire)
        self.assertIn("access_paused", wire)

    def test_ready_cli_rechecks_access_after_collection_before_printing(self):
        from iphone_use import main
        original = self.runtime.call
        self.runtime.phone.observe = Mock(return_value={
            "total_nodes": 1, "nodes": [{"label": "private-main-ready-output"}]})
        def pause_after_collection(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertTrue(result["ready"])
            self.other.set_paused(True)
            return result
        output = io.StringIO()
        with patch.object(sys, "argv", ["iphone_use.py", "--ready"]), \
                patch("iphone_use.Runtime", return_value=self.runtime), \
                patch.object(self.runtime, "call", side_effect=pause_after_collection), \
                contextlib.redirect_stdout(output):
            status = main()
        self.assertEqual(status, 1)
        self.assertTrue(self.other.paused())
        self.assertNotIn("private-main-ready-output", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "access_paused")

    def test_real_http_inflight_read_and_write_are_suppressed_and_not_replayed(self):
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                self.other.set_paused(False)
                arrived, release = threading.Event(), threading.Event()
                calls = []
                class Handler(http.server.BaseHTTPRequestHandler):
                    def do_GET(self):
                        calls.append(self.command)
                        arrived.set()
                        release.wait(3)
                        body = b'{"value":{"secret":"private-inflight"}}'
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    do_POST = do_GET
                    def log_message(self, *_):pass
                server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                host = threading.Thread(target=server.serve_forever, daemon=True)
                host.start()
                client = WDAClient(f"http://127.0.0.1:{server.server_port}")
                client.access = self.runtime.access
                results = []
                def request():
                    client.access.begin("pua_observe" if method == "GET" else "pua_tap", {})
                    try:results.append(client.request(method, "/source" if method == "GET" else "/wda/tap", {}))
                    except WDAError as exc:results.append(exc.as_dict())
                thread = threading.Thread(target=request)
                try:
                    thread.start()
                    self.assertTrue(arrived.wait(2))
                    self.other.set_paused(True)
                    # Even completing authentication before the old response arrives
                    # must not revive the response or replay the action.
                    self.other.set_paused(False)
                    release.set()
                    thread.join(3)
                    self.assertEqual(calls, [method])
                    self.assertEqual(results[0]["code"], "access_paused")
                    self.assertEqual(results[0]["uncertain"], method == "POST")
                    self.assertNotIn("private-inflight", json.dumps(results))
                finally:
                    release.set()
                    client.close()
                    server.shutdown()
                    server.server_close()
                    host.join(2)


if __name__ == "__main__":unittest.main()
