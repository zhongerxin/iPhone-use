"""Capture through Windows DVT; this does not require or prove working WDA."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
output = ROOT / "runtime" / "windows-device-screen.png"
output.parent.mkdir(exist_ok=True)
command = [sys.executable, "-m", "pymobiledevice3", "developer", "dvt", "screenshot", str(output), "--userspace"]
try:
    result = subprocess.run(command, timeout=45)
    if result.returncode:
        raise SystemExit(result.returncode)
    if not output.is_file() or not output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
        print("No valid PNG produced", file=sys.stderr)
        raise SystemExit(2)
    print(output)
except subprocess.TimeoutExpired:
    print("DVT screenshot timed out; reconnect and check device state before retrying", file=sys.stderr)
    raise SystemExit(2)
