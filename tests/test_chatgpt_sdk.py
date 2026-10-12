"""Real Midscene SDK + OAuth Responses adapter against local HTTP fixtures."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import unittest

from test_midscene_sdk import screenshot

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'server/midscene/run-ai.mjs'


@unittest.skipUnless((WORKER.parent / 'node_modules/jose').is_dir(), 'Install Midscene dependencies')
class ChatGPTSDKTests(unittest.TestCase):
    def test_real_assert_false_and_act_native_reports_through_responses(self):
        requests = []
        mode = 'assert-true'
        plans = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                nonlocal plans
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                requests.append((self.command, self.path, body))
                if self.path == '/v1/models':
                    value = {'models': [{'visibility': 'list', 'slug': 'gpt-fixture', 'display_name': 'Fixture'}]}
                elif self.path == '/v1/responses':
                    assert body['stream'] is True and body['store'] is False
                    assert body['input'][0]['role'] == 'developer'
                    assert 'temperature' not in body
                    if mode in ('act', 'stuck'):
                        plans += 1
                        assert 'Use compact execution memory.' in json.dumps(body['input'])
                        if mode == 'act' and plans > 1:
                            assert 'value-42' in json.dumps(body['input'])
                        content = ('<planning>Tap fixture</planning><action-type>Tap</action-type>'
                                   '<action-param-json>{"locate":{"prompt":"fixture", "bbox":[15,30,45,60]}}</action-param-json>') if mode == 'stuck' or plans <= 7 else '<planning>Done</planning><complete success="true">Done</complete>'
                        if mode == 'act': content = content.replace('[15,30,45,60]', json.dumps([15 + plans * 15, 30, 45 + plans * 15, 60]))
                        if mode == 'act' and plans == 1:
                            content = '<memory>{"observed":{"fixture":"value-42"}}</memory>' + content
                    elif mode.startswith('input-'):
                        plans += 1
                        input_mode = mode.removeprefix('input-')
                        param = {'locate': {'prompt': 'title', 'bbox': [15, 30, 45, 60]},
                                 'mode': input_mode, 'value': '' if input_mode == 'clear' else 'replacement'}
                        content = ('<action-type>Input</action-type><action-param-json>' + json.dumps(param) + '</action-param-json>') if plans == 1 else '<complete success="true">Done</complete>'
                    elif mode.startswith('wait-'):
                        plans += 1
                        content = '<data-json>' + json.dumps({'StatementIsTruthy': mode == 'wait-true' and plans >= 2}) + '</data-json>'
                    elif mode == 'swipe':
                        plans += 1
                        if plans <= 2:
                            param = {'start': {'prompt': 'wheel', 'bbox': [30, 60, 60, 90]}, 'duration': 1500}
                            param.update({'direction': 'down', 'distance': 90} if plans == 1 else
                                         {'end': {'prompt': 'one row below', 'bbox': [30, 150, 60, 180]}})
                            content = '<action-type>Swipe</action-type><action-param-json>' + json.dumps(param) + '</action-param-json>'
                        else:
                            content = '<complete success="true">Done</complete>'
                    else:
                        content = '<observation>Fixture evidence</observation><data-json>' + json.dumps({'StatementIsTruthy': mode == 'assert-true'}) + '</data-json>'
                    event = {'type': 'response.completed', 'response': {'id': 'fixture', 'status': 'completed',
                        'model': 'gpt-fixture', 'output': [{'type': 'message', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': content}]}]}}
                    raw = ('data: ' + json.dumps(event) + '\n\n').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers(); self.wfile.write(raw); return
                else:
                    values = {'/wda/locked': False, '/status': {'ready': True},
                              '/session/borrowed/wda/screen': {'scale': 3},
                              '/session/borrowed/window/rect': {'x': 0, 'y': 0, 'width': 100, 'height': 200},
                              '/session/borrowed/screenshot': screenshot(),
                              '/session/borrowed/element/active': {'ELEMENT': 'title'}}
                    value = {'sessionId': 'borrowed', 'value': values.get(self.path, {})}
                raw = json.dumps(value).encode()
                self.send_response(200); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        port = server.server_address[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            auth = root / 'chatgpt'; auth.mkdir(mode=0o700)
            record = auth / 'account.json'
            record.write_text(json.dumps({'access_token': 'fixture-only', 'refresh_token': 'fixture-only',
                'client_id': 'oaiapp_fixture', 'scopes': ['chatgpt.tokens.use.direct', 'resource.invoke'],
                'expires_at': (time.time() + 3600) * 1000})); record.chmod(0o600)
            # Test-only preload; production has no configurable auth/API endpoints.
            preload = root / 'fixture.mjs'
            preload.write_text('const original=globalThis.fetch;globalThis.fetch=(url,opts)=>{'
                'const s=String(url);if(s.startsWith("https://api.openai.com/v1/"))'
                f'url=s.replace("https://api.openai.com", "http://127.0.0.1:{port}");'
                'return original(url,opts);};')
            outcomes = []
            for mode in ('assert-true', 'assert-false', 'act', 'stuck', 'swipe', 'input-replace', 'input-clear', 'input-typeOnly', 'wait-true', 'wait-false'):
                plans = 0
                request_start = len(requests)
                p = subprocess.run(['node', '--import', str(preload), str(WORKER)], cwd=root,
                    input=json.dumps({**({'planning': 'compact'} if mode == 'stuck' else {}), 'action': 'wait' if mode.startswith('wait-') else 'act' if mode in ('act', 'stuck', 'swipe') or mode.startswith('input-') else 'assert', 'args': {'text': 'Fixture task', **({'timeout_ms': 4000 if mode == 'wait-true' else 1500} if mode.startswith('wait-') else {})},
                        'host': '127.0.0.1', 'port': port, 'sessionId': 'borrowed', 'reportId': 'oauth-fixture'}),
                    capture_output=True, text=True, timeout=30,
                    env={k: v for k, v in os.environ.items() if not k.startswith(('MIDSCENE_', 'OPENAI_'))})
                result = json.loads(p.stdout)
                self.assertEqual(p.returncode, 1 if mode in ('assert-false', 'stuck', 'wait-false') else 0, str(result) + p.stderr)
                self.assertEqual(result['model'], 'gpt-fixture')
                outcomes.append(result)
                if mode.startswith('wait-'):
                    mutation_paths = ('/wda/tap', '/actions', '/wda/keys', '/clear', '/wda/apps/launch')
                    self.assertFalse(any(r[1].endswith(mutation_paths) for r in requests[request_start:]))
            self.assertEqual(outcomes[2]['completion'], {'source': 'midscene_aiAct', 'summary': 'Done', 'independent_assertion': False})
            self.assertIn('screenshot', outcomes[2])
            self.assertNotIn('completion', outcomes[1])
            self.assertNotIn('completion', outcomes[3])
            html = Path(outcomes[-1]['report']).read_text()
            self.assertNotIn('fixture-only', html)
            dumps = re.findall(r'<script type="midscene_web_dump" data-group-id=[^>]*>(.*?)</script>', html, re.S)
            executions = {e['id']: e for dump in dumps for e in json.loads(dump)['executions']}
            tasks = [t for e in executions.values() for t in e['tasks']]
            self.assertEqual(len(executions), 10)
            self.assertEqual([t['output'] for t in tasks if t['subType'] == 'Assert'], [True, False])
            self.assertTrue(any(t['type'] == 'Action Space' and t['subType'] == 'Tap' for t in tasks))
            self.assertEqual(outcomes[1]['error'], 'assertion_failed')
            self.assertEqual(len([r for r in requests if r[1] == '/session/borrowed/wda/tap']), 12)
            self.assertEqual(outcomes[3]['error'], 'midscene_no_progress')
            self.assertGreaterEqual(len([r for r in requests if r[1] == '/v1/responses']), 13)

            gestures = [r[2]['actions'][0]['actions'] for r in requests if r[1] == '/session/borrowed/actions']
            self.assertEqual(len(gestures), 2)
            # Same 90 screenshot-pixel movement at DPR 3 must reach the same WDA points.
            endpoints = [[(a['x'], a['y']) for a in gesture if a['type'] == 'pointerMove'] for gesture in gestures]
            self.assertEqual(endpoints, [[(15, 25), (15, 55)], [(15, 25), (15, 55)]])

            clears = [r for r in requests if r[1] == '/session/borrowed/element/title/clear']
            keys = [r for r in requests if r[1] == '/session/borrowed/wda/keys']
            self.assertEqual(len(clears), 2)  # replace and clear, never typeOnly
            self.assertEqual(len(keys), 2)  # replace and typeOnly, never clear
            self.assertTrue(outcomes[8]['ok'])
            self.assertEqual(outcomes[9]['error'], 'wait_timeout')
            self.assertTrue(any(t['subType'] == 'WaitFor' for t in tasks))
