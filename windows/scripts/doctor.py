"""Read-only device discovery. Raw device identities stay in runtime/."""
import json
from pathlib import Path
import subprocess
import sys
from importlib.metadata import version

ROOT = Path(__file__).resolve().parents[1]


def main():
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, "-m", "pymobiledevice3", "usbmux", "list"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
    )
    (runtime / "usbmux-list.json").write_text(result.stdout, encoding="utf-8")
    (runtime / "usbmux-list.stderr.log").write_text(result.stderr, encoding="utf-8")
    try:
        devices = json.loads(result.stdout)
    except ValueError:
        print(json.dumps({"ok": False, "error": "Discovery returned invalid JSON", "exit_code": result.returncode}))
        return 2
    if not isinstance(devices, list):
        print(json.dumps({"ok": False, "error": "Unexpected discovery schema"}))
        return 2
    summary = {
        "ok": result.returncode == 0,
        "python": sys.version.split()[0],
        "pymobiledevice3": version("pymobiledevice3"),
        "device_count": len(devices),
        "devices": [{key: d.get(key) for key in ("DeviceName", "ProductType", "ProductVersion", "ConnectionType")} for d in devices],
    }
    (runtime / "doctor-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
