"""Direct, persistent PUA HTTP transport. No Appium server, no action replay."""
import collections
import errno
import http.client
import json
import socket
import time
from urllib.parse import urlsplit

import preview_fast

# One element query answers with the fields target selection needs, so it replaces
# separate rect and type reads. Both keys are handled by the pinned WDA's settings.
ELEMENT_RESPONSE_ATTRIBUTES = "type,label,rect,enabled,attribute/name,attribute/value"
SESSION_SETTINGS = {"waitForIdleTimeout": 0, "animationCoolOffTimeout": 0,
                    "snapshotMaxDepth": 50, "snapshotMaxChildren": 2147483647,
                    "shouldUseCompactResponses": False,
                    "elementResponseAttributes": ELEMENT_RESPONSE_ATTRIBUTES,
                    # Empty unless IPHONE_USE_FAST_PREVIEW is set, so the classic preview
                    # keeps whatever stream settings the runner already has.
                    **preview_fast.stream_settings()}


class WDAError(Exception):
    def __init__(self, code, message, uncertain=False, details=None):
        super().__init__(message)
        self.code, self.uncertain, self.details = code, uncertain, details or {}

    def as_dict(self):
        return {"code": self.code, "message": str(self), "uncertain": self.uncertain, **self.details}


class WDAClient:
    def __init__(self, base_url="http://127.0.0.1:18100", timeout=15):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path not in ("", "/") or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("PUA URL must be a local HTTP endpoint; forward USB to loopback first.")
        self.host, self.port = parsed.hostname, parsed.port or 80
        self.timeout, self.connection, self.session_id = timeout, None, None
        self._settings_session_id, self._settings_error = None, None
        self._settings_restore = {}
        self.records = collections.deque(maxlen=2000)

    def close(self):
        if self.connection:
            self.connection.close()
        self.connection = None

    @staticmethod
    def is_read(method, path):
        """PUA's element lookup uses POST, but does not execute a phone action."""
        route = path.split("?", 1)[0]
        if route.startswith("/session/"):
            route = "/" + "/".join(route.split("/")[3:])
        if method == "GET":
            # The upstream labels healthcheck as state-changing on simulators.
            return route != "/wda/healthcheck"
        if method != "POST":
            return False
        parts = route.strip("/").split("/")
        return parts in (["element"], ["elements"]) or (
            len(parts) == 3 and parts[0] == "element" and parts[2] in ("element", "elements"))

    @staticmethod
    def transport_error(exc, readonly):
        if isinstance(exc, (socket.timeout, TimeoutError)) or isinstance(exc, OSError) and exc.errno == errno.ETIMEDOUT:
            kind = "pua_timeout"
            message = "PUA request timed out; this does not prove the phone or service disconnected."
            recovery = {"retry_after_seconds": 2, "max_wait_retries": 2,
                        "next_tool": "pua_observe", "next_arguments": {"mode": "screenshot"},
                        "replay_action": False,
                        "next_step": "Wait on the host before one fresh observation; do not use pua_wait or immediately repeat a tree query. After at most two delayed attempts, inspect pua_setup(action=status) diagnostically. A repeated timeout alone does not authorize restart."}
        elif isinstance(exc, (ConnectionError, http.client.RemoteDisconnected, http.client.IncompleteRead)) or (
                isinstance(exc, OSError) and exc.errno in (errno.ENOTCONN, errno.ENETUNREACH, errno.EHOSTUNREACH)):
            kind = "pua_unreachable"
            message = "PUA HTTP connection was refused or interrupted; this does not prove USB was unplugged."
            recovery = {"next_tool": "pua_setup", "next_arguments": {"action": "status"},
                        "replay_action": False,
                        "next_step": "Inspect service and jobs; reuse an active start/recovery job, or start once from the existing configuration/build only if the service is confirmed stopped and startup is allowed. Verify READY before continuing, then inspect actual state without replaying the failed action."}
        else:
            kind = "pua_transport_error"
            message = "PUA HTTP transport failed without a confirmed timeout or disconnect."
            recovery = {"next_tool": "pua_setup", "next_arguments": {"action": "status"},
                        "replay_action": False,
                        "next_step": "Inspect channel status diagnostically; do not infer disconnection or restart solely from this error. Read actual state before continuing any uncertain action."}
        message += (" No phone action was executed by this read." if readonly
                    else " The write request may have completed; inspect fresh state before deciding what remains. Never blindly replay phone actions.")
        return WDAError(kind if readonly else "action_uncertain", message, uncertain=not readonly,
                        details={"transport_error": kind, "recovery": recovery})

    @staticmethod
    def remaining(deadline):
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise WDAClient.transport_error(TimeoutError("PUA request deadline expired"), readonly=True)
        return budget

    def request(self, method, path, payload=None, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        # A timed-out bounded snapshot must not leave subsequent AX queries capped.
        # Health/status can still diagnose the channel without waiting for settings.
        if self._settings_restore and path not in ("/status", "/session") and not path.endswith("/appium/settings"):
            self.ensure_session(timeout=self.remaining(deadline))
        readonly = self.is_read(method, path)
        for attempt in range(2 if readonly else 1):
            try:
                return self._request_once(method, path, payload, self.remaining(deadline), readonly)
            except WDAError as exc:
                # Reconnect once only for transport failures, within the original budget.
                # HTTP/JSON/backend failures do not become silent retry loops.
                if not (readonly and attempt == 0 and exc.code in ("pua_unreachable", "pua_transport_error")
                        and time.monotonic() < deadline):
                    raise

    def _request_once(self, method, path, payload, budget, readonly):
        started = time.monotonic()
        deadline = started + budget
        status, code, received = None, None, 0
        try:
            if self.connection is None:
                self.connection = http.client.HTTPConnection(self.host, self.port, timeout=budget)
            self.connection.timeout = budget
            if self.connection.sock:
                self.connection.sock.settimeout(budget)
            body = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.connection.request(method, path, body, {"Content-Type": "application/json", "Accept": "application/json"})
            network_socket = self.connection.sock
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise socket.timeout("PUA response deadline expired")
            if network_socket:
                network_socket.settimeout(remaining)
            response = self.connection.getresponse()
            status = response.status
            # read1 returns available buffered bytes instead of waiting for a full
            # body. Resetting the socket timeout to the remaining budget bounds
            # slow/chunked responses too, rather than granting each receive anew.
            chunks, size, limit = [], 0, 24 * 1024 * 1024 + 1
            while size < limit and not response.isclosed():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise socket.timeout("PUA response deadline expired")
                if network_socket:
                    network_socket.settimeout(remaining)
                chunk = response.read1(min(65536, limit-size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
            received = len(raw)
            # Python 3.9's read1 leaves fp open when Content-Length reaches zero.
            # Mark the completed response closed so HTTPConnection can reuse its socket.
            response.close()
            if len(raw) > 24 * 1024 * 1024:
                raise WDAError("response_too_large", "PUA response exceeds 24 MiB.", uncertain=not readonly)
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise WDAError("invalid_response", "PUA returned invalid JSON.", uncertain=not readonly) from exc
            if not isinstance(result, dict):
                raise WDAError("invalid_response", "PUA returned a non-object.", uncertain=not readonly)
            value = result.get("value")
            if isinstance(value, dict) and value.get("error"):
                code = value["error"]
                raise WDAError(code, value.get("message", code)[:1500])
            if status >= 400:
                raise WDAError("http_error", f"PUA returned HTTP {status}.", uncertain=not readonly)
            return result
        except WDAError as exc:
            code = exc.code
            if exc.uncertain or exc.code == "response_too_large":
                self.close()
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            self.close()
            error = self.transport_error(exc, readonly)
            code = error.code
            raise error from exc
        finally:
            # No text, predicates, values, screenshots, app IDs or device identifiers in metrics.
            endpoint = path.split("?")[0]
            if endpoint.startswith("/session/"):
                endpoint = "/session/:id/" + "/".join(endpoint.split("/")[3:])
            if "/element/" in endpoint:
                parts = endpoint.split("/")
                idx = parts.index("element") + 1
                if idx < len(parts):
                    parts[idx] = ":id"
                endpoint = "/".join(parts)
            self.records.append({"method": method, "endpoint": endpoint, "seconds": round(time.monotonic()-started, 4),
                                 "status": status, "error": code, "bytes": received})

    def ensure_session(self, timeout=None):
        deadline = time.monotonic() + (30 if timeout is None else timeout)
        created = False
        if not self.session_id:
            self._settings_session_id, self._settings_error = None, None
            result = self.request("POST", "/session", {"capabilities": {"alwaysMatch": {
                "shouldWaitForQuiescence": False, "shouldTerminateApp": False,
                "waitForIdleTimeout": 0, "maxTypingFrequency": 30}}}, timeout=self.remaining(deadline))
            value = result.get("value") or {}
            self.session_id = result.get("sessionId") or value.get("sessionId")
            if not self.session_id:
                raise WDAError("invalid_response", "PUA did not return a session ID.")
            created = True
        if self.session_id != self._settings_session_id:
            # These settings are supported by the pinned WDA's FBSettingsHandler.
            # Configure once for both newly created and persisted sessions adopted
            # by this client. Never add a settings request to each phone action.
            self._settings_session_id, self._settings_error = self.session_id, None
            try:
                self.request("POST", f"/session/{self.session_id}/appium/settings", {
                    "settings": {**SESSION_SETTINGS, **self._settings_restore}}, timeout=self.remaining(deadline))
                self._settings_restore.clear()
            except WDAError as exc:
                exc.details.update({"operation": "session_settings", "session_created": created,
                                    "session_available": exc.code != "invalid session id",
                                    "settings_applied": False, "settings_replayed": False})
                if exc.code == "invalid session id":
                    self.session_id = None
                self._settings_error = exc
                if exc.details.get("transport_error", exc.code) in ("pua_timeout", "pua_unreachable", "pua_transport_error"):
                    # A later call may reapply idempotent settings, never a phone action.
                    self._settings_session_id = None
                    exc.details["action_executed"] = False
                raise
        elif self.session_id == self._settings_session_id and self._settings_error:
            # Keep the real session ID for backend settings failures; an explicit
            # reset can recover them without pretending session creation failed.
            raise self._settings_error
        return self.session_id

    def session(self, method, path, payload=None, timeout=None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        for attempt in range(2):
            try:
                sid = self.ensure_session(timeout=self.remaining(deadline))
                return self.request(method, f"/session/{sid}{path}", payload, self.remaining(deadline)).get("value")
            except WDAError as exc:
                if exc.code == "invalid session id":
                    self.session_id = None
                    # An invalid-session query did not execute. Old element IDs cannot
                    # be used in a fresh session even though their lookup is a read.
                    if attempt == 0 and self.is_read(method, path) and not path.startswith("/element/"):
                        continue
                raise

    def reapply_settings(self, restore=None):
        """Send the session settings again before the next command."""
        if restore:self._settings_restore.update(restore)
        self._settings_session_id, self._settings_error = None, None

    def clear_metrics(self):
        self.records.clear()

    def metrics(self):
        endpoints = collections.defaultdict(list)
        for record in self.records:
            endpoints[record["endpoint"]].append(record)
        return {"retained_requests": len(self.records), "http_seconds": round(sum(r["seconds"] for r in self.records),4),
                "http_bytes": sum(r["bytes"] for r in self.records),
                "errors": sum(bool(r["error"]) for r in self.records),
                "endpoints": {k: {"count": len(v), "seconds": round(sum(r["seconds"] for r in v),4),
                                  "max_seconds": max(r["seconds"] for r in v), "bytes": sum(r["bytes"] for r in v)}
                              for k,v in endpoints.items()},
                "measurement": "Transport time and response bytes only; model, host scheduling and user wait are outside this process."}
