"""Host-driven Midscene device actions, under Runtime's operation lock."""
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid

from wda_client import WDAError
import wda_image
import wda_chatgpt
import wda_mode

WORKER = Path(__file__).parent / "midscene" / "run.mjs"
MUTATIONS = {"tap", "swipe", "input", "home", "launch", "act"}
FIELDS = {"screenshot": set(), "tap": {"x", "y"},
          "swipe": {"x", "y", "end_x", "end_y"}, "input": {"text"},
          "home": set(), "launch": {"text"}, "record": {"text", "passed"},
          "act": {"text"}, "assert": {"text"}, "wait": {"text", "timeout_ms"}}


def execute_worker(command, *, runtime, action, input, timeout, **options):
    """Cancel an in-flight worker when another runtime pauses authentication."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, **options)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if runtime.screen.paused():
                raise WDAError("preview_paused", "Midscene was cancelled for authentication takeover; inspect state before continuing.",
                               uncertain=action in MUTATIONS)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(input=input, timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                input = None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=2)


def run(runtime, action, report_id=None, planning=None, **args):
    report_id = report_id if report_id is not None else uuid.uuid4().hex
    if not isinstance(report_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", report_id):
        raise WDAError("invalid_arguments", "report_id must contain 1–64 letters, digits, underscores or hyphens.")
    if action == "wait":
        args.setdefault("timeout_ms", 15000)
        if type(args["timeout_ms"]) is not int or not 1000 <= args["timeout_ms"] <= 60000:
            raise WDAError("invalid_arguments", "wait timeout_ms must be an integer from 1000 to 60000.")
    if action not in FIELDS or set(args) != FIELDS[action]:
        raise WDAError("invalid_arguments", "Supply exactly the fields for this action; see the Midscene guide.")
    if planning is not None and (action != "act" or planning not in ("balanced", "compact")):
        raise WDAError("invalid_arguments", "Planning is accepted only for act: balanced or compact.")
    if action in ("act", "assert", "wait") and not args["text"].strip():
        raise WDAError("invalid_arguments", "AI instructions must not be empty.")
    if action == "input" and any(c in args["text"] for c in "\r\n\t"):
        raise WDAError("invalid_arguments", "Input accepts single-line text only; no implicit submit.")
    if action == "launch" and not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", args["text"]):
        raise WDAError("invalid_arguments", "Launch requires an installed app bundle ID in text.")
    wda_mode.require(runtime.state_dir, action)
    node = shutil.which("node")
    if not node or not (WORKER.parent / "node_modules/@midscene/ios/package.json").is_file():
        raise WDAError("midscene_not_installed", "Install Node.js 22.19+ and run npm ci --prefix <plugin-root>/server/midscene.")
    if action in ("act", "assert", "wait") and not wda_chatgpt.run(runtime.state_dir, "auth_status")["authorized"]:
        raise WDAError("chatgpt_sign_in_required", "Use pua_midscene(action=auth_login) and complete official ChatGPT consent before act/assert/wait.")
    if runtime.screen.paused():
        raise WDAError("preview_paused", "Resume after the user finishes authentication before using Midscene.", details={"action_executed": False})
    if runtime.client.request("GET", "/wda/locked").get("value") is not False:
        raise WDAError("phone_locked", "Unlock the iPhone before using Midscene.")
    session_id = runtime.client.ensure_session()
    request = {"action": action, "args": args, "host": runtime.client.host,
               "port": runtime.client.port, "sessionId": session_id, "reportId": report_id}
    if planning is not None:
        request["planning"] = planning
    details = {"action_complete": False, "report_id": report_id}
    # OAuth-only AI mode must not inherit any provider credentials/configuration.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("MIDSCENE_", "OPENAI_"))}
    worker = WORKER.with_name("run-ai.mjs") if action in ("act", "assert", "wait") else WORKER
    lock_fd = getattr(runtime, "operation_lock_fd", None)
    try:
        if action in MUTATIONS:
            runtime.phone.external_action()
            runtime._midscene_action_revision += 1
        process = execute_worker([node, str(worker)], runtime=runtime, action=action, input=json.dumps(request),
                                 text=True, env=env,
                                 pass_fds=(lock_fd,) if lock_fd is not None else (),
                                 cwd=runtime.state_dir, timeout=330 if action == "act" else 180 if action == "assert" else (args["timeout_ms"] / 1000 + 30) if action == "wait" else 60)
        result = json.loads(process.stdout)
        if isinstance(result, dict) and isinstance(result.get("report"), str):
            details["report"] = result["report"]
        if not isinstance(result, dict):
            raise ValueError()
        if isinstance(result.get("error"), str):
            details["reason"] = result["error"]
        if isinstance(result.get("model"), str):
            details["model"] = result["model"]
        if isinstance(result.get("model_requests"), list):
            details["model_requests"] = result["model_requests"]
        encoded = result.pop("screenshot", None)
        if encoded:
            data = base64.b64decode(encoded.split(",", 1)[-1], validate=True)
            if not data.startswith(wda_image.PNG_SIGNATURE):
                raise ValueError()
            artifacts = Path(runtime.state_dir) / "artifacts"
            artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = artifacts / ("midscene-" + uuid.uuid4().hex + ".png")
            path.write_bytes(data)
            path.chmod(0o600)
            image_path, mime, width, height = wda_image.model_image(path)
            viewport = result["viewport"]
            result["image"] = {"path": str(image_path), "mimeType": mime,
                               "width": width, "height": height,
                               "pixel_to_point": [viewport["width"] / width, viewport["height"] / height]}
            details["observation"] = {"image": result["image"], "viewport": viewport}
        if process.returncode or result.get("ok") is not True:
            raise ValueError()
        return {**result, "report_id": report_id}
    except WDAError as exc:
        partial = Path(runtime.state_dir) / "midscene_run" / "report" / ("iphone-use-" + report_id + ".html")
        if partial.is_file():
            details["report"] = str(partial)
        exc.details.update(details)
        raise
    except (subprocess.TimeoutExpired, ValueError, OSError, KeyError, TypeError):
        partial = Path(runtime.state_dir) / "midscene_run" / "report" / ("iphone-use-" + report_id + ".html")
        if partial.is_file():
            details.setdefault("report", str(partial))
        raise WDAError("midscene_failed", "Midscene failed or timed out. Inspect the current screen; do not replay the action.",
                       uncertain=action in MUTATIONS, details=details) from None
    finally:
        runtime.client.reapply_settings()
        runtime._midscene_revision += 1
