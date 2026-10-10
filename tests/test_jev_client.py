import copy
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from jev_client import HOST, MAX_RESPONSE_BYTES, PATH, JevClient, load_api_key, validate_choice
from wda_client import WDAError


def choice(selected, probabilities):
    return {"type": "choice", "choice": selected, "probabilities": probabilities, "confidence": 0.8}


QUESTIONS = {
    "operation": {"type": "choice", "instructions": "Choose the next supported operation.",
                  "criteria": {"TAP": "Tap a visible control.", "DONE": "The goal is visibly complete."}},
    "tap_target": {"type": "choice", "instructions": "Choose the target if the next operation is TAP.",
                   "criteria": {"0": {"element": "[0] Button 通用"}, "1": {"element": "[1] Button 返回"}}},
    "type_text_target": {"type": "choice", "instructions": "Choose a field if entering text.",
                         "criteria": {"2": "Search field"}},
}


def result():
    return {"model": "jev-1.13.0", "answers": {
        "operation": choice("TAP", {"TAP": 0.9, "DONE": 0.1}),
        "tap_target": choice("0", {"0": 0.9, "1": 0.1}),
        "type_text_target": choice("2", {"2": 1.0})},
        "usage": {"input_tokens": 120, "output_tokens": 20}}


class FakeSocket:
    def __init__(self):
        self.timeouts = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)


class FakeResponse:
    def __init__(self, value=None, status=200, raw=None, length=None, on_read=None):
        self.status = status
        self.data = io.BytesIO(raw if raw is not None else json.dumps(value or result()).encode())
        self.length = length
        self.on_read = on_read
        self.closed = False

    def getheader(self, name):
        return self.length

    def isclosed(self):
        return self.closed

    def read1(self, amount):
        if self.on_read:
            self.on_read()
        chunk = self.data.read(amount)
        if not chunk or self.data.tell() == len(self.data.getvalue()):
            self.closed = True
        return chunk


class FakeConnection:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [FakeResponse()])
        self.error = error
        self.sock = FakeSocket()
        self.requests = []
        self.closed = False

    def request(self, method, path, body, headers):
        self.requests.append((method, path, body, headers))
        if self.error:
            raise self.error

    def getresponse(self):
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def close(self):
        self.closed = True


class JevClientTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def classify(self, response=None, **kwargs):
        connection = FakeConnection([response or FakeResponse()])
        with patch("jev_client.http.client.HTTPSConnection", return_value=connection):
            client = JevClient(api_key="test-key")
            return client.classify({"nodes": [{"label": "通用"}]}, QUESTIONS, **kwargs), connection

    def test_official_contract_authentication_and_persistent_connection(self):
        connection = FakeConnection([FakeResponse(), FakeResponse()])
        with patch("jev_client.http.client.HTTPSConnection", return_value=connection) as factory:
            client = JevClient(api_key="test-key")
            first = client.classify({"nodes": [{"label": "通用"}]}, QUESTIONS)
            second = client.classify({"nodes": []}, QUESTIONS, timeout=2)
            factory.assert_called_once()
            self.assertEqual(factory.call_args.args, (HOST,))
            self.assertEqual(len(connection.requests), 2)
            method, path, body, headers = connection.requests[0]
            self.assertEqual((method, path), ("POST", PATH))
            self.assertEqual(headers["Authorization"], "Bearer test-key")
            self.assertEqual(json.loads(body)["model"], "jev-latest")
            self.assertEqual(json.loads(body)["state"]["nodes"][0]["label"], "通用")
            self.assertEqual(first["answers"]["tap_target"]["choice"], "0")
            self.assertNotIn("type_text_target", first["answers"])
            self.assertEqual(first["usage"], {"input_tokens": 120, "output_tokens": 20})
            self.assertEqual(second["model"], "jev-1.13.0")
            self.assertGreaterEqual(first["latency_ms"], 0)
            self.assertLessEqual(connection.sock.timeouts[-1], 2)
            client.close()
            self.assertTrue(connection.closed)

    def test_unused_target_head_does_not_invalidate_selected_action(self):
        response = result()
        response["answers"]["type_text_target"] = {"choice": "unoffered", "confidence": float("nan")}
        parsed, _ = self.classify(FakeResponse(response))
        self.assertEqual(set(parsed["answers"]), {"operation", "tap_target"})

    def test_done_does_not_require_any_target_answer(self):
        response = result()
        response["answers"] = {"operation": choice("DONE", {"TAP": 0.1, "DONE": 0.9})}
        parsed, _ = self.classify(FakeResponse(response))
        self.assertEqual(set(parsed["answers"]), {"operation"})

    def test_standalone_choices_are_all_validated(self):
        questions = {"target": QUESTIONS["tap_target"]}
        response = {"model": "jev-1.13.0", "answers": {"target": result()["answers"]["tap_target"]}, "usage": {}}
        connection = FakeConnection([FakeResponse(response)])
        with patch("jev_client.http.client.HTTPSConnection", return_value=connection):
            answer = JevClient(api_key="test-key").classify("Phone UI", questions)
        self.assertEqual(answer["answers"]["target"]["choice"], "0")

    def test_malformed_unknown_and_nonfinite_choices_fail_without_action(self):
        invalid = [None, {}, {"type": "noul", "noul": 0.9}]
        for change in (
                {"choice": "unknown"}, {"probabilities": {"0": 1}},
                {"probabilities": {"0": 1, "1": 0, "unknown": 0}},
                {"probabilities": {"0": float("nan"), "1": 0}},
                {"probabilities": {"0": float("inf"), "1": 0}},
                {"probabilities": {"0": True, "1": False}},
                {"probabilities": {"0": 0.1, "1": 0.9}},
                {"probabilities": {"0": 0.6, "1": 0.1}},
                {"confidence": -1}, {"confidence": True}, {"confidence": float("nan")},
                {"type": "score"}):
            answer = copy.deepcopy(result()["answers"]["tap_target"])
            answer.update(change)
            invalid.append(answer)
        for answer in invalid:
            with self.subTest(answer=answer):
                with self.assertRaises(WDAError) as captured:
                    validate_choice(answer, QUESTIONS["tap_target"]["criteria"])
                self.assertEqual(captured.exception.code, "jev_invalid_response")
                self.assertFalse(captured.exception.details["action_executed"])

    def test_missing_selected_target_and_invalid_envelope_fail(self):
        samples = [[], {"answers": {}}, result(), result()]
        samples[2]["answers"].pop("tap_target")
        samples[3]["usage"] = "invalid"
        for value in samples:
            with self.subTest(value=value), self.assertRaises(WDAError) as captured:
                self.classify(FakeResponse(raw=json.dumps(value).encode()))
            self.assertEqual(captured.exception.code, "jev_invalid_response")

    def test_http_errors_are_redacted_and_never_retried(self):
        for status, code in ((401, "jev_authentication"), (422, "jev_http_error"),
                             (429, "jev_rate_limited"), (529, "jev_overloaded"), (503, "jev_overloaded")):
            connection = FakeConnection([FakeResponse(status=status, raw=b"private-key sensitive-phone-data")])
            with self.subTest(status=status), patch("jev_client.http.client.HTTPSConnection", return_value=connection):
                with self.assertRaises(WDAError) as captured:
                    JevClient(api_key="private-key").classify("sensitive-phone-data", QUESTIONS)
                self.assertEqual(captured.exception.code, code)
                self.assertEqual(captured.exception.details["http_status"], status)
                self.assertNotIn("private-key", str(captured.exception))
                self.assertNotIn("sensitive-phone-data", str(captured.exception))
                self.assertEqual(len(connection.requests), 1)
                self.assertTrue(connection.closed)

    def test_transport_failure_has_only_one_redacted_reconnect(self):
        for error, code in ((socket.timeout("private-key"), "jev_timeout"),
                            (OSError("private-key"), "jev_unavailable")):
            connections = [FakeConnection(error=error), FakeConnection(error=error)]
            with patch("jev_client.http.client.HTTPSConnection", side_effect=connections) as factory:
                with self.assertRaises(WDAError) as captured:
                    JevClient(api_key="private-key").classify("UI", QUESTIONS)
                self.assertEqual(captured.exception.code, code)
                self.assertNotIn("private-key", str(captured.exception))
                self.assertEqual(factory.call_count, 2)
                self.assertTrue(all(len(connection.requests) == 1 and connection.closed for connection in connections))

    def test_stale_pooled_https_connection_reconnects_once_without_phone_action(self):
        pooled = FakeConnection([FakeResponse(), http.client.RemoteDisconnected("private-key")])
        fresh = FakeConnection([FakeResponse()])
        with patch("jev_client.http.client.HTTPSConnection", side_effect=[pooled, fresh]) as factory:
            client = JevClient(api_key="private-key")
            first = client.classify("UI", QUESTIONS)
            second = client.classify("UI", QUESTIONS)
            self.assertEqual(first["answers"], second["answers"])
            self.assertEqual(factory.call_count, 2)
            self.assertEqual(len(pooled.requests), 2)
            self.assertEqual(len(fresh.requests), 1)
            self.assertTrue(pooled.closed)
            self.assertIs(client.connection, fresh)

    def test_reconnect_retains_original_deadline(self):
        now = [0.0]
        broken, fresh = FakeConnection(), FakeConnection()
        def failure(*args):
            now[0] = 0.8
            raise OSError("private-key")
        broken.request = failure
        with patch("jev_client.time.monotonic", side_effect=lambda: now[0]):
            with patch("jev_client.http.client.HTTPSConnection", side_effect=[broken, fresh]) as factory:
                parsed = JevClient(api_key="private-key").classify("UI", QUESTIONS, timeout=1)
        self.assertAlmostEqual(factory.call_args.kwargs["timeout"], 0.2)
        self.assertEqual(parsed["latency_ms"], 800)
        self.assertTrue(broken.closed)

    def test_expired_transport_failure_does_not_reconnect(self):
        now = [0.0]
        connection = FakeConnection()
        def failure(*args):
            now[0] = 1.1
            raise socket.timeout("private-key")
        connection.request = failure
        with patch("jev_client.time.monotonic", side_effect=lambda: now[0]):
            with patch("jev_client.http.client.HTTPSConnection", return_value=connection) as factory:
                with self.assertRaises(WDAError) as captured:
                    JevClient(api_key="private-key").classify("UI", QUESTIONS, timeout=1)
        self.assertEqual(captured.exception.code, "jev_timeout")
        self.assertEqual(factory.call_count, 1)

    def test_slow_response_obeys_original_deadline(self):
        now = [0.0]
        def delayed_read():
            now[0] += 1.1
        response = FakeResponse(on_read=delayed_read)
        with patch("jev_client.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(WDAError) as captured:
                self.classify(response, timeout=1)
        self.assertEqual(captured.exception.code, "jev_timeout")

    def test_response_size_and_json_are_bounded(self):
        for response in (FakeResponse(raw=b"private-key not json"),
                         FakeResponse(length=str(MAX_RESPONSE_BYTES + 1)),
                         FakeResponse(length="garbage"),
                         FakeResponse(raw=b"x" * (MAX_RESPONSE_BYTES + 1))):
            with self.assertRaises(WDAError) as captured:
                self.classify(response)
            self.assertEqual(captured.exception.code, "jev_invalid_response")
            self.assertNotIn("private-key", str(captured.exception))

    def test_invalid_request_never_connects(self):
        with patch("jev_client.http.client.HTTPSConnection") as factory:
            for timeout in (0, -1, True, float("inf"), 61):
                with self.assertRaises(WDAError):
                    JevClient(api_key="test-key").classify("UI", QUESTIONS, timeout=timeout)
            for questions in ({}, {"target": {"type": "choice", "criteria": {"0": None}}},
                              {"target": {"type": "choice", "instructions": "Pick", "criteria":
                                          {str(index): None for index in range(256)}}}):
                with self.assertRaises(WDAError):
                    JevClient(api_key="test-key").classify("UI", questions)
            with self.assertRaises(WDAError):
                JevClient(api_key="test-key").classify({"bad": float("nan")}, QUESTIONS)
            factory.assert_not_called()

    def test_credential_environment_priority_and_model(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.json"
            path.write_text(json.dumps({"api_key": "file-key"}))
            path.chmod(0o600)
            self.assertEqual(load_api_key(directory), "file-key")
            with patch.dict(os.environ, {"TYPESAFE_API_KEY": " environment-key ", "TYPESAFE_MODEL": "jev-1.13.0"}):
                self.assertEqual(load_api_key(directory, "explicit-key"), "environment-key")
                self.assertEqual(JevClient(directory).model, "jev-1.13.0")
            path.chmod(0o644)
            with self.assertRaises(WDAError) as captured:
                load_api_key(directory)
            self.assertEqual(captured.exception.code, "jev_configuration_invalid")
            self.assertNotIn("file-key", str(captured.exception))

    def test_missing_malformed_and_symlinked_credentials_fail_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jev.json"
            with self.assertRaises(WDAError) as captured:
                load_api_key(directory)
            self.assertEqual(captured.exception.code, "jev_not_configured")
            for raw in ("private-key not json", "[]", '{"api_key":null}'):
                path.write_text(raw)
                path.chmod(0o600)
                with self.assertRaises(WDAError) as captured:
                    load_api_key(directory)
                self.assertEqual(captured.exception.code, "jev_configuration_invalid")
                self.assertNotIn("private-key", str(captured.exception))
            path.unlink()
            target = Path(directory) / "other.json"
            target.write_text('{"api_key":"file-key"}')
            target.chmod(0o600)
            path.symlink_to(target)
            with self.assertRaises(WDAError):
                load_api_key(directory)
        for invalid in ("two keys", "key\nheader", "密钥", ""):
            with self.assertRaises(WDAError):
                load_api_key(api_key=invalid)


if __name__ == "__main__":
    unittest.main()
