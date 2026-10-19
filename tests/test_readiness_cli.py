"""CLI health probes must fail when READY reports an expected waiting state."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
import iphone_use
from wda_client import WDAError
from wda_access import AccessGate
from wda_screen import ScreenHub

spec = importlib.util.spec_from_file_location("readiness_smoke", ROOT / "scripts" / "smoke_mcp.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class CapturedInput(io.StringIO):
    def close(self):
        self.requests = [json.loads(line) for line in self.getvalue().splitlines()]
        super().close()


class FakeChild:
    """Supplies protocol replies without spawning a service or touching a phone."""
    def __init__(self, ready_result):
        self.stdin = CapturedInput()
        responses = [
            {"serverInfo": {"name": "iphone-use", "version": "test"}},
            {"tools": [{"name": "pua_ready"}]},
            {},
            ready_result,
        ]
        self.stdout = io.StringIO("".join(json.dumps({"jsonrpc": "2.0", "id": index, "result": result}) + "\n"
                                             for index, result in enumerate(responses, start=1)))
        self.wait = Mock(return_value=0)
        self.terminate = Mock()


class ReadinessCLITests(unittest.TestCase):
    def server_main(self, data=None, error=None):
        runtime = Mock()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        screen = ScreenHub(directory.name)
        self.addCleanup(screen.close)
        runtime.access = AccessGate(screen)
        if error is not None:
            runtime.call.side_effect = error
        else:
            runtime.call.return_value = data
        output = io.StringIO()
        with patch.object(iphone_use, "Runtime", return_value=runtime), \
                patch.object(sys, "argv", ["iphone_use.py", "--ready"]), contextlib.redirect_stdout(output):
            status = iphone_use.main()
        runtime.call.assert_called_once_with("pua_ready", {})
        runtime.close.assert_called_once_with()
        return status, json.loads(output.getvalue())

    def test_server_cli_waiting_states_return_one_with_structured_state(self):
        for state in ("recovery_required", "recovering"):
            with self.subTest(state=state):
                data = {"ready": False, "state": state, "recovery": {"state": "disabled" if state == "recovery_required" else "queued"}}
                status, result = self.server_main(data)
                self.assertEqual(status, 1)
                self.assertEqual(result, data)
                self.assertNotIn("error", result)

    def test_server_cli_requires_explicit_true_ready(self):
        for ready in (None, 1, "true"):
            with self.subTest(ready=ready):
                data = {} if ready is None else {"ready": ready}
                status, result = self.server_main(data)
                self.assertEqual(status, 1)
                self.assertEqual(result, data)

    def test_server_cli_healthy_ready_returns_zero(self):
        data = {"ready": True, "state": "ready", "proof": {"foreground_resolved": True}}
        status, result = self.server_main(data)
        self.assertEqual(status, 0)
        self.assertEqual(result, data)

    def test_server_cli_real_failure_still_returns_error_and_one(self):
        status, result = self.server_main(error=WDAError("phone_locked", "Unlock the phone"))
        self.assertEqual(status, 1)
        self.assertEqual(result["error"]["code"], "phone_locked")

    def smoke_main(self, data, error=False, image=False):
        content = [{"type": "text", "text": json.dumps(data)}]
        if image:
            content.append({"type": "image", "data": "test-image", "mimeType": "image/png"})
        child = FakeChild({"isError": error, "content": content})
        output = io.StringIO()
        with patch.object(smoke.subprocess, "Popen", return_value=child) as launch, \
                patch.object(sys, "argv", ["smoke_mcp.py", "--ready"]), contextlib.redirect_stdout(output):
            status = smoke.main()
        launch.assert_called_once()
        self.assertTrue(child.stdin.closed)
        self.assertTrue(child.stdout.closed)
        child.wait.assert_called_once_with(timeout=5)
        child.terminate.assert_not_called()
        ready_calls = [request for request in child.stdin.requests if request["method"] == "tools/call"]
        self.assertEqual(len(ready_calls), 1)
        self.assertEqual(ready_calls[0]["params"], {"name": "pua_ready", "arguments": {"recover": False}})
        return status, json.loads(output.getvalue())

    def test_read_only_smoke_expected_not_ready_keeps_state_without_proof_or_image(self):
        for state in ("recovery_required", "recovering"):
            with self.subTest(state=state):
                data = {"ready": False, "state": state, "recovery": {"state": "disabled" if state == "recovery_required" else "queued"}}
                status, result = self.smoke_main(data)
                self.assertEqual(status, 1)
                self.assertEqual(result["state"], state)
                self.assertFalse(result["ready"])
                self.assertTrue(result["protocol_ok"])
                self.assertNotIn("error", result)
                self.assertNotIn("proof", result)
                self.assertNotIn("image_content_returned", result)

    def test_smoke_missing_ready_never_claims_healthy_or_indexes_missing_proof(self):
        status, result = self.smoke_main({"state": "recovering"})
        self.assertEqual(status, 1)
        self.assertEqual(result["state"], "recovering")
        self.assertNotIn("proof", result)

    def test_smoke_healthy_ready_returns_zero_with_proof_and_image_flag(self):
        data = {"ready": True, "state": "ready", "proof": {"foreground_resolved": True}}
        status, result = self.smoke_main(data, image=True)
        self.assertEqual(status, 0)
        self.assertTrue(result["ready"])
        self.assertEqual(result["proof"], data["proof"])
        self.assertTrue(result["image_content_returned"])
        self.assertEqual(len(result["timings"]), 4)

    def test_smoke_real_tool_error_remains_nonzero_error(self):
        status, result = self.smoke_main({"error": {"code": "phone_locked", "message": "Unlock"}}, error=True)
        self.assertEqual(status, 1)
        self.assertFalse(result["ready"])
        self.assertEqual(result["error"]["code"], "phone_locked")


if __name__ == "__main__":
    unittest.main()
