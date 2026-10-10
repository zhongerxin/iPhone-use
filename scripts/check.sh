#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$task_root"
# Tests and package probes must never send production usage events.
export IPHONE_USE_ANALYTICS=0
python3 -m unittest discover -s tests -v
python3 scripts/package.py --validate-only
node --check tooling/forward.mjs
node --check tooling/screen-stream.mjs
node --check server/midscene/run.mjs
node --check server/midscene/action-report.mjs
node --check server/midscene/run-ai.mjs
node --check server/midscene/chatgpt-cli.mjs
node --test tests/chatgpt.test.mjs
python3 scripts/check_screen_ui.py
