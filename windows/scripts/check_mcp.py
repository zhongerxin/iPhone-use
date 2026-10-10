"""Verify the stdio MCP contract and optionally read-only real-phone operations."""
import argparse
import base64
import io
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Read phone status, UI and screenshots; never tap or type')
    args = parser.parse_args()
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    replies = queue.Queue()
    summary = {'protocol': False, 'tool_count': 0, 'invalid_actions_rejected': False, 'live': args.live}
    with (runtime / 'mcp-check.stderr.log').open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'server/iphone_mcp.py')],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   text=True, encoding='utf-8', cwd=ROOT,
                                   creationflags=subprocess.CREATE_NO_WINDOW)

        def read():
            try:
                for line in process.stdout:
                    replies.put(json.loads(line))
            except Exception as error:
                replies.put(error)
            finally:
                replies.put(None)

        threading.Thread(target=read, daemon=True).start()
        ident = 0

        def rpc(method, params=None):
            nonlocal ident
            ident += 1
            started = time.monotonic()
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method,
                                           'params': params or {}}, ensure_ascii=False) + '\n')
            process.stdin.flush()
            response = replies.get(timeout=100)
            assert isinstance(response, dict) and response.get('id') == ident, 'Invalid stdio response'
            assert 'error' not in response, 'JSON-RPC error'
            return response['result'], round(time.monotonic() - started, 3)

        def call(name, arguments):
            result, elapsed = rpc('tools/call', {'name': name, 'arguments': arguments})
            data = json.loads(result['content'][0]['text'])
            return result, data, elapsed

        try:
            initialized, _ = rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                               'clientInfo': {'name': 'windows-mcp-check', 'version': '1'}})
            assert initialized['protocolVersion'] == '2025-06-18'
            process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            process.stdin.flush()
            tools, _ = rpc('tools/list')
            names = [tool['name'] for tool in tools['tools']]
            assert len(names) == len(set(names)) == 15
            assert {'pua_ready', 'pua_batch', 'pua_observe', 'pua_type_text'} <= set(names)
            assert all(tool['inputSchema']['additionalProperties'] is False for tool in tools['tools'])
            rpc('ping')
            summary.update(protocol=True, tool_count=len(names))
            for name, arguments in [('pua_tap', {'x': 1, 'y': 1, 'unknown': True}),
                                     ('pua_tap', {'x': 1}),
                                     ('pua_launch_app', {'bundle_id': 'invalid'}),
                                     ('pua_batch', {'steps': [{'op': 'tap', 'args': {'x': 1, 'y': 1}},
                                                            {'op': 'tap', 'args': {'x': 1}}]})]:
                result, data, _ = call(name, arguments)
                assert result['isError'] and data['error']['code'] == 'invalid_argument'
            _, metrics, _ = call('pua_metrics', {})
            assert metrics['retained_requests'] == 0, 'Invalid actions reached the phone'
            summary['invalid_actions_rejected'] = True
            if args.live:
                result, ready, elapsed = call('pua_ready', {'recover': True})
                assert not result['isError'] and ready['ready'], ready.get('error', {}).get('code', 'Phone not ready')
                summary['ready_seconds'] = elapsed
                _, startup_metrics, _ = call('pua_metrics', {})
                summary['startup_http_errors'] = startup_metrics['errors']
                summary['screenshot_seconds'] = []
                from PIL import Image
                for _ in range(2):
                    result, data, elapsed = call('pua_observe', {'mode': 'screenshot'})
                    assert not result['isError'], data.get('error', {}).get('code', 'Observation failed')
                    images = [block for block in result['content'] if block['type'] == 'image']
                    assert len(images) == 1 and images[0]['mimeType'] == 'image/jpeg'
                    raw = base64.b64decode(images[0]['data'], validate=True)
                    with Image.open(io.BytesIO(raw)) as preview:
                        assert max(preview.size) <= 1280
                        assert list(preview.size) == [data['image']['width'], data['image']['height']]
                    assert all(value > 0 for value in data['image']['pixel_to_point'])
                    summary['screenshot_seconds'].append(elapsed)
                    summary['screenshot_bytes'] = len(raw)
                _, apps, _ = call('pua_apps', {'query': 'WebDriverAgent', 'limit': 10})
                assert 'matches' in apps, apps.get('error', {}).get('code', 'USB app lookup failed')
                _, metrics, _ = call('pua_metrics', {})
                creations = sum(value['count'] for key, value in metrics['endpoints'].items()
                                if key == '/session')
                assert creations == 1 and metrics['persistent_session'], 'Session was recreated'
                summary['session_creations'] = creations
                summary['http_errors'] = metrics['errors'] - startup_metrics['errors']
                assert summary['http_errors'] == 0, 'WDA requests failed after ready'
            print(json.dumps(summary, ensure_ascii=False))
            (runtime / 'mcp-check-result.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            assert process.returncode == 0, 'MCP server did not exit cleanly'


if __name__ == '__main__':
    main()
