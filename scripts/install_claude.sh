#!/bin/sh
# 为 Claude Code 安装 iPhone Use；不修改 Codex 的插件、MCP 注册与 dist/iphone-use。
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
task_plugin=iphone-use@iphone-use-local
task_stage=$(python3 "$task_root/scripts/package_claude.py")
# 重复添加同一本地 marketplace 返回成功；update 让 Claude Code 重新读取暂存目录。
claude plugin marketplace add "$task_stage" --scope user
claude plugin marketplace update iphone-use-local
# 插件按版本号缓存，同版本 update 不会刷新内容；已安装时先卸载再安装。
if claude plugin list --json | python3 -c 'import json,sys;sys.exit(0 if any(item.get("id")==sys.argv[1] and item.get("scope")=="user" for item in json.load(sys.stdin)) else 1)' "$task_plugin"; then
  claude plugin uninstall "$task_plugin" --scope user
fi
claude plugin install "$task_plugin" --scope user
cat <<'EOF'

iPhone Use is installed for Claude Code. Start a new Claude Code session, or run /reload-plugins in an open one.

Recommended permissions for ~/.claude/settings.json (not written by this script; see claude/README.md):
  "permissions": {
    "allow": ["mcp__plugin_iphone-use_iphone_use__*"],
    "deny": ["mcp__plugin_iphone-use_iphone_use__pua_screen_frame",
             "mcp__plugin_iphone-use_iphone_use__pua_screen_action"]
  }
EOF
