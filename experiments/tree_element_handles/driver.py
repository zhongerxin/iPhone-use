"""Opt-in native handle experiment; preserves shared state and restores stock WDA."""
from contextlib import contextmanager
from datetime import datetime
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import quote


def build_environment():
    return {key: value for key, value in os.environ.items() if key in
            {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DEVELOPER_DIR", "USER", "LOGNAME"}}


def private_json(path, value):
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


@contextmanager
def operation_lock(runtime, check_session=True):
    with (runtime.state_dir / "operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if check_session:
                cached = json.loads((runtime.state_dir / "session.json").read_text())
                if cached.get("session_id") != runtime.client.session_id or cached.get("url") != runtime.base_url:
                    raise RuntimeError("Shared session changed; obtain READY again")
                if runtime.client.request("GET", "/wda/locked").get("value") is not False:
                    raise RuntimeError("phone_locked: user must unlock the phone")
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def build_command(manager, source, derived, action):
    config = dict(manager.config, source_dir=str(source))
    command = manager._build_command(config, action)
    command[command.index("-derivedDataPath") + 1] = str(derived)
    # Xcode 27 rejects host-only /usr/local/include during iOS cross-compilation.
    command.insert(-1, "OTHER_CFLAGS=$(inherited) -Wno-error=poison-system-directories")
    return command


class Experiment:
    def __init__(self, runtime_class, source, directory):
        self.runtime_class = runtime_class
        self.runtime = runtime_class()
        self.runtime.client.timeout = 60
        self.source, self.directory = source, directory
        self.derived = self.runtime.state_dir / "derived_data_tree_handles"
        self.children, self.logs, self.entries = [], [], []
        manifest = directory / "manifest.json"
        if manifest.exists():
            self.entries = json.loads(manifest.read_text())
        self.original_config = self.runtime.setup_manager.config
        self.switched = False
        self.last_tree = None
        self.native_log = None

    def build(self):
        command = build_command(self.runtime.setup_manager, self.source, self.derived, "build-for-testing")
        with (self.directory / "build.log").open("wb") as log:
            result = subprocess.run(command, cwd=self.source, stdout=log, stderr=subprocess.STDOUT,
                                    env=build_environment())
        print(json.dumps({"build_exit_code": result.returncode, "log": str(self.directory / "build.log")}), flush=True)
        return result.returncode

    def launch(self, command, log_name, cwd=None):
        log = (self.directory / log_name).open("wb")
        self.logs.append(log)
        child = subprocess.Popen(command, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, env=build_environment())
        self.children.append(child)
        return child

    def switch(self):
        if self.switched:
            raise RuntimeError("Experiment already active")
        manager = self.runtime.setup_manager
        with operation_lock(self.runtime, check_session=False):
            if manager.config != self.original_config:
                raise RuntimeError("Configuration changed")
            owned = [job for job in manager._jobs() if job.get("state") in {"queued", "running"}
                     and manager._matching_job(job, manager.config) and manager._owns_listener(job)]
            if len(owned) != 1:
                raise RuntimeError("Refusing to stop an unverified or ambiguous original service")
            stopped = manager.setup("stop", job_id=owned[0]["id"])
            if not stopped.get("ok") or stopped.get("stopped_jobs") != [owned[0]["id"]]:
                raise RuntimeError("Original service was not stopped")
            self.switched = True
            deadline = time.monotonic() + 15
            while manager._listener_pids() != [] or manager._group_running(owned[0]["pid"]):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Original service did not exit; no experiment launched")
                time.sleep(0.2)
            config = manager.config
            forward = manager.state_dir / "runtime/forward/forward.mjs"
            self.launch(["node", str(forward), config["udid"], str(config["local_port"]), str(config["device_port"])], "forward.log")
            self.native_log = self.directory / "runner.log"
            self.launch(build_command(manager, self.source, self.derived, "test-without-building"), "runner.log", self.source)
            deadline = time.monotonic() + 60
            while not manager._probe_status().get("ready"):
                if any(child.poll() is not None for child in self.children):
                    raise RuntimeError("Experimental child exited; inspect private logs")
                if time.monotonic() >= deadline:
                    raise RuntimeError("Experimental service did not become reachable")
                time.sleep(0.3)
        self.runtime.close()
        self.runtime = self.runtime_class()
        self.runtime.client.timeout = 60
        return {"experimental_service": "reachable", "runner_log": str(self.native_log)}

    def restore(self):
        if not self.switched:
            return {"restored": False, "reason": "original service was never stopped"}
        self.runtime.close()
        for child in reversed(self.children):
            if child.poll() is None:
                if os.getpgid(child.pid) != child.pid:
                    raise RuntimeError("Experiment child group identity changed")
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        for log in self.logs:
            log.close()
        self.children.clear()
        self.logs.clear()
        # Cleanup is complete even if stock XCTest cannot start; do not retry in finally.
        self.switched = False
        manager = self.runtime.setup_manager
        if manager.config != self.original_config:
            raise RuntimeError("Configuration changed; restore refused")
        result = manager.setup("start", wait_seconds=20)
        job_id = result.get("job_id") or result.get("job", {}).get("id")
        deadline = time.monotonic() + 90
        while result.get("ok") and not manager._probe_status().get("ready"):
            if not job_id or time.monotonic() >= deadline:
                raise RuntimeError("Original WDA restart not confirmed")
            result = manager.setup("status", job_id=job_id, wait_seconds=20)
            if any(job.get("state") not in {"running", "queued"} for job in result.get("jobs", [])):
                raise RuntimeError("Original WDA restart failed")
        if not result.get("ok"):
            raise RuntimeError("Original WDA restart failed")
        self.runtime = self.runtime_class()
        self.runtime.client.timeout = 60
        return {"restored": True, "service_ready": True, "job_id": job_id}

    def run(self, command):
        label = command["label"]
        if not label or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in label):
            raise ValueError("Use a non-private ASCII label")
        before = len(self.runtime.client.records)
        started = time.monotonic()
        entry = {"label": label, "op": command["op"], "started": datetime.now().astimezone().isoformat()}
        if self.native_log:
            entry["log_start_byte"] = self.native_log.stat().st_size
        try:
            if command["op"] == "tool":
                result = self.runtime.call(command["tool"], command.get("arguments", {}))
            elif command["op"] == "switch":
                result = self.switch()
            elif command["op"] == "restore":
                result = self.restore()
            elif command["op"] == "handles":
                with operation_lock(self.runtime):
                    path = "/wda/sourceWithHandles?max_nodes=200&register_handles=" + str(command.get("register", True)).lower()
                    result = self.runtime.client.session("GET", path)
                    self.last_tree = result
            elif command["op"] == "trial":
                with operation_lock(self.runtime):
                    result = self.trial(command, entry)
            elif command["op"] == "settings":
                with operation_lock(self.runtime):
                    result = self.runtime.client.session("GET", "/appium/settings")
                entry["settings"] = {key: result.get(key) for key in
                                     ("snapshotMaxDepth", "snapshotMaxChildren", "waitForIdleTimeout", "animationCoolOffTimeout")}
            elif command["op"] == "cached-click":
                with operation_lock(self.runtime):
                    if not self.last_tree:
                        raise RuntimeError("No handle observation")
                    target = select_target(self.last_tree["nodes"], command["target"])
                    self.runtime.phone.post("/element/" + quote(target["element_id"], safe="") + "/click", {})
                    result = {"clicked": True, "image_observation": self.runtime.phone.observe("screenshot")}
            else:
                raise ValueError("Unknown operation")
        except Exception as error:
            result = {"error": error.as_dict() if hasattr(error, "as_dict") else {"code": type(error).__name__, "message": str(error)}}
        entry["seconds"] = round(time.monotonic() - started, 4)
        entry["http"] = list(self.runtime.client.records)[before:]
        if self.native_log:
            entry["log_end_byte"] = self.native_log.stat().st_size
        if command["op"] == "handles" and "error" not in result:
            entry.update({key: result.get(key) for key in
                          ("total_nodes", "eligible_nodes", "truncated", "registered_handles", "handle_api_supported", "timings", "viewport")})
            targets = [node for node in result["nodes"] if node.get("type") == "Cell" and command.get("target", "\u6587\u4ef6\u4f20\u8f93\u52a9\u624b") in node.get("label", "")]
            entry["target_handles"] = [{key: node.get(key) for key in ("type", "rect", "native_uid", "element_id")} for node in targets]
        observed = result.get("observation", result.get("image_observation", result))
        entry.update({key: result[key] for key in ("ready", "state", "restored", "error", "clicked") if key in result})
        if "error" in entry:
            entry["error"] = {key: entry["error"][key] for key in
                              ("code", "uncertain", "operation") if key in entry["error"]}
        if isinstance(observed, dict):
            entry["observation"] = {key: observed[key] for key in ("app", "viewport", "image") if key in observed}
        self.entries.append(entry)
        private_json(self.directory / (label + ".private.json"), {"command": command, "result": result})
        private_json(self.directory / "manifest.json", self.entries)
        print(json.dumps(entry, ensure_ascii=False), flush=True)

    def trial(self, command, entry):
        stages = {}
        entry["stages"] = stages
        started = time.monotonic()
        if command["route"] in {"handles", "lookup-control"}:
            path = "/wda/sourceWithHandles?max_nodes=200"
            if command["route"] == "lookup-control":
                path += "&register_handles=false"
            tree = self.runtime.client.session("GET", path)
            stages["observation"] = time.monotonic() - started
            stages["server_timings"] = tree["timings"]
            stages["registered_handles"] = tree["registered_handles"]
            if command["route"] == "handles":
                self.last_tree = tree
                target = select_target(tree["nodes"], command["target"])
                element_id = target["element_id"]
        elif command["route"] == "ordinary":
            self.runtime.phone.tree(False, False)
            stages["observation"] = time.monotonic() - started
        else:
            raise ValueError("Unknown trial route")
        if command["route"] in {"ordinary", "lookup-control"}:
            started = time.monotonic()
            query = self.runtime.phone.query({"type": "Cell", "label_contains": command["target"]})
            if query["matches"] != 1:
                raise RuntimeError("Target is not unique")
            target = query["elements"][0]
            element_id = target["element_id"]
            stages["lookup"] = time.monotonic() - started
        entry["stages"] = stages
        stages["target_rect"] = target["rect"]
        entry["target_handle"] = {key: target[key] for key in
                                  ("native_uid", "element_id", "rect") if key in target}
        started = time.monotonic()
        # Both routes use the same safety check; it is not part of UUID extraction.
        hittable = self.runtime.client.session("GET", "/element/" + quote(element_id, safe="") + "/attribute/hittable")
        stages["hittable"] = time.monotonic() - started
        if str(hittable).lower() != "true":
            raise RuntimeError("Target is not hittable; no click executed")
        started = time.monotonic()
        self.runtime.phone.post("/element/" + quote(element_id, safe="") + "/click", {})
        stages["click"] = time.monotonic() - started
        stages["path_total"] = sum(stages.get(key, 0) for key in ("observation", "lookup", "hittable", "click"))
        return {"clicked": True, "image_observation": self.runtime.phone.observe("screenshot")}


def select_target(nodes, text):
    targets = [node for node in nodes if node.get("type") == "Cell" and text in node.get("label", "")]
    if len(targets) != 1:
        raise RuntimeError("Expected exactly one visible target Cell")
    if not targets[0].get("element_id"):
        raise RuntimeError("Target has no registered handle")
    return targets[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--native-source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.chmod(0o700)
    sys.path.insert(0, str(args.plugin_root / "server"))
    from iphone_use import Runtime
    experiment = Experiment(Runtime, args.native_source, args.output)
    try:
        if args.build:
            return experiment.build()
        print(json.dumps({"interactive": True, "http_timeout_seconds": 60}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            if command.get("op") == "close":
                break
            experiment.run(command)
    finally:
        if experiment.switched:
            print(json.dumps(experiment.restore()), flush=True)
        experiment.runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
