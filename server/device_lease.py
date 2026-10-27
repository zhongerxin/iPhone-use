"""Device Lease: one physical device, many independent control processes, one at a time.

Reference implementation of the Device Lease protocol, on-disk format 1 (DEVICE_LEASE.md).

Several AI chats, each its own process, and several control channels (UI automation over
USB, a hardware KVM, ...) may drive the same phone. The first call of a process that would
touch the device takes a kernel ``flock`` on ``<lease_dir>/device.lock`` and keeps it while
calls keep coming; a watchdog hands it back after an idle period, and process death hands it
back through the kernel. Waiting processes queue through ticket files in FIFO order.

Plain files plus ``flock``: no daemon, socket or database, so a tool in any language can join
the same queue by following DEVICE_LEASE.md.

Channels vendor this file unchanged and keep their own policy in their own code: which calls
need the lease, which calls act on the current screen, which ones count as a fresh look at
it, and how queue errors are worded for their users.

Layout of one lease directory (default ``~/.local/share/device-lease/<platform>-<device id>``)::

    device.lock          the lease itself (held open by the owner process)
    device-owner.json    pid, label, transport, timestamps, generation, released_at
    device-queue/        one ``<ns>-<pid>.json`` ticket per waiting process
"""
import contextlib
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import threading
import time

PROTOCOL_FORMAT = 1
LIBRARY_VERSION = "2.0.0"
DEFAULT_LABEL = "未命名任务"
_PLATFORM = re.compile(r"[a-z][a-z0-9]{1,15}")
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,127}")


def lease_root():
    """``DEVICE_LEASE_ROOT`` or ``~/.local/share/device-lease``; read on each call so tests can move it."""
    override = os.environ.get("DEVICE_LEASE_ROOT")
    return Path(override).expanduser() if override else Path.home() / ".local/share/device-lease"


def device_directory(platform, device_id):
    """Lease directory of one physical device: ``<root>/<platform>-<device id>``.

    Name the hardware, never a connection: the iOS UDID, the Android ``ro.serialno`` (USB and
    Wi-Fi adb list the same phone twice), or a fingerprint of an appliance such as a KVM.
    """
    if not isinstance(platform, str) or not _PLATFORM.fullmatch(platform):
        raise ValueError("platform must be 2-16 lowercase letters or digits, e.g. ios or android")
    if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id) or ".." in device_id:
        raise ValueError("device id must be 3-128 letters, digits, dots, underscores or hyphens")
    return lease_root() / f"{platform}-{device_id}"


def devices(platform=None):
    """``(platform, device id, directory)`` for every lease directory under the root, optionally one platform."""
    root = lease_root()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    found = []
    for name in names:
        kind, separator, ident = name.partition("-")
        if (separator and _PLATFORM.fullmatch(kind) and _DEVICE_ID.fullmatch(ident) and ".." not in ident
                and (platform is None or kind == platform) and (root / name).is_dir()):
            found.append((kind, ident, root / name))
    return found


