#!/usr/bin/env python3
"""Prepare iPhone Use for Doubao Work: an installed server and two skills ready to add.

Doubao Work keeps custom connectors and skills in the account, entered through its own
window, so nothing in it can be registered from a script. This puts the server in a fixed
directory, proves it starts, and prints what to type into the connector form and which
folders to upload as skills. Phone state stays in ~/.local/share/iphone-use, shared with
the Codex and WorkBuddy installs.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import doubao_text


def workbuddy_installer():
    """workbuddy/install.py: packaging, the PATH a GUI app's child needs, the start-up check."""
    spec = importlib.util.spec_from_file_location("workbuddy_install", ROOT / "workbuddy/install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


shared = workbuddy_installer()
InstallError = shared.InstallError
NAME = "iphone-use-doubao"
CONNECTOR = "iphone_use"
SKILLS = ("iphone-use", "iphone-use-setup")
MARKER = ".doubao-install.json"
ENTRY = "doubao/mcp_stdio.py"
RUNTIME_FILES = ("mcp_stdio.py", "doubao_text.py")


def host_file(analytics):
    """What the entrypoint adds to the environment Doubao Work starts it with (see mcp_stdio.py)."""
    env = {"NO_PROXY": shared.LOOPBACK, "no_proxy": shared.LOOPBACK}
    if not analytics:
        # Upstream labels every event as its Codex plugin; a port should not add to that count unasked.
        env["IPHONE_USE_ANALYTICS"] = "0"
    return {"path": shared.server_path().split(os.pathsep), "env": env}


def stage(target, analytics):
    """Copy the upstream install package into target, with Doubao Work wording and entrypoint."""
    package = shared.packaging()
    version = package.validate()["version"]
    for source, relative in package.package_files():
        if relative.parts[0] in shared.CODEX_ONLY:
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative.parts[0] == "skills" and relative.suffix == ".md":
            destination.write_text(doubao_text.skill_text(relative.as_posix(), source.read_text()))
        elif relative.as_posix() == shared.SCREEN_PAGE:
            destination.write_text(doubao_text.screen_html(source.read_text()))
        else:
            shutil.copy2(source, destination)
    for directory, names in (("workbuddy", shared.RUNTIME_FILES), ("doubao", RUNTIME_FILES)):
        (target / directory).mkdir()
        for name in names:
            shutil.copy2(ROOT / directory / name, target / directory / name)
    (target / "doubao/host.json").write_text(json.dumps(host_file(analytics), indent=2) + "\n")
    (target / MARKER).write_text(json.dumps({"name": NAME, "version": version, "source": str(ROOT)}, indent=2) + "\n")
    return version


def connector(install_dir, python):
    """The custom connector form in Doubao Work, field by field."""
    return {"服务器名称": CONNECTOR, "传输类型": "STDIO", "命令": python, "参数": [str(install_dir / ENTRY)]}


def install(install_dir, python, analytics=False, force=False):
    if install_dir.exists() and not (install_dir / MARKER).is_file() and not force:
        raise InstallError(f"{install_dir} exists and was not installed by this script. Move it or pass --force.")
    with tempfile.TemporaryDirectory(prefix="iphone-use-stage-") as staging:
        staged = Path(staging) / NAME
        staged.mkdir()
        version = stage(staged, analytics)
        # Prove the package starts with nothing but system directories on PATH before anything is replaced.
        served, tools = shared.handshake({"command": python, "args": [str(staged / ENTRY)], "env": {"PATH": os.pathsep.join(shared.SYSTEM_PATH)}})
        if served != version:
            raise InstallError(f"Staged server reports {served}, expected {version}.")
        shared.replace_directory(staged, install_dir)
    return {"version": version, "tools": tools, "install_dir": str(install_dir), "connector": connector(install_dir, python),
            "skills": [str(install_dir / "skills" / name) for name in SKILLS]}


def uninstall(install_dir, force=False):
    """Remove the installed server. The connector and the skills are removed in Doubao Work itself."""
    if not install_dir.exists():
        return False
    if not (install_dir / MARKER).is_file() and not force:
        raise InstallError(f"{install_dir} was not installed by this script. Remove it yourself or pass --force.")
    shutil.rmtree(install_dir)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path.home() / ".local/share" / NAME, help="where the server is installed")
    parser.add_argument("--python", default=sys.executable, help="interpreter Doubao Work starts the server with (3.9+)")
    parser.add_argument("--analytics", action="store_true", help="keep upstream's anonymous usage statistics enabled")
    parser.add_argument("--force", action="store_true", help="replace or remove a directory this script did not create")
    parser.add_argument("--uninstall", action="store_true", help="remove the installed server")
    args = parser.parse_args()
    install_dir = args.dir.expanduser().resolve()
    try:
        if args.uninstall:
            print(f"Removed {install_dir}." if uninstall(install_dir, args.force) else "Nothing to remove.")
            print(f"In Doubao Work, 插件 · 技能 · 伙伴 > 管理: remove the connector {CONNECTOR} and the skills {', '.join(SKILLS)}.")
            return 0
        if sys.platform != "darwin":
            raise InstallError("iPhone Use needs macOS with Xcode.")
        python = shutil.which(args.python) or args.python
        if not Path(python).is_absolute() or not os.access(python, os.X_OK):
            raise InstallError(f"--python must resolve to an executable: {args.python}")
        result = install(install_dir, python, args.analytics, args.force)
    except (InstallError, doubao_text.Drift) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"iPhone Use {result['version']} prepared for Doubao Work ({result['tools']} tools) in {result['install_dir']}.")
    if not all(shutil.which(name) for name in ("node", "npm")):
        print("warning: node or npm was not found on PATH; install Node.js 20.19+ and run this script again.")
    print("\nIn Doubao Work, 插件 · 技能 · 伙伴 > 添加 > 新建自定义连接器:")
    for field, value in result["connector"].items():
        print(f"  {field}  {value if isinstance(value, str) else '  '.join(value)}")
    print("\nThen 添加 > 上传技能 > 选择文件夹, once for each:")
    for directory in result["skills"]:
        print(f"  {directory}")
    print(f"\nStart a new task on 本地电脑 and pick {CONNECTOR} under 插件.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
