"""Bounded authentication handoff checks. Never fetch pixels or field values."""
import fcntl
import json
import os
import re
import time

from wda_client import WDAClient, WDAError, SESSION_SETTINGS


class AuthWaiter:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stage = "not_started"

    def wait(self, bundle_id=None, ready_label=None, auth_label=None, timeout=25):
        screen = self.runtime.screen
        state = screen._read_state()
        if not state["paused"]:
            return {"paused": False, "state": "not_paused", "fresh_observation_required": True}
        pause_id, epoch = state["pause_id"], state["access_epoch"]
        if not pause_id:
            # Give legacy pauses an identity without clearing them.
            pause_id = screen.reconnect_pause_id()
        device_lock = state["pause_reason"] == "device_locked"
        if not device_lock and not (bundle_id and (ready_label or auth_label)):
            return {"paused": True, "state": "auth_target_required",
                    "next_step": "Use wait_auth with the target bundle_id and an observed ready_label or auth_label from task context. No host confirmation exists; do not ask the user to confirm completion. Do not supply credentials."}
        deadline = time.monotonic() + timeout
        stable = 0
        last_state = "waiting_auth"
        while time.monotonic() < deadline:
            current = screen._read_state()
            if current["pause_id"] != pause_id or current["access_epoch"] != epoch:
                return {"paused": current["paused"], "state": "pause_changed", "fresh_observation_required": True}
            try:
                clear = self._probe(deadline, device_lock, bundle_id, ready_label, auth_label)
                stable = stable + 1 if clear else 0
                last_state = "waiting_auth"
            except (WDAError, OSError, ValueError, TypeError, KeyError) as exc:
                # Backend error messages can contain page content. Return only a code.
                return {"paused": True, "state": "probe_unavailable",
                        "error_code": exc.code if isinstance(exc, WDAError) else "invalid_probe",
                        "probe_stage": self.stage,
                        "replay_action": False}
            if stable >= 2:
                resumed = screen.resume_after_auth_probe(pause_id, epoch)
                return {"paused": screen.paused(), "state": "auth_ui_cleared" if resumed else "pause_changed",
                        "resumed": resumed, "authentication_verified": False,
                        "fresh_observation_required": True, "replay_action": False}
            # Leave room for two observations even at the minimum one-second budget.
            time.sleep(min(1, timeout / 4, max(0, deadline - time.monotonic())))
        return {"paused": True, "state": last_state, "retry_after_seconds": 1,
                "next_step": "Continue wait_auth while the user authenticates on the phone. No host confirmation or chat reply is needed."}

    def _probe(self, deadline, device_lock, bundle_id, ready_label, auth_label):
        runtime = self.runtime
        # Serialize settings with every normal phone operation, across processes.
        with (runtime.state_dir / "operation.lock").open("a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            client = WDAClient(runtime.base_url, timeout=10)
            def request(method, path, payload=None):
                self.stage = path.rsplit("/", 1)[-1]
                return client.request(method, path, payload, timeout=min(10, WDAClient.remaining(deadline))).get("value")
            try:
                status = request("GET", "/status")
                if not isinstance(status, dict) or status.get("ready") is not True:
                    raise WDAError("not_ready", "Authentication probe unavailable.")
                if request("GET", "/wda/locked") is not False:
                    return False
                if device_lock:
                    return True
                if (request("GET", "/wda/activeAppInfo") or {}).get("bundleId") != bundle_id:
                    return False
                cached = json.loads((runtime.state_dir / "session.json").read_text())
                sid = cached.get("session_id")
                if cached.get("url") != runtime.base_url or not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", sid):
                    raise WDAError("auth_session_required", "An existing session is required.")
                prefix = f"/session/{sid}"
                settings = request("GET", prefix + "/appium/settings")
                compact = settings.get("shouldUseCompactResponses") if isinstance(settings, dict) else None
                if not isinstance(compact, bool):
                    raise WDAError("invalid_response", "Compact response mode is unavailable.")
                if settings.get("snapshotMaxDepth", 0) < 50 or settings.get("snapshotMaxChildren", 0) < 2147483647:
                    raise WDAError("auth_snapshot_incomplete", "Restore normal snapshot settings before checking authentication.")
                try:
                    request("POST", prefix + "/appium/settings", {"settings": {"shouldUseCompactResponses": True}})
                    if request("GET", prefix + "/appium/settings").get("shouldUseCompactResponses") is not True:
                        raise WDAError("invalid_response", "Compact response mode was not applied.")
                    def exists(predicate):
                        # /elements resolves and caches every match, which can stall
                        # on long or changing pages. Existence needs one ID only.
                        try:
                            value = request("POST", prefix + "/element", {"using": "predicate string", "value": predicate})
                        except WDAError as exc:
                            if exc.code == "no such element":
                                return False
                            raise
                        # Compact WDA results must contain IDs only, never labels/values.
                        if not isinstance(value, dict) or not value or set(value) - {
                                "ELEMENT", "element-6066-11e4-a52e-4f735466cecf"}:
                            raise WDAError("invalid_response", "Noncompact authentication result suppressed.")
                        return True
                    # Filter type/label before visible: native visibility can stall
                    # on unrelated elements in long Settings or custom pages.
                    # An empty Application/Window tree cannot prove that an auth
                    # heading disappeared. Require actual noneditable UI content.
                    if not exists("(type == 'XCUIElementTypeButton' OR type == 'XCUIElementTypeStaticText' OR type == 'XCUIElementTypeNavigationBar' OR type == 'XCUIElementTypeTabBar') AND visible == 1"):
                        return False
                    if exists("(type == 'XCUIElementTypeSecureTextField' OR type == 'XCUIElementTypeKeyboard' OR type == 'XCUIElementTypeAlert' OR type == 'XCUIElementTypeSheet') AND visible == 1"):
                        return False
                    def label_predicate(label):
                        # Never match editable field values or return text attributes.
                        literal = json.dumps(label, ensure_ascii=False)
                        return f"type != 'XCUIElementTypeTextField' AND type != 'XCUIElementTypeSecureTextField' AND type != 'XCUIElementTypeTextView' AND (label == {literal} OR name == {literal}) AND visible == 1"
                    if auth_label and exists(label_predicate(auth_label)):
                        return False
                    if ready_label and not exists(label_predicate(ready_label)):
                        return False
                    return (request("GET", "/wda/activeAppInfo") or {}).get("bundleId") == bundle_id and request("GET", "/wda/locked") is False
                finally:
                    # Restore even when the bounded check runs out of time. If this
                    # fails the caller leaves access paused; a later check can retry.
                    # All normal plugin sessions require rich responses. Restore that
                    # baseline even after a previous probe failed to restore it.
                    client.request("POST", prefix + "/appium/settings", {"settings": {"shouldUseCompactResponses": SESSION_SETTINGS["shouldUseCompactResponses"]}}, timeout=3)
            finally:
                client.close()
