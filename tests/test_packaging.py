import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('iphone_use_packaging', ROOT / 'scripts/package.py')
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='iphone use package ')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name).resolve() / 'checkout'
        for source, rel in packaging.package_files(source_package=True):
            target = cls.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        # Local-only material exists on disk even when it is not Git-tracked.
        for name in ('docs/private-notes.md', 'evals/cases.json', 'plugins/experiment/private.txt'):
            target = cls.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('local-only fixture')
        (cls.root / 'server/posthog.local.json').write_text('{"projectToken":"local-only-token"}')
        subprocess.run([sys.executable, str(cls.root / 'scripts/package.py')], check=True, capture_output=True, text=True)
        cls.stage = cls.root / 'dist/iphone-use'
        cls.archive = next((cls.root / 'dist').glob('*-source.zip'))

    def test_source_and_install_exclude_local_material_without_deleting_it(self):
        with zipfile.ZipFile(self.archive) as archive:
            entries = {Path(name).relative_to('iphone-use').as_posix() for name in archive.namelist()}
        installed = {p.relative_to(self.stage).as_posix() for p in self.stage.rglob('*') if p.is_file()}
        for contents in (entries, installed):
            self.assertFalse(any(p.startswith(('docs/', 'evals/', 'plugins/', 'ui/node_modules/')) for p in contents))
            self.assertNotIn('server/posthog.local.json', contents)
            self.assertIn('server/analytics.py', contents)
            self.assertIn('server/posthog.json', contents)
            self.assertIn('ANALYTICS.md', contents)
        self.assertIn('tests/test_packaging.py', entries)
        self.assertIn('.github/workflows/check.yml', entries)
        self.assertIn('scripts/benchmark.py', entries)
        self.assertNotIn('scripts/benchmark.py', installed)
        self.assertNotIn('scripts/smoke_mcp.py', installed)
        self.assertFalse(any(p.startswith(('tests/', '.github/')) for p in installed))
        self.assertEqual((self.root / 'docs/private-notes.md').read_text(), 'local-only fixture')
        self.assertTrue((self.root / 'evals/cases.json').is_file())

    def test_installed_widget_build_inputs_and_operational_links_are_complete(self):
        for source in (ROOT / 'ui').rglob('*'):
            rel = source.relative_to(ROOT)
            if source.is_file() and 'node_modules' not in rel.parts:
                self.assertEqual((self.stage / rel).read_bytes(), source.read_bytes(), str(rel))
        for name in ('assets/phone-screen.html', 'tooling/screen-stream.mjs', 'tooling/forward.mjs', 'tooling/device-transport.mjs',
                     'tooling/package-lock.json', 'scripts/phone.py', 'scripts/check_screen_ui.py'):
            self.assertEqual((self.stage / name).read_bytes(), (ROOT / name).read_bytes(), name)
        for document in list((self.stage / 'skills').rglob('*.md')) + [self.stage / 'README.md', self.stage / 'README.en.md', self.stage / 'CHANGELOG.md']:
            for target in re.findall(r'\]\(([^)]+)\)', document.read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                linked = (document.parent / target.split('#')[0]).resolve()
                self.assertTrue(linked.is_relative_to(self.stage.resolve()), str(linked))
                self.assertTrue(linked.exists(), f'{document.relative_to(self.stage)} -> {target}')

    def test_portable_source_recreates_install_and_serves_widget_resource(self):
        checkout = Path(self.directory.name) / 'extracted source'
        with zipfile.ZipFile(self.archive) as archive:
            archive.extractall(checkout)
        source_root = checkout / 'iphone-use'
        subprocess.run([sys.executable, str(source_root / 'scripts/package.py'), '--stage-only'], check=True, capture_output=True, text=True)
        rebuilt = source_root / 'dist/iphone-use'
        expected = {p.relative_to(self.stage): p.read_bytes() for p in self.stage.rglob('*') if p.is_file()}
        actual = {p.relative_to(rebuilt): p.read_bytes() for p in rebuilt.rglob('*') if p.is_file()}
        self.assertEqual(actual, expected)
        version = json.loads((rebuilt / 'plugin.json').read_text())['version']
        requests = [
            {'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'package-test', 'version': '1'}}},
            {'method': 'tools/list', 'params': {}},
            {'method': 'resources/list', 'params': {}},
            {'method': 'ping', 'params': {}},
        ]
        wire = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': i, **request}) for i, request in enumerate(requests, 1)) + '\n'
        process = subprocess.run([sys.executable, str(rebuilt / 'server/iphone_use.py')], input=wire, capture_output=True, text=True, timeout=15, check=True)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        self.assertTrue(all('error' not in reply for reply in replies), str([r.get('error') for r in replies]))
        self.assertEqual(replies[0]['result']['serverInfo']['version'], version)
        self.assertEqual(len(replies[1]['result']['tools']), 19)
        uri = replies[2]['result']['resources'][0]['uri']
        read = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'resources/read', 'params': {'uri': uri}}) + '\n'
        process = subprocess.run([sys.executable, str(rebuilt / 'server/iphone_use.py')], input=read, capture_output=True, text=True, timeout=15, check=True)
        content = json.loads(process.stdout)['result']['contents'][0]
        self.assertEqual(content['mimeType'], 'text/html;profile=mcp-app')
        self.assertEqual(content['text'], (rebuilt / 'assets/phone-screen.html').read_text())


if __name__ == '__main__':
    unittest.main()
