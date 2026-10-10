#!/usr/bin/env python3
"""Register iPhone Use and retire only its previous, owned registration."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def retire_previous_registration():
    previous = subprocess.run(["codex", "mcp", "get", "iphone_wda", "--json"],
                              capture_output=True, text=True, check=False)
    if previous.returncode == 0:
        try:
            transport = json.loads(previous.stdout).get("transport", {})
            args = transport.get("args", [])
            parts = Path(args[0]).parts if len(args) == 1 and isinstance(args[0], str) else ()
            owned = any(parts[i:i+3] == ("cache", "iphone-wda-local", "iphone-use-wda")
                        for i in range(len(parts)-2))
            if transport.get("type") == "stdio" and owned and parts[-2:] == ("server", "iphone_wda.py"):
                subprocess.run(["codex", "mcp", "remove", "iphone_wda"], check=True)
        except (ValueError, TypeError):
            pass
    # Keep the old cache because a running setup worker may still use it.
    config = Path(os.environ.get("CODEX_HOME", str(Path.home()/".codex"))) / "config.toml"
    if not config.is_file():
        return
    original = config.read_text()
    section = r'(?ms)(^\[plugins\."iphone-use-wda@iphone-wda-local"\]\n)(.*?)(?=^\[|\Z)'
    updated = re.sub(section, lambda m: m[1] + re.sub(r'(?m)^(enabled\s*=\s*)true(\s*(?:#.*)?)$', r'\1false\2', m[2]), original)
    if updated == original:
        return
    fd, temporary = tempfile.mkstemp(prefix=".iphone-use-config-", dir=config.parent)
    try:
        os.fchmod(fd, config.stat().st_mode & 0o777)
        with os.fdopen(fd, "w") as stream:
            stream.write(updated)
        os.replace(temporary, config)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


def main():
    installation=json.load(sys.stdin)
    if installation.get("pluginId")!="iphone-use@iphone-use-local":
        raise SystemExit("Expected the iPhone Use plugin installation result.")
    root=Path(installation["installedPath"]).resolve(strict=True)
    server=root/"server"/"iphone_use.py"
    if not server.is_file():
        raise SystemExit("Installed iPhone Use MCP entrypoint is missing.")
    print(json.dumps(installation,ensure_ascii=False),flush=True)
    # Same name as the plugin registration: Config wins over Plugin in Codex,
    # so there is one namespace, without the shared agent-plugin tool budget.
    command=["codex","mcp","add","iphone_use"]
    config=root/"mcp.json"
    language=json.loads(config.read_text()).get("mcpServers",{}).get("iphone_use",{}).get("env",{}).get("IPHONE_USE_LANGUAGE") if config.is_file() else None
    if language in ("default","ja"):
        command.extend(["--env","IPHONE_USE_LANGUAGE="+language])
    subprocess.run([*command,"--","python3",str(server)],check=True)
    retire_previous_registration()


if __name__=="__main__":main()
