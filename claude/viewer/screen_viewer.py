#!/usr/bin/env python3
"""Claude Code 手机屏幕查看服务：在 127.0.0.1 上托管 Codex 同款屏幕 widget。

内置浏览器面板打开 start 返回的地址；widget 的工具调用经 POST /tool 交给与 MCP 服务
相同的 Runtime，共享运行目录中的预览状态、动作事件与跨进程操作锁。只放行 widget 使用的
三个工具，每个实例使用随机令牌，并校验 Host 与 Origin。
"""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import urllib.request

HERE = Path(__file__).resolve().parent


def plugin_root():
    """仓库内为 claude/viewer，安装包内为 <插件根目录>/viewer。"""
    for candidate in HERE.parents:
        if (candidate / 'server' / 'iphone_use.py').is_file():
            return candidate
    raise SystemExit('iPhone Use server directory not found.')


ROOT = plugin_root()
# 查看服务只在 Claude Code 中使用：匿名统计的来源记为 Claude，后台 serve 子进程继承此变量。
os.environ.setdefault('IPHONE_USE_HOST', 'claude')
sys.path.insert(0, str(ROOT / 'server'))
from iphone_use import Runtime, tool_result  # noqa: E402
from wda_setup import state_directory  # noqa: E402

VERSION = '0.1.0'
WIDGET_TOOLS = frozenset({'pua_screen', 'pua_screen_frame', 'pua_screen_action'})
INSTANCE_NAME = 'claude-viewer.json'
IDLE_SECONDS = 600
MAX_BODY_BYTES = 64 * 1024
START_TIMEOUT_SECONDS = 15
# 外壳页与 srcdoc 中的 widget 共用此策略：只允许内联脚本与样式、data/blob 图像和同源请求。
CONTENT_SECURITY_POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                           "img-src data: blob:; connect-src 'self'; frame-src 'self' about:; base-uri 'none'; form-action 'none'")


def host_page():
    """viewer.html 是 host.html 模板与 host.ts 的打包产物（npm run build）。"""
    page = HERE / 'viewer.html'
    if not page.is_file():
        raise SystemExit('Built viewer page not found; run npm run build in claude/viewer.')
    return page


def instance_path(state_dir):
    return Path(state_dir) / INSTANCE_NAME


def read_instance(state_dir):
    try:
        data = json.loads(instance_path(state_dir).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not all(isinstance(data.get(key), expected) for key, expected in
                                             (('pid', int), ('port', int), ('token', str), ('url', str))):
        return None
    return data


def write_instance(state_dir, data):
    target = instance_path(state_dir)
    temporary = target.with_name(f'.{INSTANCE_NAME}.{os.getpid()}.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream)
    os.replace(temporary, target)


def healthy(instance, timeout=2):
    """由令牌证明该端口上是本实例，而不是复用了同一端口的其他程序。"""
    if not instance:
        return False
    request = urllib.request.Request(f"http://127.0.0.1:{instance['port']}/health", headers={'X-Viewer-Token': instance['token']})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response).get('pid') == instance['pid']
    except (OSError, ValueError):
        return False


class ViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, runtime, token, idle_seconds):
        super().__init__(('127.0.0.1', 0), ViewerHandler)
        self.runtime = runtime
        self.token = token
        self.idle_seconds = idle_seconds
        self.last_request = time.monotonic()
        self.host_html = host_page().read_bytes()
        self.widget_html = (ROOT / 'assets' / 'phone-screen.html').read_bytes()

    @property
    def port(self):
        return self.server_address[1]


