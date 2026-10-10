"""Windows MCP stdio adapter for iPhone Use, with one persistent WDA connection."""
import argparse
import asyncio
import base64
from collections import deque
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT.parent / 'server'
sys.path.insert(0, str(UPSTREAM))
from PIL import Image
from wda_client import WDAClient, WDAError
from wda_controller import PhoneController
from protocol import SCHEMAS, DESCRIPTIONS, READS, obj, published_schema, validate, validate_semantics

VERSION = '0.1.0'
PROTOCOLS = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
OPERATIONS = ('ready', 'observe', 'find', 'tap', 'swipe', 'type_text', 'launch_app',
              'press_button', 'wait', 'scroll_find', 'collect_list', 'batch', 'apps', 'setup', 'metrics')
SCHEMAS['setup'] = obj({'action': {'type': 'string', 'enum': ['status', 'start']}}, ('action',))
SCHEMAS['apps'] = obj({'query': {'type': 'string', 'maxLength': 100},
                       'limit': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ('query',))
SCHEMAS['ready'] = obj({'screenshot': {'type': 'boolean', 'default': False},
                        'recover': {'type': 'boolean', 'default': True}})
DESCRIPTIONS['ready'] = 'Initialize once per phone task. Verify status, unlock state and WDA session. recover=true can start the existing signed Windows runner; never replays phone actions.'
DESCRIPTIONS['setup'] = 'Read Windows runner status, or start the existing signed runner. No signing, login, installation or app removal.'
DESCRIPTIONS['apps'] = 'Find installed apps by name or bundle ID through USB. Only matching apps are returned. Use their real bundle IDs with launch_app.'
DESCRIPTIONS['metrics'] = 'In-process WDA HTTP request counts, timings and tool timings. No input text or screenshots. reset=true clears the measurements.'
INSTRUCTIONS = (
    'Windows iPhone Use reuses one WDA session and HTTP connection. Start each phone task with pua_ready, then use the healthy channel. '
    'Coordinates are iPhone points; screenshot pixels multiplied by image.pixel_to_point give points. '
    'Prefer observed selectors or coordinates from a fresh screenshot. Never guess a changed keyboard/send-button position. '
    'Batch only known steps and request observe=both or screenshot on the final action to avoid extra calls. '
    'Typing defaults to a draft with submit=false. Send messages only when the user authorized sending. '
    'Never replay uncertain taps, typing or submissions. Inspect actual state first. '
    'Ask the user to handle passwords, verification codes and device unlock. If the user also edits, stop to avoid mixing input. '
    'No phone actions run in the background. Setup is independent of CrossCode GUI.'
)
TOOLS = [{'name': 'pua_' + op, 'description': DESCRIPTIONS[op],
          'inputSchema': published_schema(op),
          'annotations': {'readOnlyHint': op in READS, 'destructiveHint': False,
                          'idempotentHint': op in READS, 'openWorldHint': True}}
         for op in OPERATIONS]


class WindowsPhone(PhoneController):
    def capture(self, viewport):
        raw = base64.b64decode(self.client.request('GET', '/screenshot')['value'], validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format != 'PNG':
                raise WDAError('invalid_response', 'WDA screenshot is not PNG')
            if (source.width > source.height) != (viewport['width'] > viewport['height']):
                viewport.update(self.viewport())
            preview = source.convert('RGB')
            preview.thumbnail((1280, 1280))
            buffer = io.BytesIO()
            preview.save(buffer, format='JPEG', quality=85)
            data = buffer.getvalue()
            width, height = preview.size
        # Screenshots stay in ignored runtime; retain at most ten previews.
        artifacts = self.state_dir / 'artifacts'
        artifacts.mkdir(parents=True, exist_ok=True)
        destination = artifacts / (str(time.time_ns()) + '-' + uuid.uuid4().hex[:6] + '.jpg')
        destination.write_bytes(data)
        for old in sorted(artifacts.glob('*.jpg'))[:-10]:
            old.unlink(missing_ok=True)
        return {'path': str(destination), 'mimeType': 'image/jpeg', 'width': width, 'height': height,
                'pixel_to_point': [viewport['width'] / width, viewport['height'] / height]}


class Runtime:
    def __init__(self, url):
        self.client = WDAClient(url, timeout=15)
        self.phone = WindowsPhone(self.client, ROOT / 'runtime/mcp')
        self.phone.call_budget = 40
        self.phone.settle_seconds = 0
        self.calls = deque(maxlen=500)
        self.ready_verified = False

    def status(self):
        try:
            value = self.client.request('GET', '/status', timeout=3).get('value', {})
            return {'ready': isinstance(value, dict) and value.get('ready') is True}
        except WDAError as error:
            return {'ready': False, 'error': error.as_dict()}

    def start(self):
        if self.status()['ready']:
            return {'started': False, 'ready': True, 'reused': True}
        # Stop only the exact loopback forwarder whose command line proves ownership.
        # A healthy service is never restarted by this path.
        script = ROOT / 'scripts/start_wda.ps1'
        cleanup = (
            "$ErrorActionPreference='Stop'; "
            "Get-NetTCPConnection -LocalPort 18100 -State Listen -ErrorAction SilentlyContinue | "
            "ForEach-Object { $owner=Get-CimInstance Win32_Process -Filter ('ProcessId='+$_.OwningProcess); "
            "if ($owner.CommandLine -match 'pymobiledevice3.*usbmux.*forward.*18100.*8100.*127\\.0\\.0\\.1') "
            "{ Stop-Process -Id $owner.ProcessId } else { throw 'Port belongs to another service' } }; exit 0"
        )
        log_path = ROOT / 'runtime/mcp-start.log'
        with log_path.open('w', encoding='utf-8') as log:
            for command in (["powershell", '-NoProfile', '-Command', cleanup],
                            ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)]):
                result = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=log, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
                if result.returncode:
                    raise WDAError('setup_failed', 'Windows runner startup failed; inspect runtime/mcp-start.log',
                                   details={'action_executed': False})
        self.client.close()
        self.client.session_id = None
        self.phone.reset()
        self.ready_verified = False
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if self.status()['ready']:
                return {'started': True, 'ready': True}
            time.sleep(0.5)
        raise WDAError('not_ready', 'Runner started but WDA did not become ready within 25 seconds')

    def ready(self, screenshot=False, recover=True):
        service = self.status()
        if not service['ready']:
            if not recover:
                return {'ready': False, 'service': service}
            self.start()
        if self.client.request('GET', '/wda/locked').get('value') is not False:
            self.ready_verified = False
            raise WDAError('phone_locked', 'Unlock the phone yourself, then call pua_ready again')
        self.client.ensure_session()
        observation = self.phone.observe(mode='both' if screenshot else 'tree', max_nodes=60)
        self.ready_verified = True
        return {'ready': True, 'transport': 'Windows USB WDA', 'persistent_session': True,
                'observation': observation}

    async def installed_apps(self, query, limit=10):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        from pymobiledevice3.usbmux import select_devices_by_connection_type
        devices = await select_devices_by_connection_type(connection_type='USB')
        if len(devices) != 1:
            raise WDAError('device_count', 'Connect exactly one USB phone')
        lockdown = await create_using_usbmux(serial=devices[0].serial, autopair=False)
        try:
            async with InstallationProxyService(lockdown=lockdown) as service:
                apps = await service.get_apps()
            matches = []
            for bundle, app in apps.items():
                name = app.get('CFBundleDisplayName') or app.get('CFBundleName') or bundle
                if query.casefold() in name.casefold() or query.casefold() in bundle.casefold():
                    matches.append({'bundle_id': bundle, 'name': name, 'installed_verified': True})
            return {'matches': matches[:limit], 'truncated': len(matches) > limit}
        finally:
            await lockdown.close()

    def call(self, name, arguments):
        if not isinstance(name, str) or not name.startswith('pua_') or name[4:] not in OPERATIONS:
            raise WDAError('unknown_tool', 'Unknown phone tool')
        operation = name[4:]
        validate(arguments, SCHEMAS[operation])
        validate_semantics(operation, arguments)
        started = time.monotonic()
        error_code = None
        try:
            if operation == 'metrics':
                metrics = {**self.client.metrics(), 'tools': list(self.calls),
                           'persistent_session': bool(self.client.session_id)}
                if arguments.get('reset'):
                    self.client.clear_metrics()
                    self.calls.clear()
                return metrics
            if operation == 'setup':
                return self.status() if arguments['action'] == 'status' else self.start()
            if operation == 'ready':
                return self.ready(**arguments)
            if operation == 'apps':
                return asyncio.run(self.installed_apps(**arguments))
            if not self.ready_verified:
                raise WDAError('not_initialized', 'Call pua_ready before phone tasks',
                               details={'action_executed': False})
            if operation in ('tap', 'swipe', 'type_text', 'launch_app', 'press_button', 'batch', 'scroll_find', 'collect_list'):
                if self.client.request('GET', '/wda/locked').get('value') is not False:
                    raise WDAError('phone_locked', 'Unlock the phone yourself, then read fresh state')
            return getattr(self.phone, operation)(**arguments)
        except WDAError as error:
            error_code = error.code
            if error.code in ('pua_unreachable', 'invalid session id', 'phone_locked'):
                self.ready_verified = False
            raise
        finally:
            # Record only operation timings, never input, selectors, app data or text.
            self.calls.append({'tool': name, 'seconds': round(time.monotonic() - started, 4), 'error': error_code})

    def close(self):
        self.client.close()


