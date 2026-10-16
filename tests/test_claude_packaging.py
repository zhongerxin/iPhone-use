"""Claude Code 暂存包：不带 Codex 专属配置，复用同一服务与技能，且不依赖 claude CLI。"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('iphone_use_claude_packaging', ROOT / 'scripts/package_claude.py')
claude_packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(claude_packaging)


class ClaudePackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='iphone use claude ')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.stage = claude_packaging.build(Path(cls.directory.name) / 'claude')
        cls.plugin = cls.stage / 'iphone-use'

    def test_marketplace_points_at_staged_plugin(self):
        marketplace = json.loads((self.stage / '.claude-plugin/marketplace.json').read_text())
        self.assertEqual(marketplace['name'], 'iphone-use-local')
        self.assertTrue(marketplace['owner']['name'])
        [entry] = marketplace['plugins']
        self.assertEqual((entry['name'], entry['source']), ('iphone-use', './iphone-use'))
        self.assertTrue((self.stage / entry['source'] / '.claude-plugin/plugin.json').is_file())

    def test_manifest_uses_claude_plugin_root_and_shared_version(self):
        manifest = json.loads((self.plugin / '.claude-plugin/plugin.json').read_text())
        version = json.loads((ROOT / 'plugin.json').read_text())['version']
        self.assertEqual((manifest['name'], manifest['version']), ('iphone-use', version))
        # IPHONE_USE_HOST 让匿名统计区分 Claude 与 Codex（Codex 不设置，默认仍记为 Codex）。
        self.assertEqual(manifest['mcpServers'], {'iphone_use': {
            'command': 'python3', 'args': ['${CLAUDE_PLUGIN_ROOT}/server/iphone_use.py'],
            'env': {'IPHONE_USE_HOST': 'claude'}}})

    def test_codex_only_files_stay_out_of_claude_plugin(self):
        files = {path.relative_to(self.plugin).as_posix() for path in self.plugin.rglob('*') if path.is_file()}
        for name in claude_packaging.CODEX_ONLY:
            self.assertNotIn(name, files)
        self.assertFalse(any('${PLUGIN_ROOT}' in (self.plugin / name).read_text() for name in files if name.endswith('.json')))
        self.assertFalse(any(name.startswith(('tests/', '.github/', 'docs/', 'evals/')) for name in files))
        self.assertFalse(any(name.startswith('viewer/') and name.endswith(('.ts', '.mjs', '.json')) for name in files))
        for name in ('server/iphone_use.py', 'scripts/phone.py', 'tooling/forward.mjs', 'assets/phone-screen.html', 'claude/README.md'):
            self.assertIn(name, files)

    def test_shared_skills_and_claude_supplement_are_packaged(self):
        for skill in ('iphone-use', 'iphone-use-setup', 'iphone-use-claude'):
            body = (self.plugin / 'skills' / skill / 'SKILL.md').read_text()
            self.assertTrue(body.startswith('---\n'))
            self.assertRegex(body, r'(?m)^name: ' + skill + r'$')
            self.assertRegex(body, r'(?m)^description: .+')
        for skill in ('iphone-use', 'iphone-use-setup'):
            self.assertEqual((self.plugin / 'skills' / skill / 'SKILL.md').read_bytes(), (ROOT / 'skills' / skill / 'SKILL.md').read_bytes())

    def test_document_links_resolve_inside_plugin(self):
        for document in list((self.plugin / 'skills').rglob('*.md')) + [self.plugin / 'claude/README.md']:
            for target in re.findall(r'\]\(([^)]+)\)', document.read_text()):
                if '://' in target or target.startswith('#'):
                    continue
                linked = (document.parent / target.split('#')[0]).resolve()
                self.assertTrue(linked.is_relative_to(self.plugin.resolve()), str(linked))
                self.assertTrue(linked.exists(), f'{document.relative_to(self.plugin)} -> {target}')

    def test_staged_server_serves_full_catalog(self):
        requests = [
            {'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'claude-package-test', 'version': '1'}}},
            {'method': 'tools/list', 'params': {}},
        ]
        wire = '\n'.join(json.dumps({'jsonrpc': '2.0', 'id': index, **request}) for index, request in enumerate(requests, 1)) + '\n'
        environment = {**os.environ, 'IPHONE_USE_ANALYTICS': '0'}
        process = subprocess.run([sys.executable, str(self.plugin / 'server/iphone_use.py')], input=wire, capture_output=True,
                                 text=True, timeout=15, check=True, env=environment)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertTrue(all('error' not in reply for reply in replies), str(replies))
        self.assertEqual(len(replies[1]['result']['tools']), 19)

    def test_viewer_dependencies_keep_the_codex_source_package_buildable(self):
        # package.py 把 claude/ 整体打进源码包并拒绝符号链接；viewer 的 .npmrc 关闭 bin 链接。
        self.assertIn('bin-links=false', (ROOT / 'claude/viewer/.npmrc').read_text())
        self.assertEqual([path for path in (ROOT / 'claude').rglob('*') if path.is_symlink()], [])
        entries = [rel.as_posix() for _, rel in claude_packaging.load_packaging().package_files(source_package=True)]
        self.assertIn('claude/viewer/viewer.html', entries)
        self.assertFalse(any('node_modules' in name for name in entries))

    def test_screen_viewer_runs_from_the_plugin_layout(self):
        viewer = self.plugin / 'viewer'
        self.assertEqual(sorted(path.name for path in viewer.iterdir()), ['screen_viewer.py', 'viewer.html'])
        self.assertNotIn('/*__SCRIPT__*/', (viewer / 'viewer.html').read_text())
        with tempfile.TemporaryDirectory() as scratch:
            environment = {**os.environ, 'IPHONE_USE_ANALYTICS': '0'}
            process = subprocess.run([sys.executable, str(viewer / 'screen_viewer.py'), 'status', '--state-dir', scratch],
                                     capture_output=True, text=True, timeout=15, check=True, env=environment)
        self.assertEqual(json.loads(process.stdout), {'running': False})

    def test_auto_open_hooks_are_packaged_for_both_old_and_new_claude_code(self):
        hooks = json.loads((self.plugin / 'hooks/hooks.json').read_text())
        # 旧版只认 hooks 字段，缺失时报 Hook load failed；新版从 modules 加载钩子模块。
        self.assertEqual(hooks, {'hooks': {}, 'modules': ['./register.ts']})
        self.assertEqual(sorted(path.name for path in (self.plugin / 'hooks').iterdir()), ['hooks.json', 'register.ts'])
        source = (self.plugin / 'hooks/register.ts').read_text()
        for tool in ('pua_ready', 'pua_screen'):
            self.assertIn(f"{{ tool: 'mcp__plugin_iphone-use_iphone_use__{tool}' }}", source)
        self.assertIn('viewer/screen_viewer.py', source)

    def test_auto_open_hooks_pass_claude_plugin_validate_and_test(self):
        binary = os.environ.get('CLAUDE_CODE_BIN') or shutil.which('claude')
        probe = subprocess.run([binary, 'plugin', '--help'], capture_output=True, text=True, timeout=30) if binary else None
        # 旧版对未知子命令的 --help 也返回 0，按子命令列表判断是否支持。
        if not probe or not re.search(r'(?m)^\s+test \[dir\]', probe.stdout) or not re.search(r'(?m)^\s+validate ', probe.stdout):
            self.skipTest('需要支持 claude plugin test 的 Claude Code（可用 CLAUDE_CODE_BIN 指定）')
        validated = subprocess.run([binary, 'plugin', 'validate', str(self.plugin)], capture_output=True, text=True, timeout=120)
        self.assertEqual(validated.returncode, 0, validated.stdout + validated.stderr)
        with tempfile.TemporaryDirectory() as scratch:
            # 测试文件不进安装包：在临时插件目录中与钩子模块放在一起运行。
            harness = Path(scratch)
            shutil.copytree(self.plugin / 'hooks', harness / 'hooks')
            (harness / 'tests').mkdir()
            shutil.copy2(ROOT / 'claude/hooks/register.test.ts', harness / 'tests')
            (harness / '.claude-plugin').mkdir()
            (harness / '.claude-plugin/plugin.json').write_text(json.dumps({'name': 'iphone-use', 'version': '0.0.0'}))
            tested = subprocess.run([binary, 'plugin', 'test', '.'], cwd=harness, capture_output=True, text=True, timeout=180)
        self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)

    def test_rebuild_replaces_stage_and_refuses_foreign_directory(self):
        with tempfile.TemporaryDirectory() as scratch:
            stage = claude_packaging.build(Path(scratch) / 'claude')
            leftover = stage / 'iphone-use' / 'leftover.txt'
            leftover.write_text('old')
            claude_packaging.build(stage)
            self.assertFalse(leftover.exists())
            self.assertEqual(sorted(path.name for path in Path(scratch).iterdir()), ['claude'])
            foreign = Path(scratch) / 'foreign'
            foreign.mkdir()
            (foreign / 'keep.txt').write_text('keep')
            with self.assertRaises(SystemExit):
                claude_packaging.build(foreign)
            self.assertEqual((foreign / 'keep.txt').read_text(), 'keep')

    def test_install_script_is_valid_shell(self):
        subprocess.run(['sh', '-n', str(ROOT / 'scripts/install_claude.sh')], check=True)


if __name__ == '__main__':
    unittest.main()
