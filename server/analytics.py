"""Anonymous, bounded PostHog delivery using only the Python standard library.

Never pass phone arguments, observations or exception messages to capture().
Failures here must never change a phone operation or write to MCP stdout.
"""
from datetime import datetime, timezone
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import platform
import queue
import threading
import time
import uuid

HOST = "us.i.posthog.com"
EVENTS = frozenset(("iphone_use_session_started", "iphone_use_tool_called",
                    "iphone_use_ready_result", "iphone_use_setup_result", "iphone_use_screen_action"))
TOOLS = frozenset("pua_" + op for op in (
    "doctor", "setup", "ready", "metrics", "observe", "find", "tap", "swipe", "type_text",
    "press_button", "launch_app", "wait", "scroll_find", "collect_list", "apps", "batch", "jev", "screen", "screen_action"))
ERRORS = frozenset((
    "ambiguous_target", "clipboard_unavailable", "device_busy", "http_error",
    "input_continuation_expired", "input_mismatch", "invalid_argument", "invalid_response",
    "invalid_selector", "mirroring_conflict", "newline_requires_intent", "newline_unsafe",
    "no_focused_field", "no_scroll_progress", "no_such_element", "not_editable", "not_ready",
    "occluded_target", "offscreen_target", "phone_locked", "postcondition_failed", "preview_paused",
    "pua_foreground_unavailable", "pua_recovery_required", "pua_unreachable", "response_too_large",
    "scroll_context_changed", "search_exhausted", "stale_observation", "unknown_tool", "internal_error"))
ENUMS = {
    "tool_name": TOOLS,
    "outcome": {"success", "error", "pending"},
    "error_code": ERRORS | {"other", "none"},
    "ready_state": {"ready", "recovering", "recovery_required", "not_ready", "error"},
    "setup_action": {"discover", "fetch", "configure", "build", "start", "stop", "status"},
    "setup_state": {"success", "queued", "running", "completed", "failed", "error", "other"},
    "screen_action": {"open", "pause", "resume", "refresh", "home", "screenshot"},
}
NUMBERS = {"duration_ms": 3600000, "batch_size": 20}


def safe_properties(values):
    """Allowlist even internal callers: unknown fields and free text are discarded."""
    result = {}
    for key, value in values.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
        elif key in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = round(max(0, min(value, NUMBERS[key])), 2)
    return result


def project_token():
    override = os.environ.get("IPHONE_USE_POSTHOG_PROJECT_TOKEN")
    if override is not None:
        return override.strip()
    for name in ("posthog.local.json", "posthog.json"):
        try:
            token = json.loads(Path(__file__).with_name(name).read_text()).get("projectToken")
            if isinstance(token, str) and token.strip():
                return token.strip()
        except (OSError, ValueError, AttributeError):
            pass
    return ""


def opted_out():
    return (os.environ.get("IPHONE_USE_ANALYTICS", "").lower() in ("0", "false", "off", "no")
            or os.environ.get("DO_NOT_TRACK", "").lower() in ("1", "true"))


def environment():
    value = os.environ.get("IPHONE_USE_ANALYTICS_ENV", "production")
    return value if value in ("production", "development", "test") else "production"


