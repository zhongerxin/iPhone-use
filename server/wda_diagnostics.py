"""Bounded, private evidence for XCTest failures before WDA's test method runs."""
from collections import deque
import json
import os
from pathlib import Path
import re
import subprocess
import threading


HOST_PREDICATE = (
    '(process == "xcodebuild" OR process == "CoreDeviceService" OR process == "remoted") AND '
    '(subsystem CONTAINS[c] "XCTest" OR eventMessage CONTAINS[c] "XCTest" OR eventMessage CONTAINS[c] "DTX" OR '
    'eventMessage CONTAINS[c] "testmanager" OR eventMessage CONTAINS[c] "proxy channel" OR '
    'eventMessage CONTAINS[c] "serialized transport" OR eventMessage CONTAINS[c] "handoff" OR '
    'eventMessage CONTAINS[c] "error" OR eventMessage CONTAINS[c] "failed" OR '
    'eventMessage CONTAINS[c] "refused" OR eventMessage CONTAINS[c] "disconnect")'
)
SIGNALS = re.compile(
    r"XCTestDriverInterface|DTXProxyChannel|peer refused channel|IDE disconnection|"
    r"retrieve test configuration|test runner exited with code|"
    r"enabling automation mode|not authorized for performing ui testing|"
    r"XCTDaemonError|serialized transport|proxy channel request|"
    r"failed to initialize for ui testing|Error Domain=", re.I
)
RUNNER_EXIT = re.compile(r"test runner exited with code\s+['\"]?(-?\d+)", re.I)
HOST_LOG_BYTES = 1024 * 1024


def failure_stage(evidence):
    # Shared CoreDevice logs can include other tests/devices. Classify using
    # this worker's console and result bundle, not an unrelated host message.
    text = "\n".join(item["message"] for item in evidence
                     if item["source"] in {"xcodebuild", "xcresult"}).lower()
    if "peer refused channel" in text and "xctestdriverinterface" in text:
        return "xctest_control_channel_refused"
    if "enabling automation mode" in text or "not authorized for performing ui testing" in text:
        return "ui_automation_initialization"
    if "retrieve test configuration" in text:
        return "test_configuration_unavailable"
    if "ide disconnection" in text:
        return "ide_disconnected"
    return "undetermined"


