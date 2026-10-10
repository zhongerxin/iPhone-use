"""Private simulator benchmark transport; one JSON request/response per line."""
import json
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
entry = [str(root / 'server/iphone_use.py')]
if len(sys.argv) > 2:
    # Isolated worker copy for the effort ablation; production files stay unchanged.
    entry = ['-c', 'import sys; from pathlib import Path; '
        f'sys.path.insert(0, {str(root / "server")!r}); '
        'import wda_midscene; '
        f'wda_midscene.WORKER=Path({str(Path(sys.argv[2]) / "run.mjs")!r}); '
        'import iphone_use; sys.exit(iphone_use.main())']
p = subprocess.Popen([sys.executable, *entry,
    '--state-dir', sys.argv[1], '--url', 'http://127.0.0.1:18101'],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
counter = 0

def rpc(method, params):
    global counter
    counter += 1
    p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params}) + '\n')
    p.stdin.flush()
    response = json.loads(p.stdout.readline())
    if 'error' in response:
        raise RuntimeError(response['error'])
    return response['result']

try:
    rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
        'clientInfo': {'name': 'instrumented-comparison', 'version': '1'}})
    p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    p.stdin.flush()
    for line in sys.stdin:
        req = json.loads(line)
        response = rpc('tools/call', req)
        data = json.loads(response['content'][0]['text'])
        data['mcp_is_error'] = response.get('isError', False)
        print(json.dumps(data), flush=True)
finally:
    p.stdin.close()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.terminate()
        p.wait(timeout=5)