def _env_float(name, default, low, high):
    try:
        value = float(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if math.isfinite(value) and low <= value <= high else default


def settings():
    """Bounded tuning knobs, read on each access so tests can change them."""
    return {
        "idle_release_seconds": _env_float("DEVICE_LEASE_IDLE_RELEASE_SECONDS", 60.0, 1, 3600),
        "wait_seconds": _env_float("DEVICE_LEASE_WAIT_SECONDS", 40.0, 0, 55),
        "handover_settle_seconds": _env_float("DEVICE_LEASE_HANDOVER_SETTLE_SECONDS", 3.0, 0, 30),
        "ticket_ttl_seconds": _env_float("DEVICE_LEASE_TICKET_TTL_SECONDS", 30.0, 1, 300),
        "queue_hold_seconds": _env_float("DEVICE_LEASE_QUEUE_HOLD_SECONDS", 180.0, 0, 3600),
    }


class LeaseError(Exception):
    """Nothing was executed. ``details`` lets each channel word the error for its own users."""

    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = {"action_executed": False, "replay_action": False, **details}


def _alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _iso(ts):
    if not isinstance(ts, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


def _write_json(path, data):
    temp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
        os.replace(temp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temp)


def _read_json(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _seconds_since(ts, now):
    return round(now - ts, 1) if isinstance(ts, (int, float)) else None


class DeviceLease:
    """One instance per process and lease directory; every user in the process shares it.

    ``flock`` treats two descriptors of the same file as two lockers even inside one process,
    so sharing the descriptor is what makes a second user in the same process (another runtime
    object, a gesture thread, a test) cooperate instead of queueing behind itself.
    """

    _instances = {}
    _instances_lock = threading.Lock()

    @classmethod
    def get(cls, lease_dir, transport=None, default_label=None):
        """The process-wide lease for ``lease_dir``; ``transport`` names this channel, e.g. wda or kvm."""
        key = str(Path(lease_dir).expanduser().resolve(strict=False))
        with cls._instances_lock:
            lease = cls._instances.get(key)
            if lease is None:
                lease = cls._instances[key] = cls(key)
            lease.users += 1
        if transport:
            lease.transport = str(transport)[:32]
        if default_label:
            lease.default_label = str(default_label)[:60]
        return lease

    def __init__(self, lease_dir):
        self.dir = Path(lease_dir)
        self.queue_dir = self.dir / "device-queue"
        self.lock_path = self.dir / "device.lock"
        self.owner_path = self.dir / "device-owner.json"
        self.pid = os.getpid()
        self.transport = "unknown"
        self.default_label = DEFAULT_LABEL
        self.users = 0
        self.label = None
        self.auto_label = None
        self.needs_observation = False
        self.generation = None
        self.handover = None
        self._fd = None
        self._in_call = 0
        self._last_call = time.monotonic()
        self._acquired_at = None
        self._ticket = None
        self._ticket_wanted_until = 0.0
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._watchdog = None

    # ----- public API -------------------------------------------------------
    @property
    def holding(self):
        return self._fd is not None

    @contextlib.contextmanager
    def hold(self, name, *, mutation=True, wait_seconds=None):
        """Run one device call under the lease; acquire it first when this process lacks it.

        ``name`` is recorded as the last call. ``mutation`` marks calls that act on whatever is on
        screen: after another process used the device they raise ``fresh_observation_required``
        until ``observed()``. ``wait_seconds=0`` never queues and never jumps ahead of a queue.
        """
        with self._lock:
            if not self.holding:
                self._acquire(settings()["wait_seconds"] if wait_seconds is None else wait_seconds)
            if self.needs_observation and mutation:
                raise LeaseError("fresh_observation_required",
                                 "Another process used the device since this process last observed it; observe "
                                 "the screen first, then decide the action. Nothing was executed.",
                                 generation=self.generation)
            self._in_call += 1
            self._last_call = time.monotonic()
        try:
            yield self
        finally:
            with self._lock:
                self._in_call -= 1
                self._last_call = time.monotonic()
                if self.holding:
                    self._write_owner(last_tool=str(name)[:64])

    def set_auto_label(self, label):
        """A label taken from the work itself, e.g. the app just launched; an explicit claim label wins."""
        if isinstance(label, str) and label:
            with self._lock:
                self.auto_label = label[:60]

    def observed(self, auto_label=None):
        """A fresh look at the current screen clears the handover requirement."""
        with self._lock:
            self.needs_observation = False
            if isinstance(auto_label, str) and auto_label and self.label is None:
                self.auto_label = auto_label[:60]

    def claim(self, label=None, wait_seconds=None):
        """Take the device now, optionally with an explicit task label other processes see."""
        with self._lock:
            if label:
                self.label = str(label)[:60]
            if not self.holding:
                self._acquire(settings()["wait_seconds"] if wait_seconds is None else wait_seconds)
            else:
                self._write_owner()
            return self.status()

    def release(self, reason="explicit"):
        """Hand the device back now and leave the queue. A call in flight finishes first; this never cuts it short."""
        with self._lock:
            self._drop_ticket()
            if not self.holding or self._in_call:
                return False
            self._write_owner(released_at=time.time(), release_reason=reason)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
                self._acquired_at = None
            return True

    def waiting(self):
        """Live tickets of other processes."""
        return [t for t in self._tickets() if t["pid"] != self.pid]

    def status(self):
        with self._lock:
            owner = _read_json(self.owner_path)
            now = time.time()
            holder = None
            if owner and "released_at" not in owner and _alive(owner.get("pid")):
                holder = {"pid": owner.get("pid"), "label": owner.get("label") or DEFAULT_LABEL,
                          "transport": owner.get("transport", "unknown"),
                          "since": _iso(owner.get("acquired_ts")),
                          "held_seconds": _seconds_since(owner.get("acquired_ts"), now),
                          "idle_seconds": _seconds_since(owner.get("last_call_ts"), now),
                          "last_tool": owner.get("last_tool"),
                          "is_me": owner.get("pid") == self.pid}
            queue = []
            for position, ticket in enumerate(self._tickets(), 1):
                queue.append({"position": position, "pid": ticket["pid"], "label": ticket.get("label") or DEFAULT_LABEL,
                              "transport": ticket.get("transport", "unknown"),
                              "waiting_seconds": _seconds_since(ticket.get("enqueued_ts"), now),
                              "is_me": ticket["pid"] == self.pid})
            return {"holder": holder, "queue": queue,
                    "me": {"pid": self.pid, "holding": self.holding, "needs_observation": self.needs_observation,
                           "label": self.current_label(), "queued": any(q["is_me"] for q in queue)},
                    "idle_release_seconds": settings()["idle_release_seconds"], "lease_dir": str(self.dir)}

    def coordination(self):
        """Small note for tool results, only while someone else is waiting."""
        waiting = len(self.waiting())
        if not waiting:
            return None
        held = round(time.time() - self._acquired_at, 1) if self._acquired_at else None
        return {"waiting": waiting, "held_seconds": held, "idle_release_seconds": settings()["idle_release_seconds"]}

    def current_label(self):
        return self.label or self.auto_label or self.default_label

    def close(self):
        """Called by every user that obtained the lease with ``get``; the lease ends with the last one."""
        with self._lock:
            self.users = max(0, self.users - 1)
            if self.users:
                return
            self._closed.set()
            self.release("closed")
        with self._instances_lock:
            if self._instances.get(str(self.dir)) is self:
                del self._instances[str(self.dir)]

    # ----- acquisition ------------------------------------------------------
    def _acquire(self, wait_seconds):
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.queue_dir.mkdir(mode=0o700, exist_ok=True)
        self._start_watchdog()
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        if wait_seconds <= 0:
            # A non-blocking attempt never jumps ahead of a queue and leaves no ticket.
            self._prune()
            if not self._tickets() and self._try_flock():
                self._on_acquired()
                return
            self._raise_busy("device_held", 0)
        ticket = self._ensure_ticket()
        while True:
            self._prune()
            tickets = self._tickets()
            if not tickets or tickets[0]["path"] == ticket:
                if self._try_flock():
                    self._on_acquired()
                    return
            if time.monotonic() >= deadline:
                self._ticket_wanted_until = time.monotonic() + settings()["queue_hold_seconds"]
                self._raise_busy("device_queued", wait_seconds)
            self._refresh_ticket()
            time.sleep(0.25)

    def _raise_busy(self, code, waited):
        snapshot = self.status()
        holder = snapshot["holder"] or {}
        position = next((q["position"] for q in snapshot["queue"] if q["is_me"]), None)
        hold = settings()["queue_hold_seconds"]
        who = (f"'{holder.get('label')}' (pid {holder.get('pid')}, transport {holder.get('transport')}, "
               f"idle {holder.get('idle_seconds')}s)") if holder else "another process"
        if code == "device_queued":
            message = (f"The device is in use by {who}; this process is number {position} in the queue after "
                       f"waiting {int(waited)}s. Call again within {int(hold)}s to keep the place. Nothing was executed.")
        else:
            message = f"The device is in use by {who}. Nothing was executed."
        raise LeaseError(code, message, holder=holder or None, position=position, waiting=len(snapshot["queue"]),
                         waited_seconds=int(waited), keep_place_seconds=int(hold),
                         idle_release_seconds=settings()["idle_release_seconds"])

    def _try_flock(self):
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def _on_acquired(self):
        previous = _read_json(self.owner_path)
        self._drop_ticket()
        now = time.time()
        handover = bool(previous) and previous.get("pid") != self.pid
        generation = previous.get("generation", 0) + 1 if previous and isinstance(previous.get("generation"), int) else 1
        self.generation = generation
        self.handover = {"from_pid": previous.get("pid"), "clean": "released_at" in previous} if handover else None
        self._acquired_at = now
        self._last_call = time.monotonic()
        self._write_owner(acquired_ts=now)
        if handover:
            self.needs_observation = True
            if "released_at" not in previous:
                # The previous holder died without releasing: one device command may still be in
                # flight. Let it finish before the first command of this process.
                time.sleep(settings()["handover_settle_seconds"])

    def _write_owner(self, **extra):
        if not self.holding and "released_at" not in extra:
            return
        current = _read_json(self.owner_path) if self.holding else None
        if current and current.get("pid") != self.pid:
            current = None
        data = dict(current or {})
        data.update({"pid": self.pid, "label": self.current_label(), "transport": self.transport,
                     "generation": self.generation, "last_call_ts": time.time(), "format": PROTOCOL_FORMAT})
        if "acquired_ts" in extra:
            data["acquired_ts"] = extra.pop("acquired_ts")
        else:
            data.setdefault("acquired_ts", self._acquired_at or time.time())
        data.pop("released_at", None)
        data.pop("release_reason", None)
        data.update(extra)
        try:
            _write_json(self.owner_path, data)
        except OSError:
            # The directory may already be gone (tests, uninstall); bookkeeping never blocks the unlock.
            pass

    # ----- queue ------------------------------------------------------------
    def _ensure_ticket(self):
        if self._ticket is not None and self._ticket.exists():
            self._refresh_ticket()
            return self._ticket
        path = self.queue_dir / f"{time.time_ns():020d}-{self.pid}.json"
        _write_json(path, {"pid": self.pid, "label": self.current_label(), "transport": self.transport,
                           "enqueued_ts": time.time(), "refreshed_ts": time.time(), "format": PROTOCOL_FORMAT})
        self._ticket = path
        return path

    def _refresh_ticket(self):
        if self._ticket is None:
            return
        data = _read_json(self._ticket)
        if data is None:
            self._ticket = None
            return
        data["refreshed_ts"] = time.time()
        data["label"] = self.current_label()
        _write_json(self._ticket, data)

    def _drop_ticket(self):
        self._ticket_wanted_until = 0.0
        if self._ticket is not None:
            with contextlib.suppress(OSError):
                self._ticket.unlink()
            self._ticket = None

    def _holder_pid(self):
        owner = _read_json(self.owner_path)
        return owner.get("pid") if owner and "released_at" not in owner and _alive(owner.get("pid")) else None

    def _tickets(self):
        """Live tickets in FIFO order; each carries its path."""
        ttl = settings()["ticket_ttl_seconds"]
        now = time.time()
        holder_pid = self._holder_pid()
        result = []
        try:
            names = sorted(os.listdir(self.queue_dir))
        except OSError:
            return result
        for name in names:
            if not name.endswith(".json"):
                continue
            path = self.queue_dir / name
            data = _read_json(path)
            if data is None:
                continue
            pid = data.get("pid")
            refreshed = data.get("refreshed_ts")
            if _alive(pid) and isinstance(refreshed, (int, float)) and now - refreshed <= ttl and pid != holder_pid:
                result.append({**data, "path": path})
        return result

    def _prune(self):
        """Remove tickets of dead, expired or already-holding processes, and stale temporary files."""
        ttl = settings()["ticket_ttl_seconds"]
        now = time.time()
        holder_pid = self._holder_pid()
        try:
            names = os.listdir(self.queue_dir)
        except OSError:
            return
        for name in names:
            path = self.queue_dir / name
            if name.endswith(".tmp"):
                with contextlib.suppress(OSError):
                    if now - path.stat().st_mtime > ttl:
                        path.unlink()
                continue
            data = _read_json(path)
            pid = data.get("pid") if data else None
            refreshed = data.get("refreshed_ts") if data else None
            stale = (data is None or not _alive(pid) or not isinstance(refreshed, (int, float))
                     or now - refreshed > ttl or (pid == holder_pid and pid != self.pid))
            if stale:
                with contextlib.suppress(OSError):
                    path.unlink()

    # ----- watchdog ---------------------------------------------------------
    def _start_watchdog(self):
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._closed.clear()
        self._watchdog = threading.Thread(target=self._watch, name="device-lease-watchdog", daemon=True)
        self._watchdog.start()

    def _watch(self):
        while not self._closed.wait(1.0):
            try:
                with self._lock:
                    now = time.monotonic()
                    if self.holding and not self._in_call and now - self._last_call >= settings()["idle_release_seconds"]:
                        self.release("idle")
                    if self._ticket is not None and not self.holding:
                        if now < self._ticket_wanted_until:
                            self._refresh_ticket()
                        else:
                            self._drop_ticket()
            except Exception:
                pass
