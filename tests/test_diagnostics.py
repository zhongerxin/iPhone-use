import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import wda_diagnostics
import wda_setup


REFUSAL = 'Connection peer refused channel request for "dtxproxy:XCTestDriverInterface:XCTestManager_IDEInterface"'


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.diag = wda_diagnostics.XCTestDiagnostics(Path(self.temp.name), "a" * 32, wda_setup._redact)

    def tearDown(self):
        self.temp.cleanup()

    def unavailable(self, argv, **kwargs):
        return {"ok": False, "stdout": "", "stderr": "tool unavailable"}

    def test_runner_code_is_separate_and_early_error_survives_footer_and_host_noise(self):
        self.diag.record("xcodebuild", REFUSAL)
        self.diag.record("xcodebuild", "Exiting due to IDE disconnection.")
        self.diag.record("xcodebuild", "The test runner exited with code 74 before establishing connection.")
        for _ in range(300):
            self.diag.record("host", "Error Domain=DTXProxyChannel Code=1 another session")
            self.diag.record("xcodebuild", "Verbose diagnostic collection footer without a failure")
        report = self.diag.failure_report(65, None, "fixture-device", self.unavailable)
        self.assertEqual(report["xcodebuild_exit_code"], 65)
        self.assertEqual(report["runner_exit_code"], 74)
        self.assertIsNone(report["forward_exit_code"])
        self.assertEqual(report["failure_stage"], "xctest_control_channel_refused")
        self.assertEqual(report["root_cause"], "undetermined")
        self.assertEqual(report["evidence"][0]["message"], REFUSAL)
        self.assertEqual(len(report["host_evidence"]), 40)

    def test_code_74_alone_or_another_host_session_does_not_prove_channel_or_vpn_cause(self):
        self.diag.record("xcodebuild", "The test runner exited with code 74 before establishing connection.")
        self.diag.record("host", REFUSAL)
        report = self.diag.failure_report(65, None, "fixture-device", self.unavailable)
        self.assertEqual(report["failure_stage"], "undetermined")
        self.assertEqual(report["root_cause"], "undetermined")

    def test_xcodebuild_exit_74_is_not_misreported_as_a_runner_exit(self):
        report = self.diag.failure_report(74, None, "fixture-device", self.unavailable)
        self.assertEqual(report["xcodebuild_exit_code"], 74)
        self.assertIsNone(report["runner_exit_code"])
        self.assertEqual(report["failure_stage"], "undetermined")

    def test_owner_cleanup_disconnect_is_not_the_original_forward_failure(self):
        self.diag.cleanup_while_xcode_active = True
        self.diag.record("xcodebuild", REFUSAL)
        self.diag.record("xcodebuild", "Exiting due to IDE disconnection.")
        self.diag.record("xcodebuild", "The test runner exited with code 74 before establishing connection.")
        report = self.diag.failure_report(None, 1, "fixture-device", self.unavailable)
        self.assertIsNone(report["runner_exit_code"])
        self.assertEqual(report["failure_stage"], "forward_process_exited")
        self.assertTrue(report["cleanup_started_while_xcode_active"])
        self.assertEqual(report["evidence"][0]["source"], "xcodebuild_after_cleanup")

    def test_result_bundle_exposes_automation_timeout_and_connection_state(self):
        self.diag.result_bundle.mkdir()

        def run(argv, **kwargs):
            if argv[1] == "xcresulttool":
                return {"ok": True, "stdout": json.dumps({"testFailures": [
                    {"failureText": "The test runner failed to initialize for UI testing. Timed out while enabling automation mode."}
                ]}), "stderr": ""}
            Path(argv[-1]).write_text(json.dumps({"result": {"connectionProperties": {
                "tunnelState": "connected", "transportType": "localNetwork", "tunnelTransportProtocol": "tcp"
            }}}))
            return {"ok": True, "stdout": "", "stderr": ""}

        report = self.diag.failure_report(65, None, "fixture-device", run)
        self.assertEqual(report["failure_stage"], "ui_automation_initialization")
        self.assertIsNone(report["runner_exit_code"])
        self.assertEqual(report["connection"]["tunnelState"], "connected")
        self.assertEqual((self.diag.directory / "device.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.diag.directory.stat().st_mode & 0o777, 0o700)

    def test_bad_result_summary_preserves_console_failure(self):
        self.diag.result_bundle.mkdir()
        self.diag.record("xcodebuild", REFUSAL)
        report = self.diag.failure_report(65, None, "fixture-device", self.unavailable)
        self.assertIn("result_summary_error", report)
        self.assertEqual(report["failure_stage"], "xctest_control_channel_refused")

    def test_native_capture_is_bounded_private_and_redacted(self):
        process = MagicMock()
        process.stdout = io.StringIO("".join(json.dumps({"eventMessage":
            f"XCTest error {i} account=someone@example.org password=private-secret"}) + "\n" for i in range(30)))
        process.poll.return_value = 0
        with patch.object(wda_diagnostics.subprocess, "Popen", return_value=process), \
             patch.object(wda_diagnostics, "HOST_LOG_BYTES", 256):
            self.diag.start_host_log()
            self.diag.stop_host_log()
        logs = list(self.diag.directory.glob("host*.log"))
        self.assertEqual(len(logs), 2)
        for path in logs:
            self.assertLessEqual(path.stat().st_size, 256)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            text = path.read_text()
            self.assertNotIn("someone@example.org", text)
            self.assertNotIn("private-secret", text)
        self.assertFalse(self.diag.evidence)

    def test_native_capture_failure_and_opt_out_are_nonfatal(self):
        with patch.object(wda_diagnostics.subprocess, "Popen", side_effect=OSError("permission denied")):
            self.diag.start_host_log()
            self.diag.stop_host_log()
        self.assertEqual(self.diag.host_capture["state"], "unavailable")
        with patch.dict(os.environ, {"IPHONE_USE_XCTEST_HOST_LOG": "0"}), \
             patch.object(wda_diagnostics.subprocess, "Popen") as launch:
            self.diag.start_host_log()
        launch.assert_not_called()
        self.assertEqual(self.diag.host_capture["state"], "disabled")

    def test_worker_records_failed_xctest_without_touching_real_device(self):
        root = Path(self.temp.name)
        manager = wda_setup.SetupManager(root / "state")
        source = root / "source"
        (source / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        (source / "WebDriverAgent.xcodeproj/project.pbxproj").write_text("fixture")
        binaries = root / "bin"
        binaries.mkdir()
        scripts = {
            "git": "import sys\nif 'rev-parse' in sys.argv: print(" + repr(wda_setup.WDA_COMMIT) + ")\n",
            "node": "import time\ntime.sleep(120)\n",
            "xcodebuild": "import sys\nfrom pathlib import Path\n"
                          "Path(sys.argv[sys.argv.index('-resultBundlePath')+1]).mkdir()\n"
                          "print(" + repr(REFUSAL) + ")\n"
                          "print('The test runner exited with code 74 before establishing connection.')\n"
                          "print('diagnostic collection footer\\n'*60)\nsys.exit(65)\n",
            "xcrun": "import json,sys\nfrom pathlib import Path\n"
                      "if sys.argv[1]=='xcresulttool': print(json.dumps({'testFailures':[]}))\n"
                      "else: Path(sys.argv[-1]).write_text(json.dumps({'result':{'connectionProperties':{'tunnelState':'connected'}}}))\n",
        }
        for name, content in scripts.items():
            path = binaries / name
            path.write_text("#!" + sys.executable + "\n" + content)
            path.chmod(0o700)
        config = {"udid": "00008150-ABCDEF0123456789", "team_id": "ABCDE12345",
                  "bundle_id": "com.example.runner", "source_dir": str(source),
                  "local_port": 18100, "device_port": 8100}
        forward = manager.state_dir / "runtime/forward"
        (forward / "node_modules/appium-ios-device").mkdir(parents=True)
        lock_hash = wda_setup.hashlib.sha256((wda_setup.PLUGIN_ROOT / "tooling/package-lock.json").read_bytes()).hexdigest()
        wda_setup._write_json(forward / "installed.json", {"lock_hash": lock_hash})
        with patch.dict(os.environ, {"PATH": str(binaries) + os.pathsep + os.environ.get("PATH", ""),
                                    "IPHONE_USE_XCTEST_HOST_LOG": "0", "IPHONE_USE_ANALYTICS": "0"}):
            result = manager._create_job("start", config)
        process = manager._workers[result["job_id"]]
        try:
            process.wait(timeout=12)
        finally:
            if process.poll() is None:
                manager.setup("stop", job_id=result["job_id"])
                process.wait(timeout=5)
        job = wda_setup._read_json(manager._job_path(result["job_id"]))
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["diagnostics"]["runner_exit_code"], 74)
        self.assertEqual(job["diagnostics"]["xcodebuild_exit_code"], 65)
        report = Path(job["diagnostics"]["report_path"])
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report.read_text())["failure_stage"], "xctest_control_channel_refused")
        status = manager._job_status(job)
        self.assertNotIn(REFUSAL, status["log_tail"])
        self.assertTrue(any("IDE control channel" in hint for hint in status["next_steps"]))


if __name__ == "__main__":
    unittest.main()