class XCTestDiagnostics:
    def __init__(self, state_dir, job_id, redact):
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("Invalid diagnostic job ID.")
        parent = Path(state_dir) / "diagnostics"
        self.directory = parent / job_id
        for path in (parent, self.directory):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
        self.result_bundle = self.directory / "xctest.xcresult"
        self.host_log = self.directory / "host.log"
        self.report_path = self.directory / "failure.json"
        self.redact = redact
        self.evidence = deque(maxlen=80)
        self.host_evidence = deque(maxlen=40)
        self._lock = threading.Lock()
        self._process = None
        self._thread = None
        self.host_capture = {"state": "not_started"}
        self.cleanup_while_xcode_active = False

    def paths(self):
        return {"directory": str(self.directory), "result_bundle_path": str(self.result_bundle),
                "host_log_path": str(self.host_log), "report_path": str(self.report_path),
                "report_available": self.report_path.is_file(), "host_capture": dict(self.host_capture)}

    def record(self, source, line):
        message = self.redact(str(line)).strip()
        if not SIGNALS.search(message):
            return
        if self.cleanup_while_xcode_active and source == "xcodebuild":
            source = "xcodebuild_after_cleanup"
        with self._lock:
            # Keep early failures even when xcodebuild appends long diagnostic
            # collection notices that displace them from setup's short log tail.
            target = self.host_evidence if source == "host" else self.evidence
            target.append({"source": source, "message": message[:3000]})

    def _drain_host(self):
        output = None
        try:
            fd = os.open(self.host_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            output = os.fdopen(fd, "wb")
            with self._process.stdout as stream:
                os.chmod(self.host_log, 0o600)
                for line in stream:
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError("Non-object native log entry.")
                        entry = {key: self.redact(event[key]) if isinstance(event[key], str) else event[key]
                                 for key in ("timestamp", "processID", "threadID", "processImagePath",
                                             "senderImagePath", "subsystem", "category", "messageType",
                                             "eventMessage") if key in event}
                        message = entry.get("eventMessage", "")
                        line = json.dumps(entry, ensure_ascii=False) + "\n"
                    except (ValueError, AttributeError):
                        line = message = self.redact(line)
                    self.record("host", message)
                    data = line.encode("utf-8", "replace")[:HOST_LOG_BYTES]
                    if output.tell() + len(data) > HOST_LOG_BYTES:
                        output.close()
                        self.host_log.replace(self.directory / "host.previous.log")
                        fd = os.open(self.host_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                        output = os.fdopen(fd, "wb")
                    output.write(data)
                    output.flush()
        except (OSError, ValueError) as error:
            self.host_capture.update(state="unavailable", error=self.redact(error))
        finally:
            if output is not None:
                output.close()

    def start_host_log(self):
        if os.environ.get("IPHONE_USE_XCTEST_HOST_LOG", "1").lower() in {"0", "false", "off"}:
            self.host_capture = {"state": "disabled"}
            return
        try:
            self._process = subprocess.Popen(
                ["/usr/bin/log", "stream", "--style", "ndjson", "--level", "debug",
                 "--predicate", HOST_PREDICATE], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                errors="replace", bufsize=1
            )
            self.host_capture = {"state": "capturing"}
            self._thread = threading.Thread(target=self._drain_host, name="xctest-host-log", daemon=True)
            self._thread.start()
        except OSError as error:
            # Missing permission/tool must not prevent otherwise usable WDA.
            self.host_capture = {"state": "unavailable", "error": self.redact(error)}

    def stop_host_log(self):
        if self._process is not None:
            exited = self._process.poll()
            if exited is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    self._process.wait(timeout=2)
            elif exited:
                self.host_capture.update(state="unavailable", error=f"log stream exited with code {exited}")
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self.host_capture["state"] == "capturing":
            self.host_capture["state"] = "stopped"

    def failure_report(self, xcode_exit_code, forward_exit_code, udid, run):
        details = {}
        if self.result_bundle.is_dir():
            result = run(["xcrun", "xcresulttool", "get", "test-results", "summary",
                          "--path", str(self.result_bundle), "--compact"], timeout=12)
            try:
                summary = json.loads(result["stdout"]) if result["ok"] else {}
                failures = summary.get("testFailures", [])
                details["test_failures"] = [
                    self.redact(item.get("failureText", "")) for item in failures if isinstance(item, dict)
                ]
                for message in details["test_failures"]:
                    self.record("xcresult_after_cleanup" if self.cleanup_while_xcode_active else "xcresult", message)
                if not result["ok"]:
                    details["result_summary_error"] = self.redact(result.get("stderr", ""))
            except (ValueError, AttributeError, TypeError) as error:
                details["result_summary_error"] = self.redact(error)
        device_path = self.directory / "device.json"
        result = run(["xcrun", "devicectl", "device", "info", "details", "--device", udid,
                      "--timeout", "5", "--json-output", str(device_path)], timeout=8)
        try:
            if device_path.is_file():
                device_path.chmod(0o600)
                device = json.loads(device_path.read_text())
                connection = device.get("result", {}).get("connectionProperties", {})
                details["connection"] = {key: connection[key] for key in
                                         ("pairingState", "tunnelState", "transportType",
                                          "tunnelTransportProtocol") if key in connection}
            if not result["ok"]:
                details["device_query_error"] = self.redact(result.get("stderr", ""))
        except (OSError, ValueError, AttributeError, TypeError) as error:
            details["device_query_error"] = self.redact(error)
        with self._lock:
            evidence = list(self.evidence)
            host_evidence = list(self.host_evidence)
        runner_codes = [int(match.group(1)) for item in evidence if item["source"] in {"xcodebuild", "xcresult"}
                        for match in [RUNNER_EXIT.search(item["message"])] if match]
        stage = failure_stage(evidence)
        if stage == "undetermined" and self.cleanup_while_xcode_active and forward_exit_code is not None:
            stage = "forward_process_exited"
        return {**self.paths(), "report_available": True,
                "xcodebuild_exit_code": xcode_exit_code, "forward_exit_code": forward_exit_code,
                "runner_exit_code": runner_codes[-1] if runner_codes else None,
                "failure_stage": stage, "root_cause": "undetermined",
                "cleanup_started_while_xcode_active": self.cleanup_while_xcode_active,
                "evidence": evidence, "host_evidence": host_evidence, **details}
