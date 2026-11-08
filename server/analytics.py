"""Anonymous, bounded PostHog and GA4 delivery using the standard library.

Only explicit launch targets may leave phone arguments; never send observations
or exception messages to capture().
Failures here must never change a phone operation or write to MCP stdout.
"""
from datetime import datetime, timezone
from functools import lru_cache
import fcntl
import http.client
import json
import math
import os
from pathlib import Path
import platform
import queue
import re
import threading
import time
import uuid
from urllib.parse import urlencode

HOST = "us.i.posthog.com"
EVENTS = frozenset(("iphone_use_session_started", "iphone_use_tool_called", "iphone_use_app_launched"))
TOOLS = frozenset("pua_" + op for op in (
    "doctor", "setup", "ready", "metrics", "observe", "find", "tap", "swipe", "type_text",
    "press_button", "screenshot", "launch_app", "wait", "scroll_find", "collect_list", "apps", "batch", "screen", "screen_action"))
ERRORS = frozenset((
    "ambiguous_target", "clipboard_unavailable", "device_busy", "http_error",
    "input_continuation_expired", "input_mismatch", "invalid_argument", "invalid_response",
    "invalid_selector", "mirroring_conflict", "newline_requires_intent", "newline_unsafe",
    "no_focused_field", "no_scroll_progress", "no_such_element", "not_editable", "not_ready",
    "occluded_target", "offscreen_target", "phone_locked", "postcondition_failed", "preview_paused",
    "pua_foreground_unavailable", "pua_recovery_required", "pua_timeout", "pua_transport_error", "pua_unreachable", "response_too_large",
    "scroll_context_changed", "search_exhausted", "stale_observation", "unknown_tool", "internal_error"))
ENUMS = {
    "tool_name": TOOLS,
    "outcome": {"success", "error", "pending"},
    "error_code": ERRORS | {"other", "none"},
    "ready_state": {"ready", "recovering", "recovery_required", "not_ready", "error"},
    "setup_action": {"discover", "fetch", "configure", "build", "start", "stop", "status"},
    "setup_state": {"success", "queued", "running", "completed", "failed", "error", "other"},
    "screen_action": {"open", "pause", "resume", "refresh", "home", "screenshot", "tap", "drag"},
    "launch_source": {"direct", "batch"},
    "foreground_verified": {"yes", "no"},
}
NUMBERS = {"duration_ms": 3600000, "batch_size": 20}


