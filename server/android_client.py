"""ADB transport and single-attempt UI Automator RPC; no mutation replay."""
import base64
import binascii
import collections
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import time
from phone_protocol import PhoneError as AndroidError


def adb_path():
    candidates = [os.environ.get('ANDROID_USE_ADB'), shutil.which('adb'),
                  str(Path(os.environ.get('ANDROID_USE_HOME',str(Path.home()/'.local/share/android-use')))/'platform-tools/adb'),
                  str(Path.home()/'Library/Android/sdk/platform-tools/adb')]
    return next((p for p in candidates if p and Path(p).is_file()), None)


class AndroidClient:
    def __init__(self, serial=None, adb=None):
        self.serial, self.adb, self.port = serial, adb or adb_path(), None
        self.records = collections.deque(maxlen=1000)
        self.mutations = 0

    def command(self, args, mutation=False, timeout=15, binary=False, device=True):
        if not self.adb:
            raise AndroidError('missing_adb', 'Install Android Platform Tools or set ANDROID_USE_ADB.')
        if device and not self.serial:
            raise AndroidError('device_not_selected', 'Use setup configure with an explicit serial.')
        args = list(args)
        if args and args[0] == 'shell':
            args = ['shell', shlex.join(args[1:])]
        command = [self.adb] + (['-s', self.serial] if device else []) + args
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AndroidError('adb_unreachable', 'ADB request failed. Inspect current state before any retry.', uncertain=mutation) from exc
        if result.returncode:
            # Never echo the command: it may carry user input or pairing codes.
            raise AndroidError('adb_failed', result.stderr.decode(errors='replace')[:500], uncertain=mutation)
        if mutation: self.mutations += 1
        return result.stdout if binary else result.stdout.decode(errors='replace').strip()

    def devices(self):
        output = self.command(['devices', '-l'], device=False)
        result = []
        for line in output.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 2: continue
            entry = {'serial': parts[0], 'state': parts[1]}
            for part in parts[2:]:
                if ':' in part:
                    key, value = part.split(':', 1); entry[key] = value
            result.append(entry)
        return result

    def attach(self):
        devices = self.devices()
        selected = next((d for d in devices if d['serial'] == self.serial), None)
        if not selected:
            raise AndroidError('device_disconnected', 'Selected device is absent; reconnect it, then setup start.')
        if selected['state'] != 'device':
            raise AndroidError('device_' + selected['state'], 'Unlock the phone and accept USB debugging authorization.')
        if self.port is None:
            self.port = int(self.command(['forward', 'tcp:0', 'tcp:9008']))

    def rpc(self, method, params=None, mutation=False, timeout=12):
        if self.port is None: self.attach()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=timeout)
        started = time.monotonic()
        try:
            # The Android 9 embedded server decodes raw UTF-8 inconsistently.
            # JSON Unicode escapes preserve all characters, including surrogate pairs.
            payload = json.dumps({'jsonrpc':'2.0', 'id':1, 'method':method, 'params':params or []})
            connection.request('POST', '/jsonrpc/0', payload.encode(), {'Content-Type':'application/json'})
            response = connection.getresponse()
            raw = response.read(8 * 1024 * 1024 + 1)
            if response.status != 200 or len(raw) > 8 * 1024 * 1024:
                raise ValueError('Invalid response')
            result = json.loads(raw)
            if 'error' in result:
                raise AndroidError('automation_error', 'UI Automator rejected the request; observe before continuing.', uncertain=mutation)
            if 'result' not in result: raise ValueError('Missing result')
            if mutation:
                self.mutations += 1
                if result['result'] is False:
                    raise AndroidError('action_rejected', 'UI Automator did not confirm the action; observe before continuing.', uncertain=True)
            return result['result']
        except AndroidError: raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise AndroidError('automation_unreachable', 'Automation response unavailable. Use setup start to restore the channel; never replay an uncertain action.', uncertain=mutation) from exc
        finally:
            connection.close()
            self.records.append({'seconds':round(time.monotonic()-started,4), 'mutation':mutation})

    def locked(self):
        text = self.command(['shell','dumpsys','window','policy'])
        # Samsung Android 9 leaves showingAndNotOccluded=true after unlock.
        # Prefer the actual keyguard monitor state over that stale derived flag.
        values = re.findall(r'(?:isStatusBarKeyguard|mIsShowing)=(true|false)', text)
        if not values:
            values = re.findall(r'^\s+showing=(true|false)', text, re.M)
        if not values:
            raise AndroidError('lock_state_unknown', 'Cannot verify Android lock state on this device; no action executed.')
        return 'true' in values

    def screenshot(self):
        data = self.command(['exec-out','screencap','-p'], binary=True)
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise AndroidError('screenshot_failed', 'Device returned no PNG screenshot.')
        return data

    def preview_screenshot(self):
        """Single read-only device JPEG capture; no host PNG recompression."""
        data = self.rpc('takeScreenshot', [1, 75], timeout=3)
        try:
            raw = base64.b64decode(''.join(data.split()) if isinstance(data,str) else data, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise AndroidError('screenshot_failed', 'Device returned invalid preview data.') from exc
        if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
            raise AndroidError('screenshot_failed', 'Device returned no JPEG preview.')
        return raw

    def close(self):
        if self.port:
            try: self.command(['forward','--remove',f'tcp:{self.port}'])
            except AndroidError: pass
            self.port = None
