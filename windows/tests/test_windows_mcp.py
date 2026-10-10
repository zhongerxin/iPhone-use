"""Simulated transport tests; these do not establish real-device success."""
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from iphone_mcp import Runtime, WDAError, content
from PIL import Image


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.locked = False
        self.drop_tap = False
        fixture = self
        buffer = io.BytesIO()
        Image.new('RGB', (1206, 2622), 'white').save(buffer, format='PNG')
        screenshot = base64.b64encode(buffer.getvalue()).decode('ascii')

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_call(self):
                fixture.requests.append((self.command, self.path, self.client_address))
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                route = self.path.split('?', 1)[0]
                if route.startswith('/session/test/'):
                    route = route[len('/session/test'):]
                if route == '/wda/tap' and fixture.drop_tap:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    self.close_connection = True
                    return
                values = {'/status': {'ready': True}, '/wda/locked': fixture.locked,
                          '/window/size': {'width': 402, 'height': 874},
                          '/wda/activeAppInfo': {'bundleId': 'com.example.fixture'},
                          '/source': '<XCUIElementTypeApplication type="XCUIElementTypeApplication" bundleId="com.example.fixture" x="0" y="0" width="402" height="874"/>',
                          '/screenshot': screenshot, '/appium/settings': {}, '/wda/tap': None}
                result = {'value': values.get(route)}
                if route == '/session':
                    result['sessionId'] = 'test'
                raw = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = handle_call
            do_POST = handle_call

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.runtime = Runtime('http://127.0.0.1:' + str(self.server.server_port))

    def tearDown(self):
        self.runtime.close()
        self.server.shutdown()
        self.server.server_close()

    def test_session_and_tcp_connection_are_reused_and_screenshot_is_portable(self):
        self.assertTrue(self.runtime.call('pua_ready', {'recover': False})['ready'])
        for _ in range(2):
            result = content(self.runtime.call('pua_observe', {'mode': 'screenshot'}))
            self.assertEqual(result['content'][1]['mimeType'], 'image/jpeg')
            image = Image.open(io.BytesIO(base64.b64decode(result['content'][1]['data'])))
            self.assertEqual(image.size, (589, 1280))
        self.assertEqual(sum(method == 'POST' and path == '/session' for method, path, _ in self.requests), 1)
        self.assertEqual(len({address for _, _, address in self.requests}), 1)

    def test_batch_is_fully_validated_before_first_action(self):
        with self.assertRaises(WDAError) as raised:
            self.runtime.call('pua_batch', {'steps': [{'op': 'tap', 'args': {'x': 1, 'y': 1}},
                                                     {'op': 'tap', 'args': {'x': 1}}]})
        self.assertEqual(raised.exception.code, 'invalid_argument')
        self.assertEqual(self.requests, [])

    def test_uncertain_tap_is_never_replayed(self):
        self.runtime.call('pua_ready', {'recover': False})
        self.drop_tap = True
        with self.assertRaises(WDAError) as raised:
            self.runtime.call('pua_tap', {'x': 50, 'y': 100})
        self.assertTrue(raised.exception.uncertain)
        self.assertEqual(sum('/wda/tap' in path for _, path, _ in self.requests), 1)


if __name__ == '__main__':
    unittest.main()
