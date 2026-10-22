"""Exercise the shipped Android runtime without iPhone modules or a phone."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('android_packaging', ROOT / 'scripts/package_android.py')
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


class AndroidPackagingTests(unittest.TestCase):
    def test_isolated_package_serves_tools_and_widget_without_iphone_runtime(self):
        manifest = packaging.validate()
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / 'android-use'
            packaging.stage_plugin(stage)
            modules = {p.name for p in (stage / 'server').glob('*.py')}
            self.assertNotIn('iphone_use.py', modules)
            self.assertNotIn('wda_client.py', modules)
            self.assertNotIn('wda_setup.py', modules)
            self.assertNotIn('analytics.py', modules)
            self.assertFalse((stage / 'scripts/android-preview').exists())
            self.assertFalse((stage / 'VALIDATION.md').exists())
            state = Path(directory) / 'state'
            requests = [
                ('initialize', {'protocolVersion': '2025-11-25'}),
                ('tools/list', {}), ('resources/list', {}),
            ]
            def rpc(requests):
                wire = ''.join(json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) + '\n'
                               for i, (method, params) in enumerate(requests))
                result = subprocess.run([sys.executable, str(stage / 'server/android_use.py'), '--state-dir', str(state)],
                                        input=wire, capture_output=True, text=True, timeout=15, check=True)
                return [json.loads(line)['result'] for line in result.stdout.splitlines()]
            init, tools, resources = rpc(requests)
            self.assertEqual(init['serverInfo']['version'], manifest['version'])
            self.assertEqual(len(tools['tools']), 19)
            widget = rpc([('resources/read', {'uri': resources['resources'][0]['uri']})])[0]['contents'][0]
            self.assertEqual(widget['text'], (stage / 'assets/phone-screen.html').read_text())
            self.assertIn('data-platform="android"', widget['text'])

    def test_iphone_install_excludes_android_runtime_but_source_keeps_it(self):
        spec = importlib.util.spec_from_file_location('iphone_packaging', ROOT / 'scripts/package.py')
        iphone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(iphone)
        installed = {str(rel) for _, rel in iphone.package_files()}
        source = {str(rel) for _, rel in iphone.package_files(source_package=True)}
        self.assertIn('server/phone_protocol.py', installed)
        self.assertNotIn('server/android_use.py', installed)
        self.assertIn('server/android_use.py', source)
        self.assertIn('scripts/package_android.py', source)
        self.assertIn('android/.codex-plugin/plugin.json', source)


if __name__ == '__main__':
    unittest.main()
