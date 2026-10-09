#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_android_home=${ANDROID_USE_HOME:-"$HOME/.local/share/android-use"}
mkdir -p "$task_android_home"
if [ ! -x "$task_android_home/venv/bin/python" ]; then python3 -m venv "$task_android_home/venv"; fi
"$task_android_home/venv/bin/python" -m pip install -r "$task_root/android/requirements.txt"
if ! command -v adb >/dev/null 2>&1 && [ ! -x "$task_android_home/platform-tools/adb" ] && [ -z "${ANDROID_USE_ADB:-}" ]; then
  if [ "$(uname -s)" != Darwin ]; then printf '%s\n' 'Install official Android Platform Tools and set ANDROID_USE_ADB.' >&2; exit 1; fi
  curl -fL --connect-timeout 20 --max-time 180 https://dl.google.com/android/repository/platform-tools-latest-darwin.zip -o "$task_android_home/platform-tools.zip"
  unzip -q -o "$task_android_home/platform-tools.zip" -d "$task_android_home"
fi
python3 "$task_root/scripts/package_android.py"
codex plugin marketplace add "$task_root/dist/android-marketplace" --json
codex plugin add android-use@android-use-local --json > "$task_android_home/installation.json"
python3 - "$task_android_home/installation.json" <<'PY'
import json,subprocess,sys
from pathlib import Path
result=json.loads(Path(sys.argv[1]).read_text())
assert result['pluginId']=='android-use@android-use-local'
root=Path(result['installedPath']).resolve(strict=True)
assert (root/'scripts/android_mcp.sh').is_file()
# The plugin already registers android_use. Remove only our old duplicate
# global registration; two registrations can create separate runtime lifetimes.
previous=subprocess.run(['codex','mcp','get','android_use','--json'],capture_output=True,text=True)
if previous.returncode==0:
    transport=json.loads(previous.stdout).get('transport',{})
    args=transport.get('args',[])
    if not (transport.get('env') or {}).get('PLUGIN_ROOT') and transport.get('command')=='sh' and len(args)==1 and '/android-use-local/android-use/' in args[0] and args[0].endswith('/scripts/android_mcp.sh'):
        subprocess.run(['codex','mcp','remove','android_use'],check=True)
print('Android Use installed. Reconnect this chat to load its MCP tools, skills and screen widget.')
PY
