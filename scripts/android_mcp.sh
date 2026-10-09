#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_android_home=${ANDROID_USE_HOME:-"$HOME/.local/share/android-use"}
if [ ! -x "$task_android_home/venv/bin/python" ]; then
  printf '%s\n' 'Android Use dependencies missing; run scripts/install_android.sh from the source checkout.' >&2
  exit 1
fi
export ANDROID_USE_STATE_DIR=${ANDROID_USE_STATE_DIR:-"$task_android_home/state"}
exec "$task_android_home/venv/bin/python" "$task_root/server/android_use.py" "$@"
