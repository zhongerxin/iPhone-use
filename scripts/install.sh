#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_language=default
if [ "$#" -gt 0 ]; then
    if [ "$#" -ne 2 ] || [ "$1" != "--language" ]; then
        printf '%s\n' 'Usage: sh scripts/install.sh [--language default|ja]' >&2
        exit 2
    fi
    case "$2" in
        default|ja) task_language=$2 ;;
        *) printf '%s\n' 'Usage: sh scripts/install.sh [--language default|ja]' >&2; exit 2 ;;
    esac
fi
python3 "$task_root/scripts/package.py" --stage-only --language "$task_language"
codex plugin marketplace add "$task_root" --json
task_installation=$(codex plugin add iphone-use@iphone-use-local --json)
printf '%s\n' "$task_installation" | python3 "$task_root/scripts/register_mcp.py"
printf '%s\n' 'Reconnect the Codex chat to load iPhone Use tools and skills.'
