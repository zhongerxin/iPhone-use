#!/usr/bin/env python3
"""Named PUA (Phone Use Agent) operations when an MCP binding is unavailable; same runtime guards."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import PUAError, Runtime, SCHEMAS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["pua_" + name for name in SCHEMAS])
    parser.add_argument("arguments", nargs="?", default="{}", help="JSON object matching the MCP tool schema")
    parser.add_argument("--state-dir")
    parser.add_argument("--url")
    args = parser.parse_args()
    runtime = None
    try:
        arguments = json.loads(args.arguments, parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))
        runtime = Runtime(args.state_dir, args.url)
        result = runtime.call(args.tool, arguments)
    except PUAError as exc:
        result = {"error": exc.as_dict()}
    except (ValueError, TypeError) as exc:
        result = {"error": {"code": "invalid_argument", "message": str(exc), "uncertain": False}}
    finally:
        if runtime:
            runtime.close()
    try:
        if runtime:
            with runtime.access.delivery():
                print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    except PUAError as exc:
        result = {"error": exc.as_dict()}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if "error" in result or (args.tool == "pua_ready" and result.get("ready") is not True) else 0


if __name__ == "__main__":
    sys.exit(main())
