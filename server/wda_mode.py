"""Persistent execution choice, independent of ChatGPT authorization."""
import fcntl
import json
import os
from pathlib import Path
import tempfile

from wda_client import WDAError

MODES = ('off', 'steps', 'ai')


def read(state_dir):
    path = Path(state_dir) / 'execution-mode.json'
    try:
        value = json.loads(path.read_text())
        if value.get('mode') not in MODES:
            raise ValueError()
        return value['mode']
    except FileNotFoundError:
        return 'steps'
    except (ValueError, AttributeError, OSError):
        raise WDAError('invalid_execution_mode', 'Execution preference is unreadable; explicitly set a mode before continuing.') from None


def settings(state_dir, mode=None):
    root = Path(state_dir)
    if mode is not None:
        if mode not in MODES:
            raise WDAError('invalid_arguments', 'mode must be off, steps or ai.')
        # Do not change routing underneath an active device task.
        with (root / 'operation.lock').open('a') as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise WDAError('device_busy', 'Wait for the active operation before changing mode.') from None
            fd, name = tempfile.mkstemp(prefix='execution-mode-', suffix='.tmp', dir=root)
            try:
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'mode': mode}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(name, root / 'execution-mode.json')
            finally:
                if os.path.exists(name):
                    os.unlink(name)
    mode = read(root)
    return {'ok': True, 'mode': mode, 'enabled': mode != 'off',
            'ai_enabled': mode == 'ai', 'authorization_changed': False}


def require(state_dir, action):
    mode = read(state_dir)
    if mode == 'off':
        raise WDAError('midscene_disabled', 'Midscene is off. Use PUA controls; change mode only at the user\'s request.')
    if action in ('act', 'assert', 'wait') and mode != 'ai':
        raise WDAError('midscene_ai_disabled', 'Single-step mode does not invoke AI. Select ai explicitly before act/assert/wait.')