def anonymous_id(state_dir):
    """Persist a random installation ID across MCP processes, never a device ID."""
    directory = Path(state_dir)
    with (directory / "analytics.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "analytics-id"
        try:
            return str(uuid.UUID(path.read_text().strip()))
        except (OSError, ValueError):
            ident = str(uuid.uuid4())
            temporary = directory / ("analytics-" + str(uuid.uuid4()) + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write(ident)
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return ident


def send_batch(token, events):
    body = json.dumps({"api_key": token, "batch": events}, allow_nan=False).encode()
    connection = http.client.HTTPSConnection(HOST, timeout=3)
    try:
        connection.request("POST", "/batch/", body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        return response.status
    finally:
        connection.close()


class Analytics:
    def __init__(self, state_dir, version, sender=None, token=None):
        self.state_dir, self.version = Path(state_dir), version
        self.token = project_token() if token is None else token
        self.sender = sender or send_batch
        self.ident = None
        self.session_id = str(uuid.uuid4())
        self.queue = queue.Queue(maxsize=256)
        self.lock = threading.Lock()
        self.worker = None
        self.started = False
        self.closed = False

    def capture(self, event, **properties):
        if opted_out() or not self.token or event not in EVENTS:
            return False
        try:
            with self.lock:
                if self.closed:
                    return False
                if self.ident is None:
                    self.ident = anonymous_id(self.state_dir)
                self.queue.put_nowait({
                    "event": event, "distinct_id": self.ident, "uuid": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "properties": {
                        "app_name": "iphone-use", "app_version": self.version,
                        "app_surface": "local_mcp", "source": "codex_plugin_mcp",
                        "$lib": "iphone-use-mcp", "$process_person_profile": False,
                        "$geoip_disable": True, "session_id": self.session_id,
                        "platform": platform.system(), "environment": environment(), **safe_properties(properties),
                    },
                })
                if self.worker is None:
                    self.worker = threading.Thread(target=self._deliver, name="iphone-use-analytics", daemon=True)
                    self.worker.start()
            return True
        except Exception:
            return False

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
        self.capture("iphone_use_session_started")

    def _deliver(self):
        while True:
            try:
                first = self.queue.get(timeout=0.1)
            except queue.Empty:
                if self.closed:
                    return
                continue
            batch = [first]
            while len(batch) < 20:
                try:
                    batch.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            for attempt in range(2):
                if opted_out():
                    break
                try:
                    status = self.sender(self.token, batch)
                    if 200 <= status < 300 or (status < 500 and status != 429):
                        break
                except Exception:
                    pass
                if attempt == 0 and not self.closed:
                    time.sleep(0.2)
                else:
                    break
            for _ in batch:
                self.queue.task_done()

    def tool_result(self, name, args, result, duration_ms, error=None):
        if name not in TOOLS:
            return
        self.start()
        data = result if isinstance(result, dict) else {}
        failure = error or data.get("error")
        if isinstance(failure, dict):
            failure = failure.get("code", "other")
        outcome = "error" if failure or data.get("ok") is False else "success"
        if outcome == "success" and (data.get("ready") is False or data.get("input_complete") is False or data.get("complete") is False
                                      or data.get("state") in ("queued", "running", "recovering")):
            outcome = "pending"
        code = failure if isinstance(failure, str) and failure in ERRORS else ("other" if outcome == "error" else "none")
        fields = {"tool_name": name, "outcome": outcome, "error_code": code, "duration_ms": duration_ms}
        # Only inspect explicitly bounded controls; never serialize args or result.
        controls = args if isinstance(args, dict) else {}
        if name == "pua_batch" and isinstance(controls.get("steps"), list):
            fields["batch_size"] = len(controls["steps"])
        self.capture("iphone_use_tool_called", **fields)
        if name == "pua_ready":
            state = "error" if outcome == "error" else ("ready" if data.get("ready") is True else data.get("state", "not_ready"))
            fields["ready_state"] = state if state in ENUMS["ready_state"] else "not_ready"
            self.capture("iphone_use_ready_result", **fields)
        elif name == "pua_setup":
            state = "error" if outcome == "error" else data.get("state", "success")
            self.capture("iphone_use_setup_result", **fields, setup_action=controls.get("action"),
                         setup_state=state if state in ENUMS["setup_state"] else "other")
        elif name in ("pua_screen", "pua_screen_action"):
            self.capture("iphone_use_screen_action", **fields, screen_action=controls.get("action", "open"))

    def close(self, timeout=1.5):
        with self.lock:
            self.closed = True
        if self.worker is not None:
            self.worker.join(timeout=timeout)
