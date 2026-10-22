"""Windows smoke test using the unchanged upstream iPhone Use HTTP transport."""
import argparse
import base64
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "server"))
from wda_client import WDAClient, WDAError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "screenshot"])
    parser.add_argument("--url", default="http://127.0.0.1:18100")
    parser.add_argument("--output", type=Path, default=ROOT / "runtime" / "wda-screen.png")
    args = parser.parse_args()
    client = None
    try:
        client = WDAClient(args.url, timeout=5)
        status = client.request("GET", "/status")
        value = status.get("value", {})
        ready = isinstance(value, dict) and value.get("ready") is True
        if not ready:
            print(json.dumps({"ok": False, "stage": "status", "error": "WDA is reachable but not ready"}))
            return 2
        if args.action == "status":
            print(json.dumps({"ok": True, "stage": "status", "note": "HTTP ready only; UI automation not yet verified"}))
            return 0
        encoded = client.request("GET", "/screenshot").get("value")
        raw = base64.b64decode(encoded, validate=True)
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("WDA did not return a PNG")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(raw)
        print(json.dumps({"ok": True, "stage": "screenshot", "path": str(args.output)}))
        return 0
    except WDAError as exc:
        print(json.dumps({"ok": False, "error": exc.as_dict()}, ensure_ascii=False))
        return 2
    except (ValueError, TypeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
