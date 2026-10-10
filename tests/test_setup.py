import os
import io
import datetime as dt
from pathlib import Path
import signal
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import wda_setup


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = wda_setup.SetupManager(Path(self.temp.name) / "state")

    def tearDown(self):
        self.temp.cleanup()

    def configure(self, **extra):
        return self.manager.setup("configure", udid="00008150-ABCDEF0123456789", team_id="ABCDE12345",
                                  bundle_id="com.example.wdarunner", **extra)

    def job(self, token="owned-marker", pid=12345):
        return {"id": "a" * 32, "action": "start", "state": "running", "pid": pid,
                "owner_token": token, "created_at": "2026-10-06T00:00:00Z", "config": {}}

    def test_start_waits_for_service_without_creating_another_job(self):
        job = {"id": "a" * 32, "action": "start", "state": "running"}
        queued = {"ok": True, "job_id": job["id"], "job": job}
        pending = {"ok": True, "jobs": [job], "service": {"ready": False}}
        serving = {"ok": True, "jobs": [job], "service": {"ready": True}}
        with patch.object(self.manager, "_setup_once", side_effect=[queued, pending, serving]) as setup, \
             patch.object(wda_setup.time, "sleep") as sleep:
            result = self.manager.setup("start")
        self.assertEqual(result["wait_reason"], "service_ready")
        self.assertTrue(result["service"]["ready"])
        self.assertEqual([call.args[0] for call in setup.call_args_list], ["start", "status", "status"])
        self.assertEqual(setup.call_args_list[-1].kwargs, {"job_id": job["id"]})
        sleep.assert_called_once()

    def test_setup_wait_timeout_keeps_same_running_job(self):
        job = {"id": "a" * 32, "action": "start", "state": "running"}
        pending = {"ok": True, "jobs": [job], "service": {"ready": False}}
        with patch.object(self.manager, "_setup_once", return_value=pending) as setup, \
             patch.object(wda_setup.time, "monotonic", side_effect=[0, 20]), \
             patch.object(wda_setup.time, "sleep") as sleep:
            result = self.manager.setup("status", job_id=job["id"], wait_seconds=20)
        self.assertEqual(result["wait_reason"], "timeout")
        self.assertEqual(result["jobs"][0]["state"], "running")
        self.assertTrue(all(call.args[0] == "status" for call in setup.call_args_list))
        sleep.assert_not_called()

    def test_setup_wait_recovery_requires_serving_phase(self):
        job = {"id": "a" * 32, "action": "recover", "state": "running", "recovery_phase": "stopping"}
        pending = {"ok": True, "jobs": [job], "service": {"ready": True}}
        serving = {"ok": True, "jobs": [{**job, "recovery_phase": "serving"}], "service": {"ready": True}}
        with patch.object(self.manager, "_setup_once", side_effect=[pending, pending, serving]), \
             patch.object(wda_setup.time, "sleep") as sleep:
            result = self.manager.setup("status", job_id=job["id"], wait_seconds=20)
        self.assertEqual(result["jobs"][0]["recovery_phase"], "serving")
        self.assertEqual(result["wait_reason"], "service_ready")
        sleep.assert_called_once()

    def test_setup_wait_returns_failed_job_without_retry(self):
        job = {"id": "a" * 32, "action": "start", "state": "failed", "log_tail": "signing failed"}
        with patch.object(self.manager, "_setup_once", side_effect=[
                {"ok": True, "job_id": job["id"]},
                {"ok": True, "jobs": [job], "service": {"ready": False}}]), \
             patch.object(wda_setup.time, "sleep") as sleep:
            result = self.manager.setup("start")
        self.assertEqual(result["wait_reason"], "job_finished")
        self.assertEqual(result["job"]["log_tail"], "signing failed")
        sleep.assert_not_called()

    def test_setup_wait_rejects_invalid_budget_before_start(self):
        with patch.object(self.manager, "_setup_once") as setup:
            for budget in (-1, 31, True, "20", float("nan")):
                self.assertFalse(self.manager.setup("start", wait_seconds=budget)["ok"])
            self.assertFalse(self.manager.setup("build", wait_seconds=20)["ok"])
        setup.assert_not_called()

    def test_config_requires_explicit_signing_values_and_safe_ports(self):
        self.assertFalse(self.manager.setup("configure")["ok"])
        self.assertFalse(self.configure(local_port=80)["ok"])
        self.assertFalse(self.configure(local_port=True)["ok"])
        self.assertFalse(self.configure(local_port=18101)["ok"])
        self.assertFalse(self.manager.setup("configure", udid="id; touch /tmp/pwn", team_id="ABCDE12345", bundle_id="com.example.runner")["ok"])
        self.assertFalse(self.configure(apple_password="secret")["ok"])
        result = self.configure()
        self.assertTrue(result["ok"])
        self.assertEqual(result["config"]["local_port"], 18100)
        self.assertTrue(result["config"]["source_dir"].startswith(str(self.manager.state_dir)))
        self.assertEqual((self.manager.state_dir / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.state_dir.stat().st_mode & 0o777, 0o700)

    def test_runtime_rejects_repository_and_non_loopback_url(self):
        with self.assertRaises(ValueError):
            wda_setup.SetupManager(wda_setup.PLUGIN_ROOT / "private-runtime")
        for url in ("http://example.org:8100", "http://user:password@localhost:18100", "http://localhost:18100/path", "https://localhost:18100"):
            with self.assertRaises(ValueError):
                wda_setup.SetupManager(Path(self.temp.name) / "other", url)
        other_repository = Path(self.temp.name) / "other-repository"
        (other_repository / ".git").mkdir(parents=True)
        with self.assertRaises(ValueError):
            wda_setup.SetupManager(other_repository / "runtime")

    def test_current_and_legacy_devicectl_json(self):
        current = {"result": {"devices": [
            {"identifier": "core-device", "properties": {"hardware": {"reality": "physical", "deviceType": "iPhone", "udid": "UDID-REAL"},
             "state": {"name": "Phone", "developerModeStatus": {"enabled": {"mode": 1}}},
             "connection": {"pairingState": "paired", "state": "connected", "transportType": "wired"},
             "software": {"osVersionNumber": {"stringValue": "26.7.1"}}}},
            {"properties": {"hardware": {"reality": "simulated", "deviceType": "iPhone"}}}
        ]}}
        devices = wda_setup.SetupManager.parse_devices(current)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["udid"], "UDID-REAL")
        self.assertEqual(devices[0]["developer_mode"], "enabled")
        self.assertEqual(devices[0]["ios_version"], "26.7.1")
        legacy = {"result": {"devices": [{"identifier": "core", "hardwareProperties": {"reality": "physical", "deviceType": "iPhone", "udid": "LEGACY-UDID"},
                  "deviceProperties": {"name": "Legacy", "developerModeStatus": "disabled", "osVersionNumber": "17.0"},
                  "connectionProperties": {"pairingState": "unpaired", "tunnelState": "disconnected"}}]}}
        parsed = wda_setup.SetupManager.parse_devices(legacy)[0]
        self.assertEqual(parsed["developer_mode"], "disabled")
        self.assertEqual(parsed["pairing_state"], "unpaired")
        self.assertEqual(parsed["unlocked"], "user_check_required")

    def test_job_status_redacts_and_reports_actionable_signing_failure(self):
        job = self.job()
        job["state"] = "failed"
        log = self.manager.state_dir / "logs" / (job["id"] + ".log")
        log.write_text("Apple ID someone@example.org password=supersecret\nAuthorization: Bearer private-token\nNo profiles for app. Provisioning profile expired.\n")
        result = self.manager._job_status(job)
        self.assertNotIn("someone@example.org", result["log_tail"])
        self.assertNotIn("supersecret", result["log_tail"])
        self.assertNotIn("private-token", result["log_tail"])
        self.assertNotIn("owner_token", result)
        self.assertTrue(any("expired" in hint for hint in result["next_steps"]))

    def test_only_owned_process_group_can_be_stopped(self):
        owned, stale = self.job(), self.job(token="old-token", pid=12346)
        stale["id"] = "b" * 32
        for job in (owned, stale):
            wda_setup._write_json(self.manager._job_path(job["id"]), job)
        command = f"python {wda_setup.__file__} --worker {self.manager.state_dir} {owned['id']} {owned['owner_token']}"
        with patch.object(wda_setup.os, "getpgid", side_effect=lambda pid: pid), \
             patch.object(wda_setup, "_run", return_value={"ok": True, "stdout": command}), \
             patch.object(wda_setup.os, "killpg") as kill:
            result = self.manager.setup("stop")
        kill.assert_called_once_with(12345, signal.SIGTERM)
        self.assertEqual(result["stopped_jobs"], [owned["id"]])
        self.assertEqual(result["unverified_jobs_preserved"], [stale["id"]])

    def test_worker_ownership_survives_installation_path_change(self):
        job = self.job()
        original_entrypoint = "/private/source checkout/server/wda_setup.py"
        self.assertNotEqual(original_entrypoint, str(Path(wda_setup.__file__).resolve()))
        job["worker_entrypoint"] = original_entrypoint
        command = f"python {original_entrypoint} --worker {self.manager.state_dir} {job['id']} {job['owner_token']} --base-url http://127.0.0.1:18100"
        with patch.object(wda_setup.os, "getpgid", return_value=job["pid"]), \
             patch.object(wda_setup, "_run", return_value={"ok": True, "stdout": command}):
            self.assertTrue(self.manager._owned(job))
            job["worker_entrypoint"] = "/different/cache/server/wda_setup.py"
            self.assertFalse(self.manager._owned(job))
            job["worker_entrypoint"] = original_entrypoint
            job["owner_token"] = "different-marker"
            self.assertFalse(self.manager._owned(job))
            for invalid in ("", "server/wda_setup.py", "/private/source/other.py", None):
                job["worker_entrypoint"] = invalid
                self.assertFalse(self.manager._owned(job))
        job["worker_entrypoint"] = original_entrypoint
        job["owner_token"] = "owned-marker"
        prefixed_command = command.replace(original_entrypoint, "/different-prefix" + original_entrypoint)
        with patch.object(wda_setup.os, "getpgid", return_value=job["pid"]), \
             patch.object(wda_setup, "_run", return_value={"ok": True, "stdout": prefixed_command}):
            self.assertFalse(self.manager._owned(job))

    def test_source_reuse_checks_pin_and_preserves_checkout(self):
        source = Path(self.temp.name) / "external-source"
        (source / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        (source / "WebDriverAgent.xcodeproj/project.pbxproj").write_text("fixture")
        responses = [{"ok": True, "stdout": "wrong-head\n"}]
        with patch.object(wda_setup, "_run", side_effect=responses):
            self.assertFalse(self.configure(source_dir=str(source))["ok"])
        self.assertEqual((source / "WebDriverAgent.xcodeproj/project.pbxproj").read_text(), "fixture")
        responses = [{"ok": True, "stdout": wda_setup.WDA_COMMIT + "\n"}, {"ok": True, "stdout": ""}]
        with patch.object(wda_setup, "_run", side_effect=responses):
            self.assertTrue(self.configure(source_dir=str(source))["ok"])

    def test_start_requires_matching_successful_build(self):
        self.configure()
        with patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(self.manager, "_probe_status", return_value={"ready": False}), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/usr/bin/xcodebuild"):
            result = self.manager.setup("start", wait_seconds=0)
        self.assertFalse(result["ok"])
        self.assertIn("build successfully", result["error"])

    def test_start_reuses_ready_external_service_without_spawning(self):
        self.configure()
        with patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(self.manager, "_probe_status", return_value={"ready": True, "reachable": True}), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/usr/bin/xcodebuild"), \
             patch.object(self.manager, "_create_job") as create:
            result = self.manager.setup("start", wait_seconds=0)
        self.assertTrue(result["already_ready"])
        create.assert_not_called()

    def prepare_start(self, port):
        self.manager = wda_setup.SetupManager(Path(self.temp.name) / "state", f"http://127.0.0.1:{port}")
        result = self.configure(local_port=port)
        self.assertTrue(result["ok"], result)
        wda_setup._write_json(self.manager.state_dir / "build.json", {
            "config_fingerprint": wda_setup._fingerprint(self.manager.config), "commit": wda_setup.WDA_COMMIT,
        })

    def start_without_device(self):
        # Exercise actual TCP availability logic while replacing Xcode, Node
        # discovery and worker creation; no real device or process is touched.
        with patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(self.manager, "_probe_status", return_value={"ready": False}), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/test/tool"), \
             patch.object(wda_setup, "_run", return_value={"ok": True, "stdout": "v24.18.0"}), \
             patch.object(self.manager, "_create_job", return_value={"ok": True, "job_id": "fixture"}) as create:
            result = self.manager.setup("start", wait_seconds=0)
        return result, create

    def test_start_reuses_port_after_closed_forward_connection(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        client = socket.create_connection(("127.0.0.1", port), timeout=2)
        accepted, _ = listener.accept()
        # The server side closes first and owns TIME_WAIT, as a stopped USB
        # forward may do. A bare bind reproduces the original false rejection.
        accepted.close()
        self.assertEqual(client.recv(1), b"")
        client.close()
        listener.close()
        with socket.socket() as bare_probe:
            with self.assertRaises(OSError):
                bare_probe.bind(("127.0.0.1", port))
        self.prepare_start(port)
        result, create = self.start_without_device()
        self.assertTrue(result["ok"], result)
        create.assert_called_once()

    def test_start_refuses_existing_listener_without_stopping_it(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            listener.listen(1)
            self.prepare_start(port)
            result, create = self.start_without_device()
            self.assertFalse(result["ok"])
            self.assertIn("Local forward port is occupied", result["error"])
            self.assertTrue(any("do not kill unrelated" in hint for hint in result["next_steps"]))
            create.assert_not_called()
            with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
                accepted, _ = listener.accept()
                accepted.close()

    def test_xctest_authorization_failure_has_owned_restart_diagnostic(self):
        hints = wda_setup._diagnose("Error Domain=XCTDaemonErrorDomain Code=41: Not authorized for performing UI testing actions")
        self.assertTrue(any("status.ready is true" in hint and "owned start job" in hint for hint in hints))
        self.assertTrue(any("Preserve external services" in hint and "pua_ready" in hint for hint in hints))

    def test_build_command_uses_explicit_build_for_testing_no_shell(self):
        self.configure()
        config = self.manager.config
        command = self.manager._build_command(config, "build-for-testing")
        self.assertEqual(command[-1], "build-for-testing")
        self.assertIn("DEVELOPMENT_TEAM=ABCDE12345", command)
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER=com.example.wdarunner", command)
        self.assertIn("id=" + config["udid"], command)
        self.assertIn(str(self.manager.state_dir / "derived_data"), command)
        self.assertIn("USE_PORT=8100", command)

    def test_actual_worker_completes_failure_without_blocking_caller(self):
        # Unsupported internal action exercises a real worker, private job output,
        # ownership handshake and persistence without touching Xcode or a phone.
        started = time.monotonic()
        result = self.manager._create_job("unsupported-test-fixture", {})
        self.assertLess(time.monotonic() - started, 2)
        path = self.manager._job_path(result["job_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = wda_setup._read_json(path, {})
            if job.get("state") == "failed":
                break
            time.sleep(0.02)
        self.assertEqual(job["state"], "failed")
        self.assertIn("Unsupported worker action", job["error"])
        self.assertEqual(job["worker_entrypoint"], str(Path(wda_setup.__file__).resolve()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.manager.state_dir / "logs" / (job["id"] + ".log")).stat().st_mode & 0o777, 0o600)
        # Reap this test's direct child; production callers keep jobs independent.
        self.manager._workers[job["id"]].wait(timeout=3)

    def test_status_rejects_path_traversal_job_ids(self):
        self.assertFalse(self.manager.setup("status", job_id="../../config")["ok"])
        self.assertFalse(self.manager.setup("execute", command="echo unsafe")["ok"])

    def test_completed_worker_cleans_up_its_orphan_descendant(self):
        # Emulate a command leaving a TERM-resistant child in the job group.
        # This verifies cleanup without running Xcode, touching any real device,
        # or signalling a process from outside this test's worker group.
        bin_dir = Path(self.temp.name) / "bin"
        bin_dir.mkdir()
        source = Path(self.temp.name) / "source"
        (source / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        (source / "WebDriverAgent.xcodeproj/project.pbxproj").write_text("fixture")
        git = bin_dir / "git"
        git.write_text(f"#!{sys.executable}\nimport sys\nif 'rev-parse' in sys.argv: print({wda_setup.WDA_COMMIT!r})\n")
        xcode = bin_dir / "xcodebuild"
        xcode.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\nfrom pathlib import Path\n"
                         "child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(120)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                         "Path('child.pid').write_text(str(child.pid))\ntime.sleep(0.15)\n")
        git.chmod(0o700)
        xcode.chmod(0o700)
        config = {"udid": "00008150-ABCDEF0123456789", "team_id": "ABCDE12345", "bundle_id": "com.example.runner",
                  "source_dir": str(source), "local_port": 18100, "device_port": 8100}
        with patch.dict(os.environ, {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}):
            result = self.manager._create_job("build", config)
        process = self.manager._workers[result["job_id"]]
        process.wait(timeout=10)
        job = wda_setup._read_json(self.manager._job_path(result["job_id"]))
        self.assertEqual(job["state"], "succeeded", job.get("error"))
        child_pid = int((source / "child.pid").read_text())
        descendant = wda_setup._run(["ps", "-p", str(child_pid), "-o", "stat="], timeout=2)
        self.assertTrue(not descendant["stdout"].strip() or descendant["stdout"].strip().startswith("Z"))

    def test_node_supported_matches_locked_dependency_engines(self):
        for version in ("v20.19.0", "v20.20.1", "22.12.0", "v24.18.0", "v26.0.0"):
            self.assertTrue(wda_setup._node_supported(version))
        for version in ("v18.20.0", "v20.18.1", "v21.9.0", "v22.11.0", "v23.0.0", "unknown"):
            self.assertFalse(wda_setup._node_supported(version))

    def recovery_fixture(self):
        self.assertTrue(self.configure(local_port=self.manager.port)["ok"])
        config = self.manager.config
        wda_setup._write_json(self.manager.state_dir / "build.json", {
            "config_fingerprint": wda_setup._fingerprint(config), "commit": wda_setup.WDA_COMMIT,
        })
        job = self.job()
        job.update(config=config, worker_entrypoint=str(Path(wda_setup.__file__).resolve()), base_url=self.manager.base_url)
        wda_setup._write_json(self.manager._job_path(job["id"]), job)
        return job

    def recovery_run(self, job, listener_pid=23456):
        def run(argv, **kwargs):
            if argv[0] == "lsof":
                return {"ok": True, "code": 0, "stdout": f"p{listener_pid}\nn127.0.0.1:{self.manager.port}\n", "stderr": ""}
            if argv[0] == "ps":
                return {"ok": True, "code": 0, "stdout": f"python {job['worker_entrypoint']} --worker {self.manager.state_dir} {job['id']} {job['owner_token']} --base-url {self.manager.base_url}", "stderr": ""}
            if argv == ["node", "--version"]:
                return {"ok": True, "code": 0, "stdout": "v24.18.0", "stderr": ""}
            raise AssertionError(argv)
        return run

    def test_recovery_queues_owned_listener_without_waiting_or_signalling(self):
        job = self.recovery_fixture()
        with patch.object(wda_setup, "_run", side_effect=self.recovery_run(job)), \
             patch.object(wda_setup.os, "getpgid", return_value=job["pid"]), \
             patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/fixture/tool"), \
             patch.object(self.manager, "_create_job", return_value={"ok": True, "job_id": "b" * 32, "job": {"state": "queued"}}) as create, \
             patch.object(wda_setup.os, "killpg") as kill:
            result = self.manager.recover()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovery"]["state"], "queued")
        self.assertEqual(result["recovery"]["recovery_of"], job["id"])
        create.assert_called_once()
        target = create.call_args.kwargs["recovery_target"]
        self.assertEqual(target["pid"], job["pid"])
        self.assertEqual(target["owner_token"], job["owner_token"])
        self.assertEqual(target["config_fingerprint"], wda_setup._fingerprint(self.manager.config))
        kill.assert_not_called()

    def test_recovery_refuses_external_listener_and_preserves_real_tcp_service(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            self.prepare_start(port)
            job = self.recovery_fixture()
            with patch.object(wda_setup, "_run", side_effect=self.recovery_run(job)), \
                 patch.object(wda_setup.os, "getpgid", side_effect=lambda pid: job["pid"] if pid == job["pid"] else 99999), \
                 patch.object(self.manager, "_create_job") as create, \
                 patch.object(wda_setup.os, "killpg") as kill:
                result = self.manager.recover()
            self.assertFalse(result["ok"])
            self.assertEqual(result["recovery"]["state"], "manual")
            create.assert_not_called()
            kill.assert_not_called()
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                accepted, _ = listener.accept()
                accepted.close()

    def test_recovery_refuses_job_configuration_or_worker_endpoint_mismatch(self):
        job = self.recovery_fixture()
        for mismatch in ("config", "base_url", "command"):
            changed = dict(job)
            if mismatch == "config":
                changed["config"] = dict(job["config"], device_port=8101)
            elif mismatch == "base_url":
                changed["base_url"] = "http://127.0.0.1:18101"
            wda_setup._write_json(self.manager._job_path(job["id"]), changed)
            run = self.recovery_run(changed)
            if mismatch == "command":
                def run(argv, **kwargs):
                    value = self.recovery_run(changed)(argv, **kwargs)
                    if argv[0] == "ps":
                        value["stdout"] = value["stdout"].replace(self.manager.base_url, "http://127.0.0.1:18101")
                    return value
            with patch.object(wda_setup, "_run", side_effect=run), \
                 patch.object(wda_setup.os, "getpgid", return_value=job["pid"]), \
                 patch.object(self.manager, "_create_job") as create, \
                 patch.object(wda_setup.os, "killpg") as kill:
                result = self.manager.recover()
            self.assertEqual(result["recovery"]["state"], "manual", mismatch)
            create.assert_not_called()
            kill.assert_not_called()

    def test_recovery_deduplicates_persisted_queued_or_running_job(self):
        original = self.recovery_fixture()
        for state in ("queued", "running"):
            job = dict(original, id="b" * 32, action="recover", state=state, created_at=wda_setup._now(), recovery_of=original["id"])
            wda_setup._write_json(self.manager._job_path(job["id"]), job)
            with patch.object(self.manager, "_owned", return_value=True), \
                 patch.object(self.manager, "_create_job") as create, \
                 patch.object(wda_setup.os, "killpg") as kill:
                result = self.manager.recover()
            self.assertTrue(result["already_running"])
            self.assertEqual(result["job_id"], job["id"])
            self.assertEqual(result["recovery"]["state"], state)
            create.assert_not_called()
            kill.assert_not_called()

    def test_recovery_can_restart_previous_recovered_service_after_cooldown(self):
        job = self.recovery_fixture()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=121)).isoformat()
        job.update(action="recover", recovery_phase="serving", created_at=old, service_started_at=old, recovery_of="c" * 32)
        wda_setup._write_json(self.manager._job_path(job["id"]), job)
        with patch.object(wda_setup, "_run", side_effect=self.recovery_run(job)), \
             patch.object(wda_setup.os, "getpgid", return_value=job["pid"]), \
             patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/fixture/tool"), \
             patch.object(self.manager, "_create_job", return_value={"ok": True, "job_id": "b" * 32, "job": {"state": "queued"}}) as create:
            result = self.manager.recover()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovery"]["recovery_of"], job["id"])
        self.assertEqual(create.call_args.kwargs["recovery_target"]["id"], job["id"])

    def test_recovery_cooldown_runs_from_service_start_or_failure_completion(self):
        job = self.recovery_fixture()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=500)).isoformat()
        for state, timestamp in (("running", "service_started_at"), ("failed", "completed_at")):
            recovery = dict(job, id="b" * 32, action="recover", state=state, recovery_phase="serving", created_at=old)
            recovery[timestamp] = wda_setup._now()
            wda_setup._write_json(self.manager._job_path(recovery["id"]), recovery)
            with patch.object(self.manager, "_create_job") as create:
                result = self.manager.recover()
            self.assertEqual(result["recovery"]["state"], "cooldown")
            create.assert_not_called()

    def test_pending_recovery_is_read_only_and_excludes_serving_services(self):
        original = self.recovery_fixture()
        for state, phase, expected in (("queued", "queued", True), ("running", "stopping", True),
                                       ("running", "starting", True), ("running", "serving", False),
                                       ("failed", "starting", False)):
            job = dict(original, id="b" * 32, action="recover", state=state, recovery_phase=phase, recovery_of=original["id"])
            path = self.manager._job_path(job["id"])
            wda_setup._write_json(path, job)
            before = path.read_bytes()
            with patch.object(self.manager, "_owned", return_value=True), \
                 patch.object(self.manager, "_create_job") as create, \
                 patch.object(wda_setup.os, "killpg") as kill:
                result = self.manager.pending_recovery()
            self.assertEqual(result is not None, expected)
            if expected:
                self.assertEqual(result["job_id"], job["id"])
                self.assertEqual(result["phase"], phase)
                self.assertNotIn("owner_token", result)
            self.assertEqual(before, path.read_bytes())
            create.assert_not_called()
            kill.assert_not_called()

    def test_recent_queued_recovery_without_pid_deduplicates_during_publication(self):
        original = self.recovery_fixture()
        job = dict(original, id="b" * 32, action="recover", state="queued", pid=None,
                   created_at=wda_setup._now(), recovery_phase="queued", recovery_of=original["id"])
        wda_setup._write_json(self.manager._job_path(job["id"]), job)
        restarted = wda_setup.SetupManager(self.manager.state_dir, self.manager.base_url)
        with patch.object(restarted, "_create_job") as create:
            pending = restarted.pending_recovery()
            result = restarted.recover()
        self.assertEqual(pending["job_id"], job["id"])
        self.assertTrue(result["already_running"])
        self.assertEqual(result["job"]["state"], "queued")
        create.assert_not_called()

    def test_orphan_queued_recovery_without_pid_is_interrupted_then_expires(self):
        original = self.recovery_fixture()
        queued = dict(original, id="b" * 32, action="recover", state="queued", pid=None,
                      created_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=6)).isoformat(),
                      recovery_phase="queued", recovery_of=original["id"])
        wda_setup._write_json(self.manager._job_path(queued["id"]), queued)
        restarted = wda_setup.SetupManager(self.manager.state_dir, self.manager.base_url)
        self.assertIsNone(restarted.pending_recovery())
        status = restarted._job_status(queued)
        self.assertEqual(status["state"], "interrupted")
        self.assertIn("no unrelated process", status["error"])
        with patch.object(restarted, "_create_job") as create:
            result = restarted.recover()
        self.assertEqual(result["recovery"]["state"], "cooldown")
        create.assert_not_called()
        queued["created_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=121)).isoformat()
        wda_setup._write_json(self.manager._job_path(queued["id"]), queued)
        with patch.object(wda_setup, "_run", side_effect=self.recovery_run(original)), \
             patch.object(wda_setup.os, "getpgid", return_value=original["pid"]), \
             patch.object(restarted, "_source", return_value=Path(self.temp.name)), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/fixture/tool"), \
             patch.object(restarted, "_create_job", return_value={"ok": True, "job_id": "c" * 32, "job": {"state": "queued"}}) as create:
            result = restarted.recover()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["recovery"]["recovery_of"], original["id"])
        create.assert_called_once()

    def test_unpublished_job_with_invalid_creation_date_never_stays_pending(self):
        original = self.recovery_fixture()
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        for created in (None, "invalid", 123, future, "2026-10-06T00:00:00"):
            job = dict(original, id="b" * 32, action="recover", state="queued", pid=None,
                       recovery_phase="queued", created_at=created)
            wda_setup._write_json(self.manager._job_path(job["id"]), job)
            self.assertIsNone(self.manager.pending_recovery(), created)
            self.assertEqual(self.manager._job_status(job)["state"], "interrupted", created)
        # Missing timestamps do not turn an honestly owned live worker into an
        # orphan: actual process ownership remains the authoritative evidence.
        job.update(pid=12345)
        with patch.object(self.manager, "_owned", return_value=True):
            self.assertEqual(self.manager._job_status(job)["state"], "queued")

    def test_recovery_failed_job_persists_cooldown_across_managers(self):
        original = self.recovery_fixture()
        recent = dict(original, id="b" * 32, action="recover", state="failed", created_at=wda_setup._now(), error="fixture")
        wda_setup._write_json(self.manager._job_path(recent["id"]), recent)
        restarted_manager = wda_setup.SetupManager(self.manager.state_dir, self.manager.base_url)
        with patch.object(restarted_manager, "_create_job") as create:
            result = restarted_manager.recover()
        self.assertFalse(result["ok"])
        self.assertEqual(result["recovery"]["state"], "cooldown")
        self.assertGreater(result["recovery"]["retry_after_seconds"], 0)
        self.assertLessEqual(result["recovery"]["retry_after_seconds"], 121)
        create.assert_not_called()
        recent["created_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=121)).isoformat()
        wda_setup._write_json(self.manager._job_path(recent["id"]), recent)
        with patch.object(self.manager, "_owns_listener", return_value=False):
            result = self.manager.recover()
        self.assertEqual(result["recovery"]["state"], "manual")

    def test_listener_ownership_requires_parseable_loopback_process_fields(self):
        for output, expected in (("p123\nn127.0.0.1:18100\n", [123]), ("p123\nn[::1]:18100\n", [123]),
                                 ("p123\nn*:18100\n", None), ("n127.0.0.1:18100\n", None), ("pbad\nn127.0.0.1:18100\n", None)):
            with patch.object(wda_setup, "_run", return_value={"ok": True, "code": 0, "stdout": output, "stderr": ""}):
                self.assertEqual(self.manager._listener_pids(), expected)
        for value, expected in (({"ok": False, "code": 1, "stdout": "", "stderr": ""}, []),
                                ({"ok": False, "code": None, "stdout": "", "stderr": "lsof unavailable"}, None)):
            with patch.object(wda_setup, "_run", return_value=value):
                self.assertEqual(self.manager._listener_pids(), expected)

    def recovery_worker_job(self, original):
        snapshot = {key: original.get(key) for key in ("id", "pid", "owner_token", "worker_entrypoint")}
        snapshot.update(config_fingerprint=wda_setup._fingerprint(original["config"]), base_url=self.manager.base_url)
        return dict(original, id="b" * 32, action="recover", recovery_of=original["id"], recovery_target=snapshot)

    def test_recovery_worker_revalidates_token_and_listener_before_any_signal(self):
        original = self.recovery_fixture()
        job = self.recovery_worker_job(original)
        for mismatch in ("token", "listener"):
            changed = dict(original)
            if mismatch == "token":
                changed["owner_token"] = "different-owner"
            wda_setup._write_json(self.manager._job_path(original["id"]), changed)
            with patch.object(self.manager, "_owns_listener", return_value=mismatch != "listener"), \
                 patch.object(wda_setup.os, "killpg") as kill:
                with self.assertRaisesRegex(ValueError, "no process was signalled"):
                    self.manager._stop_recovery_target(job)
            kill.assert_not_called()

    def test_recovery_worker_waits_for_owned_group_and_port_then_preserves_new_owner(self):
        original = self.recovery_fixture()
        job = self.recovery_worker_job(original)
        with patch.object(self.manager, "_owns_listener", return_value=True), \
             patch.object(self.manager, "_listener_pids", side_effect=[[23456], []]), \
             patch.object(self.manager, "_group_running", return_value=False), \
             patch.object(wda_setup.os, "getpgid", return_value=original["pid"]), \
             patch.object(wda_setup.os, "killpg") as kill, \
             patch.object(wda_setup.time, "sleep"):
            self.manager._stop_recovery_target(job)
        kill.assert_called_once_with(original["pid"], signal.SIGTERM)

    def test_recovery_worker_preserves_service_that_takes_port_after_old_job_stops(self):
        original = self.recovery_fixture()
        job = self.recovery_worker_job(original)
        with patch.object(self.manager, "_owns_listener", return_value=True), \
             patch.object(self.manager, "_listener_pids", return_value=[99999]), \
             patch.object(wda_setup.os, "getpgid", return_value=99999), \
             patch.object(wda_setup.os, "killpg") as kill:
            with self.assertRaisesRegex(ValueError, "Another service"):
                self.manager._stop_recovery_target(job)
        kill.assert_called_once_with(original["pid"], signal.SIGTERM)

    def test_recovery_worker_timeout_never_escalates_or_starts_another_forward(self):
        original = self.recovery_fixture()
        job = self.recovery_worker_job(original)
        clock = iter(range(12))
        with patch.object(self.manager, "_owns_listener", return_value=True), \
             patch.object(self.manager, "_listener_pids", return_value=[23456]), \
             patch.object(wda_setup.os, "getpgid", return_value=original["pid"]), \
             patch.object(wda_setup.os, "killpg") as kill, \
             patch.object(wda_setup.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(wda_setup.time, "sleep"):
            with self.assertRaisesRegex(ValueError, "within 10 seconds"):
                self.manager._stop_recovery_target(job)
        kill.assert_called_once_with(original["pid"], signal.SIGTERM)

    def test_recovery_rejects_missing_build_before_queueing_or_stopping(self):
        original = self.recovery_fixture()
        (self.manager.state_dir / "build.json").unlink()
        with patch.object(self.manager, "_owns_listener", return_value=True), \
             patch.object(self.manager, "_source", return_value=Path(self.temp.name)), \
             patch.object(self.manager, "_create_job") as create, \
             patch.object(wda_setup.os, "killpg") as kill:
            result = self.manager.recover()
        self.assertFalse(result["ok"])
        self.assertIn("build successfully", result["error"])
        create.assert_not_called()
        kill.assert_not_called()

    def test_recovery_worker_restarts_despite_ready_status_and_uses_saved_build(self):
        original = self.recovery_fixture()
        job = self.recovery_worker_job(original)
        job.update(pid=os.getpid(), owner_token="fixture-worker", state="queued")
        wda_setup._write_json(self.manager._job_path(job["id"]), job)
        forward = self.manager.state_dir / "runtime/forward"
        (forward / "node_modules/appium-ios-device").mkdir(parents=True)
        lock_hash = wda_setup.hashlib.sha256((wda_setup.PLUGIN_ROOT / "tooling/package-lock.json").read_bytes()).hexdigest()
        wda_setup._write_json(forward / "installed.json", {"lock_hash": lock_hash})
        first = MagicMock()
        first.stdout = io.StringIO("")
        first.poll.side_effect = [None, None, 1, 1]
        second = MagicMock()
        second.stdout = io.StringIO("")
        second.poll.side_effect = [None, 1]
        with patch.object(wda_setup.SetupManager, "_source", return_value=Path(self.temp.name)), \
             patch.object(wda_setup.SetupManager, "_stop_recovery_target") as stop, \
             patch.object(wda_setup.SetupManager, "_probe_status", return_value={"ready": True}) as healthy, \
             patch.object(wda_setup.subprocess, "Popen", side_effect=[first, second]) as launch, \
             patch.object(wda_setup.signal, "signal"), \
             patch.object(wda_setup.os, "getpgrp", return_value=-1), \
             patch.object(wda_setup.time, "sleep"):
            result = wda_setup._worker(str(self.manager.state_dir), job["id"], job["owner_token"], self.manager.base_url)
        self.assertEqual(result, 1)  # Fake runner exits after proving launch.
        stop.assert_called_once()
        healthy.assert_called_once()
        self.assertEqual(launch.call_args_list[0].args[0][0], "node")
        self.assertEqual(launch.call_args_list[1].args[0][-1], "test-without-building")
        stored = wda_setup._read_json(self.manager._job_path(job["id"]))
        self.assertEqual(stored["recovery_of"], original["id"])
        self.assertEqual(stored["recovery_phase"], "serving")
        self.assertIn("service_started_at", stored)
        self.assertNotIn("recovery_target", self.manager._job_status(stored))


if __name__ == "__main__":
    unittest.main()
