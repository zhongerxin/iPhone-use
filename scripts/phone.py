#!/usr/bin/env python3
"""Named PUA (Phone Use Agent) operations when an MCP binding is unavailable; same runtime guards.

One-shot calls each run in a new process, so every call is a handover of the shared
device lease and actions need a fresh read first. ``--session`` serves MCP JSON lines
on stdin/stdout from one long-lived process instead, which keeps the lease across calls
exactly like the MCP server does.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from iphone_use import PUAError, Runtime, SCHEMAS, serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", nargs="?", choices=["pua_" + name for name in SCHEMAS])
    parser.add_argument("arguments", nargs="?", default="{}", help="JSON object matching the MCP tool schema")
    parser.add_argument("--state-dir")
    parser.add_argument("--url")
    parser.add_argument("--session", action="store_true",
                        help="Serve MCP JSON lines on stdin/stdout for a whole chat; one process keeps the device lease.")
    args = parser.parse_args()
    if args.session:
        runtime = Runtime(args.state_dir, args.url)
        try:
            serve(runtime)
        finally:
            runtime.close()
        return 0
    if not args.tool:
        parser.error("tool is required unless --session is given")
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
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 1 if "error" in result or (args.tool == "pua_ready" and result.get("ready") is not True) else 0


if __name__ == "__main__":
    sys.exit(main())
