import contextlib
import errno
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from wda_client import SESSION_SETTINGS, WDAClient, WDAError


@contextlib.contextmanager
def fake_server(responder):
    requests = []
    peer_ports = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                # The timeout test intentionally closes its client socket mid-response.
                pass

        def handle_request(self):
            size = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(size)) if size else None
            requests.append((self.command, self.path, payload))
            peer_ports.append(self.client_address[1])
            status, value, delay, close = responder(self.command, self.path, payload, requests)
            if delay:
                time.sleep(delay)
            if status is None:
                # Drop the connection before sending a response, like a stale keep-alive.
                self.close_connection = True
                try:
                    self.connection.shutdown(2)
                except OSError:
                    pass
                self.connection.close()
                return
            raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = close

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:%s" % server.server_port, requests, peer_ports
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


class ClientTests(unittest.TestCase):
    @staticmethod
    def warm_session(client, sid):
        # Model an already configured client so these tests isolate command transport.
        client.session_id = client._settings_session_id = sid

    def test_reuses_http_connection_and_obeys_server_close(self):
        def respond(method, path, body, requests):
            return 200, {"value": {"ready": True}}, 0, path == "/close"

        with fake_server(respond) as (url, requests, ports):
            client = WDAClient(url)
            try:
                for path in ("/status", "/status", "/close", "/status"):
                    self.assertTrue(client.request("GET", path)["value"]["ready"])
                self.assertEqual(ports[:3], [ports[0]] * 3)
                self.assertNotEqual(ports[2], ports[3])
                self.assertEqual(len(requests), 4)
            finally:
                client.close()

    def test_explicit_invalid_session_read_creates_one_session_and_retries_once(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "fresh"}}, 0, False
            if path == "/session/expired/window/size":
                return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False
            return 200, {"value": {"width": 390, "height": 844}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                self.assertEqual(client.session("GET", "/window/size"), {"width": 390, "height": 844})
                self.assertEqual([(r[0], r[1]) for r in requests], [
                    ("GET", "/session/expired/window/size"), ("POST", "/session"),
                    ("POST", "/session/fresh/appium/settings"),
                    ("GET", "/session/fresh/window/size")])
                self.assertEqual(client.session_id, "fresh")
            finally:
                client.close()

    def test_explicit_invalid_session_mutation_is_not_replayed(self):
        def respond(method, path, body, requests):
            return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/wda/tap", {"x": 100, "y": 200})
                self.assertEqual(caught.exception.code, "invalid session id")
                self.assertFalse(caught.exception.uncertain)
                self.assertIsNone(client.session_id)
                self.assertEqual(len(requests), 1)
            finally:
                client.close()

    def test_expired_element_read_is_not_replayed_in_new_session(self):
        def respond(method, path, body, requests):
            return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("GET", "/element/expired-element/attribute/value")
                self.assertEqual(caught.exception.code, "invalid session id")
                self.assertIsNone(client.session_id)
                self.assertEqual([(method, path) for method, path, _ in requests], [
                    ("GET", "/session/expired/element/expired-element/attribute/value")])
            finally:
                client.close()

    def test_mutation_timeout_is_uncertain_and_never_replayed(self):
        def respond(method, path, body, requests):
            return 200, {"value": None}, 0.15, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url, timeout=0.025)
            self.warm_session(client, "existing")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/wda/tap", {"x": 100, "y": 200})
                self.assertEqual(caught.exception.code, "action_uncertain")
                self.assertTrue(caught.exception.uncertain)
                self.assertEqual(caught.exception.details["transport_error"], "pua_timeout")
                self.assertEqual(caught.exception.details["recovery"]["next_tool"], "pua_observe")
                self.assertFalse(caught.exception.details["recovery"]["replay_action"])
                self.assertIsNone(client.connection)
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.records[-1]["endpoint"], "/session/:id/wda/tap")
            finally:
                client.close()

    def test_read_timeout_is_not_replayed_and_a_later_read_can_recover(self):
        def respond(method, path, body, requests):
            return 200, {"value": "xml"}, 0.15 if len(requests) == 1 else 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url, timeout=0.025)
            try:
                with self.assertRaises(WDAError) as caught:
                    client.request("GET", "/source")
                error = caught.exception.as_dict()
                self.assertEqual(error["code"], "pua_timeout")
                self.assertFalse(error["uncertain"])
                self.assertEqual(error["transport_error"], "pua_timeout")
                self.assertEqual(error["recovery"]["next_tool"], "pua_observe")
                self.assertEqual(error["recovery"]["next_arguments"], {"mode": "screenshot"})
                self.assertGreater(error["recovery"]["retry_after_seconds"], 0)
                self.assertEqual(error["recovery"]["max_wait_retries"], 2)
                self.assertFalse(error["recovery"]["replay_action"])
                self.assertIsNone(client.connection)
                self.assertEqual(len(requests), 1)
                self.assertEqual(client.records[-1]["error"], "pua_timeout")
                self.assertEqual(client.request("GET", "/source", timeout=0.5)["value"], "xml")
                self.assertEqual(len(requests), 2)
            finally:
                client.close()

    def test_expired_deadline_is_a_timeout_without_a_network_request(self):
        client = WDAClient()
        with patch("wda_client.time.monotonic", return_value=100), \
                patch("wda_client.http.client.HTTPConnection") as connect, \
                self.assertRaises(WDAError) as caught:
            client.request("GET", "/source", timeout=0)
        self.assertEqual(caught.exception.code, "pua_timeout")
        self.assertFalse(caught.exception.uncertain)
        connect.assert_not_called()

    def test_body_reads_share_the_original_response_deadline(self):
        clock = [100.0]
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.isclosed.return_value = False
        def read_chunk(size):
            clock[0] += 0.04
            return b" "
        response.read1.side_effect = read_chunk
        client = WDAClient(timeout=0.08)
        with patch("wda_client.time.monotonic", side_effect=lambda: clock[0]), \
                patch("wda_client.http.client.HTTPConnection", return_value=connection) as connect, \
                self.assertRaises(WDAError) as caught:
            client.request("GET", "/source")
        self.assertEqual(caught.exception.code, "pua_timeout")
        self.assertEqual(response.read1.call_count, 2)
        connect.assert_called_once()
        connection.request.assert_called_once()
        connection.close.assert_called_once()
        self.assertIsNone(client.connection)

    def test_transport_failures_distinguish_timeout_disconnect_and_unknown(self):
        cases = [(socket.timeout("slow"), "pua_timeout"),
                 (OSError(errno.ETIMEDOUT, "slow"), "pua_timeout"),
                 (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "pua_unreachable"),
                 (ConnectionResetError(errno.ECONNRESET, "reset"), "pua_unreachable"),
                 (BrokenPipeError(errno.EPIPE, "closed"), "pua_unreachable"),
                 (http.client.RemoteDisconnected("closed"), "pua_unreachable"),
                 (http.client.IncompleteRead(b"partial", 10), "pua_unreachable"),
                 (OSError(errno.EIO, "unknown"), "pua_transport_error"),
                 (http.client.BadStatusLine("invalid"), "pua_transport_error")]
        for fault, kind in cases:
            for method in ("GET", "POST"):
                with self.subTest(fault=type(fault).__name__, method=method):
                    connection = Mock()
                    connection.request.side_effect = fault
                    client = WDAClient()
                    with patch("wda_client.http.client.HTTPConnection", return_value=connection), \
                            self.assertRaises(WDAError) as caught:
                        client.request(method, "/source" if method == "GET" else "/wda/tap", {})
                    error = caught.exception.as_dict()
                    self.assertEqual(error["code"], kind if method == "GET" else "action_uncertain")
                    self.assertEqual(error["transport_error"], kind)
                    self.assertEqual(error["uncertain"], method == "POST")
                    self.assertFalse(error["recovery"]["replay_action"])
                    self.assertEqual(error["recovery"]["next_tool"], "pua_observe" if kind == "pua_timeout" else "pua_setup")
                    self.assertEqual(connection.request.call_count, 1 if kind == "pua_timeout" or method == "POST" else 2)

    def test_non_session_read_error_does_not_trigger_retry(self):
        def respond(method, path, body, requests):
            return 500, {"value": {"error": "unknown error", "message": "Backend failed"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "existing")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("GET", "/source")
                self.assertEqual(caught.exception.code, "unknown error")
                self.assertEqual(client.session_id, "existing")
                self.assertEqual(len(requests), 1)
            finally:
                client.close()

    def test_element_lookup_reconnects_once_after_disconnect(self):
        def respond(method, path, body, requests):
            if len(requests) == 1:
                return None, None, 0, True
            return 200, {"value": [{"ELEMENT": "found"}]}, 0, False

        with fake_server(respond) as (url, requests, ports):
            client = WDAClient(url)
            self.warm_session(client, "existing")
            try:
                query = {"using": "accessibility id", "value": "按钮"}
                self.assertEqual(client.session("POST", "/elements", query), [{"ELEMENT": "found"}])
                self.assertEqual(requests, [("POST", "/session/existing/elements", query)] * 2)
                self.assertNotEqual(ports[0], ports[1])
                self.assertEqual(client.records[0]["error"], "pua_unreachable")
            finally:
                client.close()

    def test_get_reconnects_once_after_disconnect(self):
        def respond(method, path, body, requests):
            return (None, None, 0, True) if len(requests) == 1 else (200, {"value": "xml"}, 0, False)

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                self.assertEqual(client.request("GET", "/source")["value"], "xml")
                self.assertEqual(len(requests), 2)
            finally:
                client.close()

    def test_element_lookup_invalid_session_recreates_and_retries_query_once(self):
        def respond(method, path, body, requests):
            if path == "/session/expired/elements":
                return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False
            if path == "/session":
                return 200, {"value": {"sessionId": "fresh"}}, 0, False
            return 200, {"value": [{"ELEMENT": "fresh-element"}]}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                self.assertEqual(client.session("POST", "/elements", {"using": "name", "value": "A"}),
                                 [{"ELEMENT": "fresh-element"}])
                self.assertEqual([path for _, path, _ in requests], [
                    "/session/expired/elements", "/session", "/session/fresh/appium/settings", "/session/fresh/elements"])
            finally:
                client.close()

    def test_invalid_session_lookup_is_retried_only_once(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "fresh"}}, 0, False
            if path.endswith("/appium/settings"):
                return 200, {"value": {}}, 0, False
            return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/elements", {})
                self.assertFalse(caught.exception.uncertain)
                self.assertEqual(len(requests), 4)
                self.assertEqual(sum(path == "/session" for _, path, _ in requests), 1)
                self.assertIsNone(client.session_id)
            finally:
                client.close()

    def test_descendant_lookup_does_not_carry_old_element_across_sessions(self):
        def respond(method, path, body, requests):
            return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "expired")
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/element/old/elements", {})
                self.assertFalse(caught.exception.uncertain)
                self.assertIsNone(client.session_id)
                self.assertEqual(len(requests), 1)
            finally:
                client.close()

    def test_read_reconnect_does_not_reset_total_deadline(self):
        # CI scheduling can consume an 80ms wall-clock budget before a second
        # socket is accepted. Reconnect itself is covered by real HTTP fixtures;
        # use a controlled clock here to test the remaining-budget contract.
        clock = [100.0]
        budgets = []
        client = WDAClient(timeout=0.08)
        self.warm_session(client, "existing")
        def interrupted_read(method, path, payload, budget, readonly):
            self.assertTrue(readonly)
            budgets.append(budget)
            clock[0] += 0.025 if len(budgets) == 1 else budget
            raise WDAError("pua_unreachable", "Read connection interrupted")
        with patch("wda_client.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(client, "_request_once", side_effect=interrupted_read), \
                self.assertRaises(WDAError) as caught:
            client.session("POST", "/elements", {})
        self.assertEqual(caught.exception.code, "pua_unreachable")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(budgets), 2)
        self.assertAlmostEqual(budgets[0], 0.08)
        self.assertAlmostEqual(budgets[1], 0.055)
        self.assertAlmostEqual(clock[0] - 100, 0.08)

    def test_backend_and_invalid_json_query_failures_are_not_retried_or_uncertain(self):
        for response, code in (({"value": {"error": "unknown error", "message": "Failed"}}, "unknown error"),
                               (b"not json", "invalid_response"), ({}, "http_error")):
            with self.subTest(code=code):
                def respond(method, path, body, requests):
                    return 500, response, 0, False
                with fake_server(respond) as (url, requests, _):
                    client = WDAClient(url)
                    self.warm_session(client, "existing")
                    try:
                        with self.assertRaises(WDAError) as caught:
                            client.session("POST", "/elements", {})
                        self.assertEqual(caught.exception.code, code)
                        self.assertFalse(caught.exception.uncertain)
                        self.assertEqual(len(requests), 1)
                    finally:
                        client.close()

    def test_phone_mutations_never_reconnect_and_replay_after_disconnect(self):
        for path in ("/element/button/click", "/element/input/value", "/wda/keys",
                     "/wda/homescreen", "/wda/apps/activate", "/wda/dragfromtoforduration"):
            with self.subTest(path=path):
                def respond(method, path, body, requests):
                    return None, None, 0, True
                with fake_server(respond) as (url, requests, _):
                    client = WDAClient(url)
                    self.warm_session(client, "existing")
                    try:
                        with self.assertRaises(WDAError) as caught:
                            client.session("POST", path, {})
                        self.assertTrue(caught.exception.uncertain)
                        self.assertEqual(caught.exception.code, "action_uncertain")
                        self.assertEqual(caught.exception.details["transport_error"], "pua_unreachable")
                        self.assertEqual(caught.exception.details["recovery"]["next_tool"], "pua_setup")
                        self.assertEqual(len(requests), 1)
                    finally:
                        client.close()

    def test_upstream_state_changing_healthcheck_is_not_replayed(self):
        def respond(method, path, body, requests):
            return None, None, 0, True

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                with self.assertRaises(WDAError) as caught:
                    client.request("GET", "/wda/healthcheck")
                self.assertTrue(caught.exception.uncertain)
                self.assertEqual(len(requests), 1)
            finally:
                client.close()

    def test_new_session_sets_fast_timeouts_once(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "fresh"}}, 0, False
            return 200, {"value": {}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                self.assertEqual(client.ensure_session(), "fresh")
                self.assertEqual(client.ensure_session(), "fresh")
                client.session("GET", "/source")
                self.assertEqual([path for _, path, _ in requests], [
                    "/session", "/session/fresh/appium/settings", "/session/fresh/source"])
                self.assertEqual(requests[0][2]["capabilities"]["alwaysMatch"]["waitForIdleTimeout"], 0)
                self.assertEqual(requests[1][2], {"settings": SESSION_SETTINGS})
                self.assertEqual((SESSION_SETTINGS["waitForIdleTimeout"], SESSION_SETTINGS["animationCoolOffTimeout"]), (0, 0))
                # Queries return rect and type inline, so target selection needs no follow-up reads.
                self.assertIs(SESSION_SETTINGS["shouldUseCompactResponses"], False)
                self.assertEqual(set(SESSION_SETTINGS["elementResponseAttributes"].split(",")),
                                 {"type", "label", "rect", "enabled", "attribute/name", "attribute/value"})
            finally:
                client.close()

    def test_persisted_session_sets_fast_timeouts_once_without_session_creation(self):
        def respond(method, path, body, requests):
            return 200, {"value": {}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            client.session_id = "persisted"
            try:
                self.assertEqual(client.ensure_session(), "persisted")
                client.session("GET", "/source")
                client.session("GET", "/source")
                self.assertEqual([path for _, path, _ in requests], [
                    "/session/persisted/appium/settings", "/session/persisted/source", "/session/persisted/source"])
                self.assertEqual(requests[0][2], {"settings": SESSION_SETTINGS})
            finally:
                client.close()

    def test_persisted_expired_session_settings_recovery_retries_only_queries(self):
        for method, path in (("POST", "/elements"), ("POST", "/wda/tap")):
            with self.subTest(method=method, path=path):
                def respond(method, path, body, requests):
                    if path == "/session/persisted/appium/settings":
                        return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False
                    if path == "/session":
                        return 200, {"value": {"sessionId": "fresh"}}, 0, False
                    return 200, {"value": []}, 0, False
                with fake_server(respond) as (url, requests, _):
                    client = WDAClient(url)
                    client.session_id = "persisted"
                    try:
                        if path == "/elements":
                            self.assertEqual(client.session(method, path, {}), [])
                            self.assertEqual([route for _, route, _ in requests], [
                                "/session/persisted/appium/settings", "/session", "/session/fresh/appium/settings", "/session/fresh/elements"])
                        else:
                            with self.assertRaises(WDAError):
                                client.session(method, path, {})
                            self.assertIsNone(client.session_id)
                            self.assertEqual(len(requests), 1)
                    finally:
                        client.close()

    def test_settings_backend_failure_keeps_session_and_is_not_replayed(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "created"}}, 0, False
            return 500, {"value": {"error": "unknown error", "message": "Settings rejected"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                for _ in range(2):
                    with self.assertRaises(WDAError) as caught:
                        client.ensure_session()
                    self.assertEqual(caught.exception.code, "unknown error")
                    self.assertEqual(caught.exception.details["operation"], "session_settings")
                    self.assertTrue(caught.exception.details["session_created"])
                    self.assertFalse(caught.exception.details["settings_applied"])
                    self.assertFalse(caught.exception.details["settings_replayed"])
                self.assertEqual(client.session_id, "created")
                self.assertEqual(len(requests), 2)
            finally:
                client.close()

    def test_settings_disconnect_reapplies_only_on_a_later_call(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "created"}}, 0, False
            if len(requests) == 2:
                return None, None, 0, True
            return 200, {"value": {}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/wda/tap", {"x": 100, "y": 200})
                self.assertEqual(caught.exception.details["transport_error"], "pua_unreachable")
                self.assertEqual(caught.exception.details["recovery"]["next_tool"], "pua_setup")
                self.assertFalse(caught.exception.details["action_executed"])
                self.assertEqual(client.session_id, "created")
                self.assertEqual(len(requests), 2)
                self.assertEqual(client.ensure_session(), "created")
                self.assertIsNone(client._settings_error)
                self.assertEqual([path for _, path, _ in requests], [
                    "/session", "/session/created/appium/settings", "/session/created/appium/settings"])
            finally:
                client.close()

    def test_settings_timeout_recovers_on_a_later_call_without_recreating_session_or_action(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "created"}}, 0, False
            first_settings = path.endswith("/appium/settings") and sum(route == path for _, route, _ in requests) == 1
            return 200, {"value": "xml"}, 0.15 if first_settings else 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url, timeout=0.025)
            try:
                with self.assertRaises(WDAError) as caught:
                    client.session("POST", "/wda/tap", {"x": 100, "y": 200})
                self.assertEqual(caught.exception.details["transport_error"], "pua_timeout")
                self.assertEqual(caught.exception.details["operation"], "session_settings")
                self.assertFalse(caught.exception.details["action_executed"])
                self.assertEqual(client.session_id, "created")
                self.assertIsNone(client._settings_session_id)
                self.assertEqual([path for _, path, _ in requests], ["/session", "/session/created/appium/settings"])
                self.assertEqual(client.session("GET", "/source", timeout=0.5), "xml")
                self.assertIsNone(client._settings_error)
                self.assertEqual([path for _, path, _ in requests], [
                    "/session", "/session/created/appium/settings", "/session/created/appium/settings", "/session/created/source"])
            finally:
                client.close()

    def test_settings_explicit_invalid_session_clears_real_session_state(self):
        def respond(method, path, body, requests):
            if path == "/session":
                return 200, {"value": {"sessionId": "created"}}, 0, False
            return 404, {"value": {"error": "invalid session id", "message": "Expired"}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            try:
                with self.assertRaises(WDAError) as caught:
                    client.ensure_session()
                self.assertEqual(caught.exception.code, "invalid session id")
                self.assertIsNone(client.session_id)
                self.assertEqual(len(requests), 2)
            finally:
                client.close()

    def test_deferred_snapshot_restore_precedes_raw_and_session_queries(self):
        for method, path, session in (("GET", "/wda/activeAppInfo", False),
                                      ("GET", "/screenshot", False),
                                      ("GET", "/source", True),
                                      ("POST", "/wda/tap", True)):
            with self.subTest(path=path):
                def respond(method, route, body, requests):
                    return 200, {"value": {}}, 0, False

                with fake_server(respond) as (url, requests, _):
                    client = WDAClient(url)
                    self.warm_session(client, "existing")
                    try:
                        client.reapply_settings({"snapshotMaxDepth": 42, "snapshotMaxChildren": 900})
                        self.assertEqual(requests, [])
                        call = client.session if session else client.request
                        call(method, path, {"x": 100, "y": 200} if method == "POST" else None)
                        self.assertEqual(requests[0], ("POST", "/session/existing/appium/settings",
                            {"settings": {**SESSION_SETTINGS, "snapshotMaxDepth": 42, "snapshotMaxChildren": 900}}))
                        self.assertEqual(len(requests), 2)
                        self.assertEqual(client._settings_restore, {})
                    finally:
                        client.close()

    def test_status_does_not_flush_deferred_snapshot_restore(self):
        def respond(method, path, body, requests):
            return 200, {"value": {"ready": True}}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "existing")
            try:
                client.reapply_settings({"snapshotMaxChildren": 1000})
                self.assertTrue(client.request("GET", "/status")["value"]["ready"])
                self.assertEqual([path for _, path, _ in requests], ["/status"])
                self.assertEqual(client._settings_restore, {"snapshotMaxChildren": 1000})
            finally:
                client.close()

    def test_failed_deferred_restore_blocks_queries_and_remains_pending(self):
        def respond(method, path, body, requests):
            return 200, {"value": {}}, 0.15 if len(requests) == 1 else 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url, timeout=0.025)
            self.warm_session(client, "existing")
            try:
                restore = {"snapshotMaxChildren": 1000}
                client.reapply_settings(restore)
                with self.assertRaises(WDAError) as caught:
                    client.request("GET", "/wda/activeAppInfo")
                self.assertFalse(caught.exception.details["action_executed"])
                self.assertEqual(caught.exception.details["transport_error"], "pua_timeout")
                self.assertEqual(client._settings_restore, restore)
                self.assertEqual(len(requests), 1)
                client.request("GET", "/wda/activeAppInfo", timeout=0.5)
                self.assertEqual([path for _, path, _ in requests], [
                    "/session/existing/appium/settings", "/session/existing/appium/settings", "/wda/activeAppInfo"])
                self.assertEqual(client._settings_restore, {})
            finally:
                client.close()

    def test_utf8_payload_and_metrics_do_not_retain_user_content_or_ids(self):
        phrase = "你好 👋 Café 漢字"

        def respond(method, path, body, requests):
            return 200, {"value": phrase}, 0, False

        with fake_server(respond) as (url, requests, _):
            client = WDAClient(url)
            self.warm_session(client, "private-device-session")
            try:
                value = client.session("POST", "/element/private-element/value", {"text": phrase})
                self.assertEqual(value, phrase)
                self.assertEqual(requests[0][2]["text"], phrase)
                metrics = json.dumps(client.metrics(), ensure_ascii=False)
                for sensitive in (phrase, "private-device-session", "private-element"):
                    self.assertNotIn(sensitive, metrics)
                self.assertIn("/session/:id/element/:id/value", metrics)
            finally:
                client.close()

    def test_rejects_remote_or_decorated_urls(self):
        for url in ("http://192.168.1.2:8100", "https://localhost:8100", "http://user@localhost", "http://localhost/api", "http://localhost?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WDAClient(url)


if __name__ == "__main__":
    unittest.main()
