#!/usr/bin/env python3
"""Install iPhone Use into WorkBuddy as a standard MCP server plus two skills.

WorkBuddy has no plugin CLI, so this follows its own convention for local servers:
code under ~/.workbuddy/mcp-servers/<name>, one entry in ~/.workbuddy/mcp.json and
skills under ~/.workbuddy/skills. Phone state stays in ~/.local/share/iphone-use,
shared with a Codex install.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import host_text

NAME = "iphone-use"
SERVER = "iphone_use"
SKILLS = ("iphone-use", "iphone-use-setup")
MARKER = ".workbuddy-install.json"
RUNTIME_FILES = ("mcp_server.py", "host_text.py", "preview.py")
SCREEN_PAGE = "assets/phone-screen.html"
# Codex manifests mean nothing to WorkBuddy and would only suggest a second registration.
CODEX_ONLY = ("plugin.json", "mcp.json", ".mcp.json", ".codex-plugin", ".agents")
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
LOOPBACK = "127.0.0.1,localhost,::1"


class InstallError(Exception):
    pass


def packaging():
    spec = importlib.util.spec_from_file_location("iphone_use_packaging", ROOT / "scripts/package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stage(target):
    """Copy the upstream install package into target, with WorkBuddy wording and entrypoint."""
    package = packaging()
    version = package.validate()["version"]
    for source, relative in package.package_files():
        if relative.parts[0] in CODEX_ONLY:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.parts[0] == "skills" and relative.suffix == ".md":
            destination.write_text(host_text.skill_text(relative.as_posix(), source.read_text()))
        elif relative.as_posix() == SCREEN_PAGE:
            destination.write_text(host_text.screen_html(source.read_text()))
        else:
            shutil.copy2(source, destination)
    (target / "workbuddy").mkdir()
    for name in RUNTIME_FILES:
        shutil.copy2(ROOT / "workbuddy" / name, target / "workbuddy" / name)
    (target / MARKER).write_text(json.dumps({"name": NAME, "version": version, "source": str(ROOT)}, indent=2) + "\n")
    return version


def server_path():
    """PATH for the server: a GUI app starts children without Homebrew or nvm directories."""
    found = [shutil.which(name) for name in ("node", "npm", "git", "xcodebuild", "xcrun")]
    directories = [str(Path(path).parent) for path in found if path]
    return os.pathsep.join(dict.fromkeys(directories + list(SYSTEM_PATH)))


def server_entry(install_dir, python, analytics):
    env = {"PATH": server_path(), "NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK}
    if not analytics:
        # Upstream labels every event as its Codex plugin; a port should not add to that count unasked.
        env["IPHONE_USE_ANALYTICS"] = "0"
    return {"command": python, "args": [str(install_dir / "workbuddy" / "mcp_server.py")], "env": env}


def handshake(entry):
    """Start the server exactly as WorkBuddy will and list its tools, without touching phone state."""
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "workbuddy-install", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    with tempfile.TemporaryDirectory(prefix="iphone-use-check-") as state:
        env = {**entry["env"], "HOME": str(Path.home()), "IPHONE_USE_STATE_DIR": state, "IPHONE_USE_ANALYTICS": "0"}
        try:
            done = subprocess.run([entry["command"], *entry["args"]], input="".join(json.dumps(r) + "\n" for r in requests),
                                  capture_output=True, text=True, env=env, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError(f"The MCP server did not start: {error}") from error
    try:
        replies = {reply["id"]: reply["result"] for reply in map(json.loads, done.stdout.splitlines())}
        return replies[1]["serverInfo"]["version"], len(replies[2]["tools"])
    except (ValueError, KeyError, TypeError) as error:
        raise InstallError("The MCP server did not answer initialize and tools/list:\n" + done.stderr[-2000:]) from error


def is_skill(directory, name):
    try:
        return re.search(rf"^name: {re.escape(name)}$", (directory / "SKILL.md").read_text(), re.M) is not None
    except OSError:
        return False


def owned(path, home, force):
    """Whether an existing path may be replaced or removed: only what this installer put there."""
    if not path.exists() or force:
        return True
    if path.parent == home / "skills":
        return is_skill(path, path.name)
    return (path / MARKER).is_file()


def replace_directory(source, target):
    """Swap in a complete copy, so a running server never sees a half-written directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fresh = target.with_name(f".{target.name}.new-{os.getpid()}")
    retired = target.with_name(f".{target.name}.old-{os.getpid()}")
    try:
        shutil.copytree(source, fresh)
        if target.exists():
            target.rename(retired)
        fresh.rename(target)
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
        shutil.rmtree(retired, ignore_errors=True)


