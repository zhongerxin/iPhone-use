#!/usr/bin/env python3
"""Measure Jev requests or one bounded phone task without saving phone content."""
import argparse
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from jev_client import JevClient
from iphone_use import Runtime
from wda_client import WDAError
from wda_setup import state_directory


def fixture(client, samples):
    state = {"app": "iOS Settings", "elements": ["[1] 返回", "[2] 关于本机", "[3] 软件更新"]}
    questions = {
        "operation": {"type": "choice", "criteria": {"TAP": "Open an observed control", "DONE": "Already on the requested page"},
                      "instructions": "打开关于本机页面。"},
        "tap_target": {"type": "choice", "criteria": {"1": "返回", "2": "关于本机", "3": "软件更新"},
                       "instructions": "Choose the observed control that opens 关于本机."}}
    rows = []
    for index in range(samples):
        try:
            result = client.classify(state, questions)
            correct = (result["answers"]["operation"]["choice"] == "TAP"
                       and result["answers"].get("tap_target", {}).get("choice") == "2")
            rows.append({"sample": index + 1, "model": result["model"], "ms": result["latency_ms"], "correct": correct})
        except WDAError as error:
            rows.append({"sample": index + 1, "error_code": error.code, "correct": False})
    timings = [row["ms"] for row in rows if "ms" in row]
    return {"scope": "Synthetic Chinese control selection, HTTPS included; no phone actions or host-model timing.",
            "samples": rows, "correct": sum(row["correct"] for row in rows), "total": samples,
            "median_ms": round(statistics.median(timings), 3) if timings else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", help="Run one phone task instead of synthetic API samples; obtain READY first.")
    parser.add_argument("--expect", help="JSON selector for the explicit terminal UI condition.")
    parser.add_argument("--texts", default="[]", help="JSON array of literal selector/text pairs.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--samples", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--max-steps", type=int, choices=range(1, 21), default=8)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--state-dir")
    parser.add_argument("--output", type=Path, help="Save only timing summary, never nodes, text or API key.")
    args = parser.parse_args()
    runtime = client = None
    try:
        if args.goal:
            runtime = Runtime(args.state_dir)
            arguments = {"goal": args.goal, "texts": json.loads(args.texts), "max_steps": args.max_steps,
                         "timeout_seconds": args.timeout_seconds, "dry_run": args.dry_run}
            if args.expect:
                arguments["expect"] = json.loads(args.expect)
            result = runtime.call("pua_jev", arguments)
            summary = {"scope": "Bounded phone loop including observation, Jev and actions; excludes setup/READY and dispatch precheck.",
                       "status": result["status"], "complete": result["complete"], "steps": result["steps"],
                       "model": result.get("model"),
                       "metrics": result["metrics"], "operations": [step["operation"] for step in result["trace"]]}
            if result.get("error"):
                summary["error_code"] = result["error"]["code"]
            success = result["complete"] or result["status"] == "dry_run"
        else:
            client = JevClient(args.state_dir or state_directory())
            summary = fixture(client, args.samples)
            success = summary["correct"] == summary["total"]
        encoded = json.dumps(summary, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded + "\n")
        print(encoded)
        return 0 if success else 1
    except (WDAError, ValueError, TypeError) as error:
        print(json.dumps({"error_code": error.code if isinstance(error, WDAError) else "invalid_argument"}))
        return 1
    finally:
        if runtime:
            runtime.close()
        if client:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