def content(data):
    blocks = [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))}]
    screenshot = data.get('image') or data.get('observation', {}).get('image')
    if not screenshot and isinstance(data.get('error'), dict):
        screenshot = data['error'].get('observation', {}).get('image')
    if not screenshot:
        for step in reversed(data.get('results', [])):
            screenshot = step.get('image') or step.get('observation', {}).get('image')
            if screenshot:
                break
    if screenshot:
        path = Path(screenshot['path']).resolve()
        if not path.is_relative_to((ROOT / 'runtime/mcp/artifacts').resolve()):
            raise ValueError('Screenshot path is outside project runtime')
        blocks.append({'type': 'image', 'mimeType': screenshot['mimeType'],
                       'data': base64.b64encode(path.read_bytes()).decode('ascii')})
    return {'content': blocks, 'isError': 'error' in data}


def tool_call(runtime, params):
    try:
        with redirect_stdout(sys.stderr):
            return content(runtime.call(params.get('name'), params.get('arguments', {})))
    except WDAError as error:
        return content({'error': error.as_dict()})
    except Exception as error:
        print('iPhone MCP failure: ' + type(error).__name__, file=sys.stderr, flush=True)
        return content({'error': {'code': 'internal_error', 'message': 'Local phone tool failed; inspect server stderr',
                                  'uncertain': True, 'replay_action': False}})


