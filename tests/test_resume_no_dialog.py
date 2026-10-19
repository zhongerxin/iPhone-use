"""Automatic handoff and its resume alias require phone evidence and never invoke a host dialog."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import Runtime


class ResumeNoDialogTests(unittest.TestCase):
    def test_wait_without_phone_evidence_keeps_pause_without_host_dialog(self):
        for action in ("wait_auth", "resume"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                runtime = Runtime(directory)
                runtime.identify_device = Mock()
                try:
                    runtime.call("pua_screen", {"action": "pause"})
                    with patch("subprocess.run") as process:
                        result = runtime.call("pua_screen", {"action": action})
                    process.assert_not_called()
                    self.assertTrue(result["paused"])
                    self.assertTrue(runtime.screen.paused())
                    self.assertFalse(result.get("confirmation_required", False))
                    self.assertEqual(result["state"], "auth_target_required")
                    self.assertNotIn("dialog", json.dumps(result).lower())
                finally:
                    runtime.close()


if __name__ == "__main__":
    unittest.main()