@lru_cache(maxsize=1)
def app_names():
    """Names come only from the bundled public catalog, never device content."""
    try:
        catalog = Path(__file__).resolve().parent.parent / "skills/iphone-use/references/apps.json"
        return {app["bundleId"]: app["name"][:100] for app in json.loads(catalog.read_text())["apps"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def launch_target(bundle_id):
    # GA4 standard-property string values are limited to 100 characters. Drop
    # invalid/oversized IDs rather than truncate and merge unrelated apps.
    if (not isinstance(bundle_id, str) or len(bundle_id) > 100
            or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", bundle_id)):
        return {}
    return {"target_app_bundle_id": bundle_id,
            "target_app_name": app_names().get(bundle_id, bundle_id)}


def ga4_config():
    config = {}
    for name in ("ga4.json", "ga4.local.json"):
        try:
            value = json.loads(Path(__file__).with_name(name).read_text())
            if isinstance(value, dict):
                config.update(value)
        except (OSError, ValueError):
            pass
    for key, variable in (("measurementId", "IPHONE_USE_GA4_MEASUREMENT_ID"),
                          ("apiSecret", "IPHONE_USE_GA4_API_SECRET")):
        if variable in os.environ:
            config[key] = os.environ[variable].strip()
    if not all(isinstance(config.get(key), str) and config[key].strip()
               for key in ("measurementId", "apiSecret")):
        return {}
    return {key: config[key].strip() for key in ("measurementId", "apiSecret")}


def ga4_payload(events):
    """Translate the same sanitized events; never copy arbitrary PostHog fields."""
    ident = uuid.UUID(events[0]["distinct_id"]).int
    result = []
    for event in events:
        source = event["properties"]
        params = {key: source[key] for key in (
            "app_name", "app_version", "app_surface", "source", "platform",
            "environment", "analytics_schema_version")}
        params.update(safe_properties(source))
        params["active_use"] = "yes" if (
            event["event"] == "iphone_use_tool_called" and params.get("outcome") == "success"
            and params.get("tool_name") in TOOLS - {"pua_doctor", "pua_setup", "pua_metrics", "pua_screen", "pua_screen_action"}
        ) else "no"
        params.update(
            session_id=int(uuid.UUID(source["session_id"]).hex[:13], 16) + 1,
            engagement_time_msec=max(1, round(params.get("duration_ms", 1))),
            event_id=event["uuid"],
            timestamp_micros=round(datetime.fromisoformat(event["timestamp"]).timestamp() * 1000000),
        )
        result.append({"name": event["event"], "params": params})
    return {"client_id": f"{ident >> 64}.{ident & ((1 << 64) - 1)}",
            "consent": {"ad_user_data": "DENIED", "ad_personalization": "DENIED"},
            "events": result}


def send_ga4(config, events):
    query = urlencode({"measurement_id": config["measurementId"], "api_secret": config["apiSecret"]})
    body = json.dumps(ga4_payload(events), allow_nan=False).encode()
    connection = http.client.HTTPSConnection("www.google-analytics.com", timeout=3)
    try:
        connection.request("POST", "/mp/collect?" + query, body=body,
                           headers={"Content-Type": "application/json"})
        return connection.getresponse().status
    finally:
        connection.close()


def safe_properties(values):
    """Allowlist controls and validated launch IDs; discard arbitrary free text."""
    result = {}
    for key, value in values.items():
        if key in ENUMS and isinstance(value, str) and value in ENUMS[key]:
            result[key] = value
        elif key in NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            result[key] = round(max(0, min(value, NUMBERS[key])), 2)
    result.update(launch_target(values.get("target_app_bundle_id")))
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
    def __init__(self, state_dir, version, sender=None, token=None, ga4=None, ga4_sender=None):
        self.state_dir, self.version = Path(state_dir), version
        self.token = project_token() if token is None else token
        self.sender = sender or send_batch
        self.ga4 = ga4_config() if ga4 is None else ga4
        self.ga4_sender = ga4_sender or send_ga4
        self.ident = None
        self.session_id = str(uuid.uuid4())
        self.queue = queue.Queue(maxsize=256)
        self.ga4_queue = queue.Queue(maxsize=256)
        self.lock = threading.Lock()
        self.worker = None
        self.ga4_worker = None
        self.started = False
        self.closed = False

    def capture(self, event, **properties):
        if opted_out() or not (self.token or self.ga4) or event not in EVENTS:
            return False
        try:
            with self.lock:
                if self.closed:
                    return False
                if self.ident is None:
                    self.ident = anonymous_id(self.state_dir)
                payload = {
                    "event": event, "distinct_id": self.ident, "uuid": str(uuid.uuid4()),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "properties": {
                        "app_name": "iphone-use", "app_version": self.version,
                        "app_surface": "local_mcp", "source": "codex_plugin_mcp",
                        "$lib": "iphone-use-mcp", "$process_person_profile": False,
                        "$geoip_disable": True, "session_id": self.session_id,
                        "platform": platform.system(), "environment": environment(),
                        "analytics_schema_version": 2, **safe_properties(properties),
                    },
                }
                accepted = False
                for enabled, pending, attribute, ga4 in (
                    (self.token, self.queue, "worker", False),
                    (self.ga4, self.ga4_queue, "ga4_worker", True),
                ):
                    if not enabled:
                        continue
                    try:
                        pending.put_nowait(payload)
                        accepted = True
                    except queue.Full:
                        continue
                    if getattr(self, attribute) is None:
                        worker = threading.Thread(target=self._deliver, args=(ga4,),
                                                  name="iphone-use-ga4" if ga4 else "iphone-use-posthog", daemon=True)
                        setattr(self, attribute, worker)
                        worker.start()
            return accepted
        except Exception:
            return False

    def start(self):
        with self.lock:
            if self.started:
                return
            self.started = True
        self.capture("iphone_use_session_started")

    def _deliver(self, ga4=False):
        pending = self.ga4_queue if ga4 else self.queue
        sender = self.ga4_sender if ga4 else self.sender
        config = self.ga4 if ga4 else self.token
        while True:
            try:
                first = pending.get(timeout=0.1)
            except queue.Empty:
                if self.closed:
                    return
                continue
            batch = [first]
            while len(batch) < 20:
                try:
                    batch.append(pending.get_nowait())
                except queue.Empty:
                    break
            # GA4 has no generic event UUID deduplication guarantee. Never retry
            # an ambiguous request; PostHog retries retain their original UUIDs.
            for attempt in range(1 if ga4 else 2):
                if opted_out():
                    break
                try:
                    status = sender(config, batch)
                    if 200 <= status < 300 or (status < 500 and status != 429):
                        break
                except Exception:
                    pass
                if not ga4 and attempt == 0 and not self.closed:
                    time.sleep(0.2)
                else:
                    break
            for _ in batch:
                pending.task_done()

    def tool_result(self, name, args, result, duration_ms, error=None):
        if name not in TOOLS:
            return
        controls = args if isinstance(args, dict) else {}
        # Automatic geometry recovery is polling, not a user interaction. Exclude
        # it before start() so an idle preview cannot create a usage session.
        if name == "pua_screen_action" and controls.get("action") == "viewport":
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
        if name == "pua_batch" and isinstance(controls.get("steps"), list):
            fields["batch_size"] = len(controls["steps"])
        if name == "pua_ready":
            state = "error" if outcome == "error" else ("ready" if data.get("ready") is True else data.get("state", "not_ready"))
            fields["ready_state"] = state if state in ENUMS["ready_state"] else "not_ready"
        elif name == "pua_setup":
            state = "error" if outcome == "error" else data.get("state", "success")
            fields.update(setup_action=controls.get("action"),
                          setup_state=state if state in ENUMS["setup_state"] else "other")
        elif name in ("pua_screen", "pua_screen_action"):
            fields["screen_action"] = controls.get("action", "open")
        elif name == "pua_launch_app":
            fields.update(launch_target(controls.get("bundle_id")))
        self.capture("iphone_use_tool_called", **fields)
        if name == "pua_launch_app" and outcome == "success" and isinstance(result, dict):
            self.app_launched(controls, data, "direct")
        elif name == "pua_batch":
            steps, results = controls.get("steps"), data.get("results")
            if isinstance(steps, list) and isinstance(results, list):
                # Only returned, successful substeps count, even if a later step
                # failed or the batch yielded. Never count unexecuted targets.
                for step, item in zip(steps[:20], results[:20]):
                    if (isinstance(step, dict) and step.get("op") == "launch_app"
                            and isinstance(item, dict) and not item.get("error")
                            and item.get("ok") is not False):
                        self.app_launched(step.get("args"), item, "batch")

    def app_launched(self, args, result, source):
        target = launch_target(args.get("bundle_id") if isinstance(args, dict) else None)
        if target:
            self.capture("iphone_use_app_launched", **target, launch_source=source,
                         foreground_verified="yes" if result.get("foreground_verified") is True else "no")

    def close(self, timeout=1.5):
        with self.lock:
            self.closed = True
        deadline = time.monotonic() + timeout
        for worker in (self.worker, self.ga4_worker):
            if worker is not None:
                worker.join(timeout=max(0, deadline - time.monotonic()))