def serve(runtime):
    output = sys.stdout
    writing = threading.Lock()
    jobs = queue.Queue()

    def send(response):
        encoded = json.dumps(response, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
        with writing:
            output.write(encoded + '\n')
            output.flush()

    def worker():
        while True:
            job = jobs.get()
            if job is None:
                return
            ident, params = job
            send({'jsonrpc': '2.0', 'id': ident, 'result': tool_call(runtime, params)})

    thread = threading.Thread(target=worker, name='iphone-wda', daemon=True)
    thread.start()
    try:
        for line in sys.stdin:
            request = None
            try:
                if len(line) > 1024 * 1024:
                    raise ValueError('Request too large')
                request = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                    raise ValueError('Invalid request')
                if 'id' not in request:
                    continue
                ident = request['id']
                if isinstance(ident, bool) or not isinstance(ident, (str, int)):
                    raise ValueError('Invalid identifier')
                method = request['method']
                params = request.get('params', {})
                if not isinstance(params, dict):
                    raise ValueError('Invalid params')
                if method == 'initialize':
                    offered = params.get('protocolVersion')
                    result = {'protocolVersion': offered if offered in PROTOCOLS else PROTOCOLS[0],
                              'capabilities': {'tools': {'listChanged': False}},
                              'serverInfo': {'name': 'iphone-use-windows', 'version': VERSION},
                              'instructions': INSTRUCTIONS}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    result = {'tools': TOOLS}
                elif method == 'tools/call':
                    jobs.put((ident, params))
                    continue
                elif method in ('resources/list', 'resources/templates/list'):
                    result = {'resources' if method == 'resources/list' else 'resourceTemplates': []}
                else:
                    send({'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32601, 'message': 'Method not found'}})
                    continue
                send({'jsonrpc': '2.0', 'id': ident, 'result': result})
            except (ValueError, TypeError, KeyError):
                send({'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None,
                      'error': {'code': -32700 if request is None else -32600,
                                'message': 'Parse error' if request is None else 'Invalid request'}})
    finally:
        jobs.put(None)
        thread.join()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default=os.environ.get('WDA_URL', 'http://127.0.0.1:18100'))
    args = parser.parse_args()
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8', newline='\n')
    sys.stderr.reconfigure(encoding='utf-8')
    runtime = Runtime(args.url)
    try:
        serve(runtime)
    finally:
        runtime.close()


if __name__ == '__main__':
    main()
