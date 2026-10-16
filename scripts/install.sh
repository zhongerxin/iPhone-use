#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python3 "$task_root/scripts/package.py" --stage-only
codex plugin marketplace add "$task_root" --json
task_installation=$(codex plugin add iphone-use@iphone-use-local --json)
printf '%s\n' "$task_installation" | python3 "$task_root/scripts/register_mcp.py"
if [[ "$(uname)" == "Darwin" ]] && command -v swiftc >/dev/null 2>&1; then
    swiftc -O "$task_root/tools/ocr_vision.swift" -o "$task_root/server/ocr_tool" -framework Vision -framework AppKit
    echo 'Vision OCR tool compiled.'
fi
printf '%s\n' 'Reconnect the Codex chat to load iPhone Use tools and skills.
