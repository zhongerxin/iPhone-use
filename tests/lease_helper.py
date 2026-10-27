#!/usr/bin/env python3
"""Second-process actor for the device lease tests: hold, wait or crash on command.

Usage: lease_helper.py <lease_dir> <mode> [options]
  modes:
    hold      take the lease, then sleep --seconds, then release and exit
    crash     take the lease, then exit immediately without releasing
    child     take the lease, start a `sleep 30` child, then exit without releasing
    wait      queue with --wait seconds, then hold --seconds and release
    idle      take the lease and sleep --seconds without releasing (idle watchdog decides)
Events go to stdout as JSON lines so the test can order them.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from device_lease import DeviceLease, LeaseError


def event(kind, **fields):
    print(json.dumps({"event": kind, "pid": os.getpid(), "ts": time.time(), **fields}), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("lease_dir")
    parser.add_argument("mode", choices=["hold", "crash", "child", "wait", "idle"])
    parser.add_argument("--seconds", type=float, default=2.0)
    parser.add_argument("--wait", type=float, default=5.0)
    parser.add_argument("--label")
    args = parser.parse_args()
    lease = DeviceLease.get(args.lease_dir, transport="wda")
    if args.label:
        lease.label = args.label
    try:
        with lease.hold("pua_observe", mutation=False, wait_seconds=args.wait if args.mode == "wait" else 0):
            event("acquired", generation=lease.generation, handover=lease.handover)
    except LeaseError as error:
        event("error", code=error.code, details=error.details)
        return 2
    if args.mode == "crash":
        event("crashing")
        os._exit(0)
    if args.mode == "child":
        child = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        event("child", child_pid=child.pid)
        os._exit(0)
    time.sleep(args.seconds)
    if args.mode == "idle":
        event("exiting_without_release", holding=lease.holding)
        return 0
    lease.release("explicit")
    event("released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
