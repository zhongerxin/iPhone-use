"""Non-device ChatGPT authorization entry points; never return credentials."""
import json
from pathlib import Path
import select
import shutil
import subprocess
import webbrowser

from wda_client import WDAError

CLI = Path(__file__).parent / 'midscene/chatgpt-cli.mjs'
ACTIONS = {'auth_status': 'status', 'auth_login': 'login', 'auth_logout': 'logout', 'auth_cancel': 'cancel', 'models': 'models'}
_logins = []


def run(state_dir, action):
    node = shutil.which('node')
    if not node or not (CLI.parent / 'node_modules/jose/package.json').is_file():
        raise WDAError('midscene_not_installed', 'Install Node.js 22.19+ and run npm ci --prefix <plugin-root>/server/midscene.')
    command = [node, str(CLI), ACTIONS[action], str(Path(state_dir) / 'chatgpt')]
    try:
        if action == 'auth_login':
            # Reuse a still-pending authorization, without opening more flows.
            status = run(state_dir, 'auth_status')
            if (status.get('login') or {}).get('status') == 'pending':
                return {'ok': True, **status['login']}
            _logins[:] = [child for child in _logins if child.poll() is None]
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)
            _logins.append(child)
            if not select.select([child.stdout], [], [], 15)[0]:
                child.kill(); child.wait()
                raise ValueError('Login did not start')
            try:
                result = json.loads(child.stdout.readline())
            finally:
                child.stdout.close()
            if result.get('ok') and result.get('url'):
                # Only a local launch URL leaves the authorization worker.
                result['browser_opened'] = webbrowser.open(result['url'])
        else:
            child = subprocess.run(command, capture_output=True, text=True, timeout=50)
            result = json.loads(child.stdout)
        if result.get('ok') is not True:
            raise WDAError('chatgpt_auth_failed', 'ChatGPT authorization is not ready.',
                           details={'reason': result.get('error', 'chatgpt_auth_failed')})
        return result
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise WDAError('chatgpt_auth_failed', 'Could not complete the ChatGPT authorization operation; check auth_status.') from None
