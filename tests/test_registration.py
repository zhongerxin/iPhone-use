import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, call, Mock


ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location("wda_registration",ROOT/"scripts/register_mcp.py")
registration=importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory(prefix="wda install ")
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name).resolve()
        (self.root/"server").mkdir()
        (self.root/"server/iphone_use.py").write_text("# synthetic entrypoint\n")
        self.installation={"pluginId":"iphone-use@iphone-use-local","installedPath":str(self.root),"version":"0.1.6"}
        self.config_home = self.root / "codex"
        self.config_home.mkdir()
        env = patch.dict(registration.os.environ, {"CODEX_HOME": str(self.config_home)})
        env.start()
        self.addCleanup(env.stop)

    def invoke(self,data):
        with patch.object(registration.sys,"stdin",io.StringIO(json.dumps(data))), contextlib.redirect_stdout(io.StringIO()):
            registration.main()

    def test_registers_same_namespace_and_installed_entrypoint_without_phone_requests(self):
        with patch.object(registration.subprocess,"run") as run:
            self.invoke(self.installation)
        self.assertEqual(run.call_args_list, [
            call(["codex","mcp","add","iphone_use","--","python3",str(self.root/"server/iphone_use.py")],check=True),
            call(["codex","mcp","get","iphone_wda","--json"],capture_output=True,text=True,check=False)])

    def test_upgrade_removes_only_owned_mcp_and_disables_previous_plugin_preserving_other_settings(self):
        config = self.config_home / "config.toml"
        original = '[plugins."iphone-use-wda@iphone-wda-local"]\nenabled = true\n\n[plugins."other@example"]\nenabled = true\n'
        config.write_text(original)
        config.chmod(0o600)
        cache = self.root / "cache/iphone-wda-local/iphone-use-wda/0.2.7/server/iphone_wda.py"
        old = {"transport": {"type": "stdio", "args": [str(cache)]}}
        with patch.object(registration.subprocess,"run",return_value=Mock(returncode=0,stdout=json.dumps(old))) as run:
            registration.retire_previous_registration()
        run.assert_any_call(["codex","mcp","remove","iphone_wda"],check=True)
        self.assertEqual(config.read_text(), original.replace('enabled = true', 'enabled = false', 1))
        self.assertEqual(config.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.config_home.glob('.iphone-use-config-*')))

    def test_install_registers_the_explicit_locale_for_the_standard_mcp(self):
        for language in ('default','ja'):
            with self.subTest(language=language):
                (self.root/'mcp.json').write_text(json.dumps({'mcpServers':{'iphone_use':{'env':{'IPHONE_USE_LANGUAGE':language}}}}))
                with patch.object(registration.subprocess,'run') as run:
                    self.invoke(self.installation)
                self.assertEqual(run.call_args_list[0],call(['codex','mcp','add','iphone_use','--env','IPHONE_USE_LANGUAGE='+language,'--','python3',str(self.root/'server/iphone_use.py')],check=True))

    def test_unrelated_mcp_is_preserved_and_absent_or_malformed_previous_entry_is_harmless(self):
        for response in [Mock(returncode=1,stdout=''), Mock(returncode=0,stdout='invalid'),
                         Mock(returncode=0,stdout=json.dumps({"transport":{"type":"stdio","args":["/custom/server.py"]}}))]:
            with self.subTest(response=response), patch.object(registration.subprocess,"run",return_value=response) as run:
                registration.retire_previous_registration()
            self.assertEqual(run.call_count, 1)

    def test_wrong_plugin_and_missing_entrypoint_do_not_change_config(self):
        for data in ({**self.installation,"pluginId":"unrelated-plugin@example"},self.installation):
            if data==self.installation:(self.root/"server/iphone_use.py").unlink()
            with self.subTest(data=data), patch.object(registration.subprocess,"run") as run, self.assertRaises(SystemExit):
                self.invoke(data)
            run.assert_not_called()

    def test_registration_failure_propagates_instead_of_reporting_success(self):
        with patch.object(registration.subprocess,"run",side_effect=subprocess.CalledProcessError(1,["codex","mcp","add"])), self.assertRaises(subprocess.CalledProcessError):
            self.invoke(self.installation)


if __name__=="__main__":unittest.main()
