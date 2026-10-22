"""Claude Code 屏幕查看服务：令牌、来源与工具白名单，实例复用、停止与空闲退出。不需要真机。"""
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / 'claude/viewer'
SCRIPT = VIEWER / 'screen_viewer.py'


def run_viewer(state_dir, *arguments):
    environment = {**os.environ, 'IPHONE_USE_ANALYTICS': '0'}
    process = subprocess.run([sys.executable, str(SCRIPT), *arguments, '--state-dir', str(state_dir)],
                             capture_output=True, text=True, timeout=30, env=environment, check=True)
    return json.loads(process.stdout)


def wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


class ViewerServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='iphone use viewer ')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.state_dir = Path(cls.directory.name) / 'state'
        cls.started = run_viewer(cls.state_dir, 'start')
        cls.addClassCleanup(run_viewer, cls.state_dir, 'stop')
        parts = urlsplit(cls.started['url'])
        cls.port = parts.port
        cls.token = parts.query.removeprefix('token=')
        cls.origin = f'http://127.0.0.1:{cls.port}'

    def request(self, method, path, body=None, headers=None, host=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader('Host', host or f'127.0.0.1:{self.port}')
            for name, value in (headers or {}).items():
                connection.putheader(name, value)
            payload = b'' if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            if body is not None:
                connection.putheader('Content-Length', str(len(payload)))
            connection.endheaders(payload)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def call_tool(self, payload, **overrides):
        headers = {'X-Viewer-Token': self.token, 'Origin': self.origin, 'Content-Type': 'application/json', **overrides}
        return self.request('POST', '/tool', payload, {key: value for key, value in headers.items() if value is not None})

    def test_start_binds_loopback_and_reuses_the_running_instance(self):
        self.assertEqual(urlsplit(self.started['url']).hostname, '127.0.0.1')
        self.assertFalse(self.started['reused'])
        again = run_viewer(self.state_dir, 'start')
        self.assertTrue(again['reused'])
        self.assertEqual((again['pid'], again['url']), (self.started['pid'], self.started['url']))
        self.assertEqual(run_viewer(self.state_dir, 'status')['running'], True)
        self.assertEqual(oct((self.state_dir / 'claude-viewer.json').stat().st_mode & 0o777), '0o600')

    def test_host_page_requires_the_url_token(self):
        self.assertEqual(self.request('GET', '/')[0], 403)
        self.assertEqual(self.request('GET', '/?token=wrong')[0], 403)
        status, headers, body = self.request('GET', f'/?token={self.token}')
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn(b'id="widget"', body)
        self.assertNotIn(b'/*__SCRIPT__*/', body)

    def test_widget_is_the_shared_codex_screen_and_needs_the_token_header(self):
        self.assertEqual(self.request('GET', '/widget')[0], 403)
        status, _, body = self.request('GET', '/widget', headers={'X-Viewer-Token': self.token})
        self.assertEqual(status, 200)
        self.assertEqual(body, (ROOT / 'assets/phone-screen.html').read_bytes())

    def test_foreign_host_header_is_rejected(self):
        # DNS 重绑定：外部域名解析到 127.0.0.1 时 Host 不是回环地址。
        status, _, _ = self.request('GET', f'/?token={self.token}', host=f'attacker.example:{self.port}')
        self.assertEqual(status, 403)

    def test_tool_calls_need_token_same_origin_and_an_allowed_tool(self):
        frame = {'name': 'pua_screen_frame', 'arguments': {'after_seq': 0, 'last_event_id': 0}}
        self.assertEqual(self.call_tool(frame, **{'X-Viewer-Token': None})[0], 403)
        self.assertEqual(self.call_tool(frame, Origin='https://attacker.example')[0], 403)
        self.assertEqual(self.call_tool(frame, Origin=None)[0], 403)
        for name in ('pua_tap', 'pua_type_text', 'pua_setup', 'pua_ready'):
            self.assertEqual(self.call_tool({'name': name, 'arguments': {}})[0], 403, name)
        status, _, body = self.call_tool(frame)
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertFalse(result['isError'])
        self.assertEqual(result['content'], [])
        self.assertIn('frame_available', result['structuredContent'])

    def test_screen_open_returns_the_widget_initial_state(self):
        status, _, body = self.call_tool({'name': 'pua_screen', 'arguments': {'action': 'open'}})
        self.assertEqual(status, 200)
        self.assertIn('stream_id', json.loads(body)['structuredContent'])

    def test_malformed_and_oversized_requests_are_refused(self):
        self.assertEqual(self.call_tool(b'{not json')[0], 400)
        self.assertEqual(self.call_tool(b'x' * (64 * 1024 + 1))[0], 413)
        self.assertEqual(self.call_tool({'name': 'pua_screen_frame', 'arguments': []})[0], 403)
        self.assertEqual(self.call_tool({'name': 'pua_screen_frame'}, **{'Content-Type': 'text/plain'})[0], 404)


class ViewerLifecycleTests(unittest.TestCase):
    def test_stop_ends_the_process_and_removes_its_instance_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            state_dir = Path(scratch)
            started = run_viewer(state_dir, 'start')
            self.assertEqual(run_viewer(state_dir, 'stop'), {'stopped': True, 'running': False})
            self.assertFalse((state_dir / 'claude-viewer.json').exists())
            self.assertTrue(wait_until(lambda: not _alive(started['pid'])))
            self.assertEqual(run_viewer(state_dir, 'status'), {'running': False})
            self.assertEqual(run_viewer(state_dir, 'stop'), {'stopped': False, 'running': False})

    def test_idle_instance_exits_by_itself(self):
        with tempfile.TemporaryDirectory() as scratch:
            state_dir = Path(scratch)
            started = run_viewer(state_dir, 'start', '--idle-seconds', '1')
            self.assertTrue(wait_until(lambda: not (state_dir / 'claude-viewer.json').exists()))
            self.assertTrue(wait_until(lambda: not _alive(started['pid'])))

    def test_stale_instance_file_is_replaced_without_signalling_its_pid(self):
        with tempfile.TemporaryDirectory() as scratch:
            state_dir = Path(scratch)
            # 残留文件指向当前测试进程：未通过令牌证明归属，不能被当作实例停止。
            stale = {'pid': os.getpid(), 'port': 9, 'token': 'stale', 'url': 'http://127.0.0.1:9/?token=stale'}
            (state_dir / 'claude-viewer.json').write_text(json.dumps(stale))
            self.assertEqual(run_viewer(state_dir, 'stop'), {'stopped': False, 'running': False})
            (state_dir / 'claude-viewer.json').write_text(json.dumps(stale))
            started = run_viewer(state_dir, 'start')
            try:
                self.assertFalse(started['reused'])
                self.assertNotEqual(started['pid'], os.getpid())
            finally:
                run_viewer(state_dir, 'stop')


class ViewerBuildTests(unittest.TestCase):
    def test_built_page_matches_the_host_template(self):
        template = (VIEWER / 'host.html').read_text()
        before, after = template.split('/*__SCRIPT__*/')
        built = (VIEWER / 'viewer.html').read_text()
        self.assertTrue(built.startswith(before) and built.endswith(after), 'viewer.html is stale; run npm run build')
        self.assertIn('pua_screen_frame', built)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


if __name__ == '__main__':
    unittest.main()