def read_config(path):
    if not path.exists():
        return {}
    try:
        config = json.loads(path.read_text())
    except ValueError as error:
        raise InstallError(f"{path} is not valid JSON; fix it first, nothing was changed.") from error
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise InstallError(f"{path} has an unexpected shape; nothing was changed.")
    return config


def ours(entry, install_dir):
    args = entry.get("args") if isinstance(entry, dict) else None
    return isinstance(args, list) and len(args) == 1 and isinstance(args[0], str) and Path(args[0]).parent.parent == install_dir


def write_config(path, config):
    """Replace the file atomically, keeping one copy of what was there before the change."""
    text = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
    if path.exists():
        if path.read_text() == text:
            return False
        shutil.copy2(path, path.with_name(path.name + time.strftime(".bak-%Y%m%d-%H%M%S")))
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-", dir=path.parent)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777 if path.exists() else 0o644)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def install(home, python, analytics=False, force=False):
    if not home.is_dir():
        raise InstallError(f"{home} does not exist. Start WorkBuddy once, or pass --home.")
    install_dir = home / "mcp-servers" / NAME
    skill_dirs = [home / "skills" / name for name in SKILLS]
    config_path = home / "mcp.json"
    config = read_config(config_path)
    servers = config.setdefault("mcpServers", {})
    if SERVER in servers and not ours(servers[SERVER], install_dir) and not force:
        raise InstallError(f"{config_path} already has a different '{SERVER}' server. Remove it or pass --force.")
    for path in (install_dir, *skill_dirs):
        if not owned(path, home, force):
            raise InstallError(f"{path} exists and was not installed by this script. Move it or pass --force.")

    with tempfile.TemporaryDirectory(prefix="iphone-use-stage-") as staging:
        staged = Path(staging) / NAME
        staged.mkdir()
        version = stage(staged)
        entry = server_entry(install_dir, python, analytics)
        # Prove the package starts under WorkBuddy's reduced environment before anything is replaced.
        served, tools = handshake({**entry, "args": [str(staged / "workbuddy" / "mcp_server.py")]})
        if served != version:
            raise InstallError(f"Staged server reports {served}, expected {version}.")
        replace_directory(staged, install_dir)
    for directory in skill_dirs:
        replace_directory(install_dir / "skills" / directory.name, directory)
    servers[SERVER] = entry
    changed = write_config(config_path, config)
    return {"version": version, "tools": tools, "install_dir": str(install_dir), "skills": [str(d) for d in skill_dirs],
            "config": str(config_path), "config_changed": changed, "entry": entry}


def uninstall(home, force=False):
    """Remove what install() added. The phone's configuration and builds are left alone."""
    install_dir = home / "mcp-servers" / NAME
    removed = []
    config_path = home / "mcp.json"
    config = read_config(config_path)
    servers = config.get("mcpServers", {})
    if SERVER in servers and (ours(servers[SERVER], install_dir) or force):
        del servers[SERVER]
        write_config(config_path, config)
        removed.append(f"{config_path}: {SERVER}")
    for path in (install_dir, *(home / "skills" / name for name in SKILLS)):
        if path.exists() and owned(path, home, force):
            shutil.rmtree(path)
            removed.append(str(path))
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home() / ".workbuddy", help="WorkBuddy data directory")
    parser.add_argument("--python", default=sys.executable, help="interpreter WorkBuddy starts the server with (3.9+)")
    parser.add_argument("--analytics", action="store_true", help="keep upstream's anonymous usage statistics enabled")
    parser.add_argument("--force", action="store_true", help="replace an existing server entry or directory this script did not create")
    parser.add_argument("--uninstall", action="store_true", help="remove the server entry, the installed code and the skills")
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    try:
        if args.uninstall:
            removed = uninstall(home, args.force)
            print("\n".join(["Removed:"] + removed) if removed else "Nothing to remove.")
            return 0
        if sys.platform != "darwin":
            raise InstallError("iPhone Use needs macOS with Xcode.")
        python = shutil.which(args.python) or args.python
        if not Path(python).is_absolute() or not os.access(python, os.X_OK):
            raise InstallError(f"--python must resolve to an executable: {args.python}")
        result = install(home, python, args.analytics, args.force)
    except (InstallError, host_text.Drift) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"iPhone Use {result['version']} installed for WorkBuddy ({result['tools']} tools).")
    print(f"  server  {result['install_dir']}")
    print(f"  config  {result['config']} -> mcpServers.{SERVER}" + ("" if result["config_changed"] else " (unchanged)"))
    for directory in result["skills"]:
        print(f"  skill   {directory}")
    if not all(shutil.which(name) for name in ("node", "npm")):
        print("warning: node or npm was not found on PATH; install Node.js 20.19+ and run this script again.")
    print("Next: in WorkBuddy open 连接器管理, trust iphone_use, then start a new chat.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
