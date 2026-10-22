"""Security invariants for the exact pinned WDA loopback patch and build reuse."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import wda_setup


class NetworkHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = wda_setup.SetupManager(self.root / "state")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def fixture(self):
        self.source = self.root / "upstream"
        (self.source / "WebDriverAgent.xcodeproj").mkdir(parents=True)
        self.project = self.source / "WebDriverAgent.xcodeproj/project.pbxproj"
        self.project.write_text("upstream fixture\n")
        self.git("init", "--quiet")
        self.git("-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "add", ".")
        self.git("-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "--quiet", "-m", "fixture")
        commit = self.git("rev-parse", "HEAD").decode().strip()
        self.project.write_text("loopback fixture\n")
        diff = self.git("diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color", "HEAD", "--")
        self.git("restore", ".")
        patch_file = self.root / "loopback.patch"
        patch_file.write_bytes(diff)
        for name, value in (("WDA_COMMIT", commit), ("WDA_PATCH", patch_file),
                            ("WDA_PATCH_SHA256", hashlib.sha256(diff).hexdigest())):
            scope = patch.object(wda_setup, name, value)
            scope.start()
            self.addCleanup(scope.stop)
        return {"source_dir": str(self.source)}

    def test_isolated_patch_is_exact_and_original_stays_pristine(self):
        config = self.fixture()
        hardened = self.manager._hardened_source(config)
        self.assertNotEqual(hardened, self.source)
        self.assertEqual(self.project.read_text(), "upstream fixture\n")
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual((hardened / "WebDriverAgent.xcodeproj/project.pbxproj").read_text(), "loopback fixture\n")
        self.assertEqual(self.manager._hardened_source(config), hardened)
        changed = hardened / "WebDriverAgent.xcodeproj/project.pbxproj"
        changed.write_text("unreviewed mutation\n")
        with self.assertRaisesRegex(ValueError, "differs from"):
            self.manager._hardened_source(config)
        self.assertEqual(changed.read_text(), "unreviewed mutation\n")

    def test_changed_pin_patch_or_untracked_build_input_is_rejected(self):
        config = self.fixture()
        hardened = self.manager._hardened_source(config)
        (hardened / "unreviewed.h").write_text("unexpected input\n")
        with self.assertRaisesRegex(ValueError, "differs from"):
            self.manager._verify_hardened_source(hardened)
        with patch.object(wda_setup, "WDA_COMMIT", "0" * 40):
            with self.assertRaisesRegex(ValueError, "pinned"):
                self.manager._hardened_source(config)
        with patch.object(wda_setup, "WDA_PATCH_SHA256", "0" * 64):
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self.manager._hardened_source(config)

    def test_original_local_work_is_preserved(self):
        config = self.fixture()
        self.project.write_text("user changes\n")
        with self.assertRaisesRegex(ValueError, "tracked changes"):
            self.manager._hardened_source(config)
        self.assertEqual(self.project.read_text(), "user changes\n")

    def test_original_build_marker_cannot_start_unpatched_binary(self):
        config = {"device_port": 8100}
        marker = {"commit": wda_setup.WDA_COMMIT, "config_fingerprint": wda_setup._fingerprint(config)}
        wda_setup._write_json(self.manager.state_dir / "build.json", marker)
        with self.assertRaisesRegex(ValueError, "build successfully"):
            self.manager._require_build(config)
        marker["network_patch_sha256"] = wda_setup.WDA_PATCH_SHA256
        wda_setup._write_json(self.manager.state_dir / "build.json", marker)
        self.manager._require_build(config)

    def test_worker_does_not_inherit_unsafe_listener_environment(self):
        process = MagicMock(pid=12345)
        with patch.dict(os.environ, {"USE_IP": "0.0.0.0", "TEST_RUNNER_USE_IP": "::"}), \
             patch.object(wda_setup.subprocess, "Popen", return_value=process) as launch, \
             patch.object(self.manager, "_job_status", return_value={}):
            self.manager._create_job("build", {})
        environment = launch.call_args.kwargs["env"]
        self.assertEqual(environment["USE_IP"], "127.0.0.1")
        self.assertEqual(environment["TEST_RUNNER_USE_IP"], "127.0.0.1")

    def test_status_requires_both_actual_ready_loopback_listeners(self):
        def listener(port):
            return {"policy": wda_setup.WDA_NETWORK_POLICY, "listenerReady": True,
                    "bindAddress": "127.0.0.1", "ipVersion": 4, "port": port}
        value = {"iphoneUseNetwork": {"policy": wda_setup.WDA_NETWORK_POLICY,
                                      "listeners": [listener(8100), listener(9100)]}}
        self.assertTrue(self.manager._network_hardened(value))
        self.assertFalse(self.manager._network_hardened({}))
        self.assertFalse(wda_setup.network_hardened(None))
        self.assertFalse(wda_setup.network_hardened(value, 9100, 9100))
        for change in ({"bindAddress": "0.0.0.0"}, {"bindAddress": "::"}, {"ipVersion": 6},
                       {"listenerReady": False}, {"port": []}, {"policy": "other"},
                       {"observedLocalAddress": "192.168.1.2", "observedLocalPort": 9100},
                       {"observedLocalAddress": "127.0.0.1", "observedLocalPort": 8100}):
            wrong = json.loads(json.dumps(value))
            wrong["iphoneUseNetwork"]["listeners"][1].update(change)
            self.assertFalse(self.manager._network_hardened(wrong), change)
        value["iphoneUseNetwork"]["listeners"].pop()
        self.assertFalse(self.manager._network_hardened(value))

    def test_live_unpatched_service_is_never_reused_or_stopped(self):
        config = {"udid": "00000000-0000000000000000", "team_id": "ABCDE12345",
                  "bundle_id": "com.example.wdarunner", "source_dir": str(self.root),
                  "local_port": 18100, "device_port": 8100}
        wda_setup._write_json(self.manager.state_dir / "config.json", config)
        with patch.object(self.manager, "_source", return_value=self.root), \
             patch.object(wda_setup.sys, "platform", "darwin"), \
             patch.object(wda_setup.shutil, "which", return_value="/test/tool"), \
             patch.object(self.manager, "_probe_status", return_value={"ready": True}), \
             patch.object(self.manager, "_create_job") as create, \
             patch.object(wda_setup.os, "killpg") as kill:
            result = self.manager.setup("start", wait_seconds=0)
        self.assertFalse(result["ok"])
        self.assertIn("no verified loopback policy", result["error"])
        create.assert_not_called()
        kill.assert_not_called()

    def test_bundled_patch_integrity(self):
        content = wda_setup.WDA_PATCH.read_bytes()
        self.assertEqual(hashlib.sha256(content).hexdigest(), wda_setup.WDA_PATCH_SHA256)

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("IPHONE_USE_WDA_SOURCE"),
                         "Set IPHONE_USE_WDA_SOURCE to the pristine pinned checkout for the native socket test.")
    def test_native_transport_loopback_and_ipv6_exclusion(self):
        # Uses a temporary copy, a random Mac loopback port and Apple's real
        # Network.framework. It never starts Xcode or contacts the iPhone.
        source = self.manager._hardened_source({"source_dir": os.environ["IPHONE_USE_WDA_SOURCE"]})
        routing = source / "WebDriverAgentLib/Routing"
        binary = self.root / "socket-probe"
        fixture = Path(__file__).parent / "fixtures/wda_loopback_probe.m"
        subprocess.run(["xcrun", "clang", "-fobjc-arc", "-fmodules", "-Wall", "-Wextra", "-Werror",
                        "-Wno-unused-parameter", "-framework", "Foundation", "-framework", "Network",
                        "-I", str(routing), str(routing / "FBTCPSocket.m"), str(fixture), "-o", str(binary)],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        result = subprocess.run([str(binary)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=10)
        observed = json.loads(result.stdout)[0]
        self.assertEqual(observed["bindAddress"], "127.0.0.1")
        self.assertEqual(observed["observedLocalAddress"], "127.0.0.1")
        self.assertEqual(observed["observedLocalPort"], observed["port"])
        self.assertEqual(observed["ipVersion"], 4)


if __name__ == "__main__":
    unittest.main()