class ViewerHandler(BaseHTTPRequestHandler):
    server_version = 'iPhoneUseViewer/' + VERSION
    sys_version = ''

    def log_message(self, format, *args):  # noqa: A002 - 基类签名
        pass

    def _send(self, status, body=b'', content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', CONTENT_SECURITY_POLICY)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _reject(self, status=403):
        self._send(status, json.dumps({'error': 'forbidden' if status == 403 else 'not_found'}).encode())

    def _trusted_host(self):
        # 防 DNS 重绑定：只接受以本机回环地址访问本端口的请求。
        allowed = {f'127.0.0.1:{self.server.port}', f'localhost:{self.server.port}'}
        return self.headers.get('Host') in allowed

    def _token_matches(self, value):
        return isinstance(value, str) and hmac.compare_digest(value, self.server.token)

    def do_GET(self):
        self.server.last_request = time.monotonic()
        if not self._trusted_host():
            return self._reject()
        parts = urlsplit(self.path)
        if parts.path == '/':
            if not self._token_matches(parse_qs(parts.query).get('token', [''])[0]):
                return self._reject()
            return self._send(200, self.server.host_html, 'text/html; charset=utf-8')
        if not self._token_matches(self.headers.get('X-Viewer-Token')):
            return self._reject()
        if parts.path == '/widget':
            return self._send(200, self.server.widget_html, 'text/html; charset=utf-8')
        if parts.path == '/health':
            return self._send(200, json.dumps({'ok': True, 'pid': os.getpid(), 'version': VERSION}).encode())
        return self._reject(404)

    def do_POST(self):
        self.server.last_request = time.monotonic()
        if not self._trusted_host() or not self._token_matches(self.headers.get('X-Viewer-Token')):
            return self._reject()
        # 跨站页面可以猜端口但拿不到令牌；Origin 校验再挡住非本页发起的请求。
        if self.headers.get('Origin') not in (f'http://127.0.0.1:{self.server.port}', f'http://localhost:{self.server.port}'):
            return self._reject()
        if urlsplit(self.path).path != '/tool' or not (self.headers.get('Content-Type') or '').startswith('application/json'):
            return self._reject(404)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            return self._send(413, b'{"error":"payload_too_large"}')
        try:
            request = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            return self._send(400, b'{"error":"invalid_json"}')
        name = request.get('name') if isinstance(request, dict) else None
        arguments = request.get('arguments', {}) if isinstance(request, dict) else None
        if name not in WIDGET_TOOLS or not isinstance(arguments, dict):
            return self._reject()
        result = tool_result(self.server.runtime, {'name': name, 'arguments': arguments})
        self._send(200, json.dumps(result, ensure_ascii=False, allow_nan=False).encode())


def serve(state_dir, idle_seconds):
    runtime = Runtime(str(state_dir))
    server = ViewerServer(runtime, secrets.token_urlsafe(24), idle_seconds)
    instance = {'pid': os.getpid(), 'port': server.port, 'token': server.token, 'version': VERSION,
                'url': f'http://127.0.0.1:{server.port}/?token={server.token}'}
    stopping = threading.Event()

    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    def watch_idle():
        while not stopping.wait(min(5, server.idle_seconds / 2)):
            if time.monotonic() - server.last_request > server.idle_seconds:
                stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threading.Thread(target=watch_idle, name='viewer-idle', daemon=True).start()
    write_instance(state_dir, instance)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        current = read_instance(state_dir)
        if current and current.get('pid') == os.getpid():
            instance_path(state_dir).unlink(missing_ok=True)
        runtime.close()
    return 0


def start(state_dir, idle_seconds):
    current = read_instance(state_dir)
    if healthy(current):
        return {'url': current['url'], 'pid': current['pid'], 'reused': True}
    instance_path(state_dir).unlink(missing_ok=True)
    command = [sys.executable, str(Path(__file__).resolve()), 'serve', '--state-dir', str(state_dir), '--idle-seconds', str(idle_seconds)]
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise SystemExit('Viewer service exited during startup.')
        current = read_instance(state_dir)
        if current and current.get('pid') == child.pid and healthy(current):
            return {'url': current['url'], 'pid': current['pid'], 'reused': False}
        time.sleep(0.1)
    child.terminate()
    raise SystemExit('Viewer service did not become ready.')


def status(state_dir):
    current = read_instance(state_dir)
    if healthy(current):
        return {'running': True, 'url': current['url'], 'pid': current['pid']}
    return {'running': False}


def stop_instance(state_dir):
    current = read_instance(state_dir)
    # 只向令牌证明过归属的进程发信号，不按端口或残留 pid 结束进程。
    if not healthy(current):
        instance_path(state_dir).unlink(missing_ok=True)
        return {'stopped': False, 'running': False}
    os.kill(current['pid'], signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and instance_path(state_dir).exists():
        time.sleep(0.1)
    return {'stopped': True, 'running': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('command', nargs='?', default='start', choices=['start', 'status', 'stop', 'serve'])
    parser.add_argument('--state-dir', help='与 MCP 服务相同的运行目录；默认同 IPHONE_USE_STATE_DIR')
    parser.add_argument('--idle-seconds', type=int, default=IDLE_SECONDS, help='无请求多久后自动退出')
    args = parser.parse_args()
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else state_directory()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command == 'serve':
        return serve(state_dir, max(args.idle_seconds, 1))
    handlers = {'start': lambda: start(state_dir, max(args.idle_seconds, 1)), 'status': lambda: status(state_dir),
                'stop': lambda: stop_instance(state_dir)}
    print(json.dumps(handlers[args.command](), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
