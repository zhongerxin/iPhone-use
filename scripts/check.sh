#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
# Tests and package probes must never send production usage events.
export IPHONE_USE_ANALYTICS=0
python3 -m unittest discover -s tests -v
python3 scripts/package.py --validate-only
node --test tests/test_transport.mjs
node --check tooling/device-transport.mjs
node --check tooling/forward.mjs
node --check tooling/screen-stream.mjs
python3 scripts/check_screen_ui.py
