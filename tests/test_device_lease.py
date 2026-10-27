"""Device lease: several processes share one iPhone in FIFO order without a daemon."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
from device_lease import DeviceLease, LeaseError
from lease_policy import is_mutation, lease_directory, needs_lease
from iphone_use import Runtime, tool_result
from wda_client import WDAError
from wda_controller import PhoneController
from test_controller import FakeWDA

HELPER = ROOT / "tests" / "lease_helper.py"
FAST = {"DEVICE_LEASE_IDLE_RELEASE_SECONDS": "1", "DEVICE_LEASE_WAIT_SECONDS": "1", "DEVICE_LEASE_TICKET_TTL_SECONDS": "2",
        "DEVICE_LEASE_HANDOVER_SETTLE_SECONDS": "0.3", "DEVICE_LEASE_QUEUE_HOLD_SECONDS": "180", "IPHONE_USE_ANALYTICS": "0"}


def wait_for(predicate, seconds=8.0, step=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class LeaseTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name) / "state"
        self.state_dir.mkdir()
        self.lease_dir = Path(self.directory.name) / "lease"
        self.previous = {key: os.environ.get(key) for key in list(FAST) + ["IPHONE_USE_LEASE_DIR"]}
        os.environ.update(FAST)
        os.environ["IPHONE_USE_LEASE_DIR"] = str(self.lease_dir)
        self.addCleanup(self.restore_environment)
        self.helpers = []
        self.addCleanup(self.stop_helpers)
        self.runtimes = []
        self.addCleanup(self.close_runtimes)

    def restore_environment(self):
        for key, value in self.previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def stop_helpers(self):
        for process in self.helpers:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def close_runtimes(self):
        for runtime in self.runtimes:
            try:
                runtime.close()
            except Exception:
                pass

    def make_runtime(self, state_dir=None):
        client = FakeWDA()
        runtime = Runtime(str(state_dir or self.state_dir), "http://127.0.0.1:18100")
        runtime.client.close()
        client.close = lambda: None
        runtime.client = client
        runtime.phone = PhoneController(client, runtime.state_dir)
        runtime.phone.screen = runtime.screen
        if hasattr(runtime, "access"):  # the maintenance fork's authentication gate; upstream has none
            runtime.phone.access_check = runtime.access.check
        self.runtimes.append(runtime)
        return runtime

    def helper(self, mode, *extra, env=None):
        process = subprocess.Popen([sys.executable, "-I", str(HELPER), str(self.lease_dir), mode, *extra],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   env={**os.environ, **FAST, "DEVICE_LEASE_IDLE_RELEASE_SECONDS": "60", **(env or {})})
        self.helpers.append(process)
        return process

    @staticmethod
    def read_event(process, kind, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                time.sleep(0.05)
                if process.poll() is not None:
                    break
                continue
            data = json.loads(line)
            if data["event"] == kind:
                return data
        raise AssertionError(f"helper never reported {kind}; stderr: {process.stderr.read()}")

    def owner(self):
        path = self.lease_dir / "device-owner.json"
        return json.loads(path.read_text()) if path.exists() else None

    def press_home(self, runtime):
        return runtime.call("pua_press_button", {"name": "home", "observe": "none", "verify": False})


class ClassificationTests(unittest.TestCase):
    def test_which_calls_need_the_lease(self):
        self.assertTrue(needs_lease("pua_tap", {"x": 1, "y": 1}))
        self.assertTrue(needs_lease("pua_observe", {}))
        self.assertTrue(needs_lease("pua_setup", {"action": "start"}))
        self.assertTrue(needs_lease("pua_setup", {"action": "stop"}))
        self.assertFalse(needs_lease("pua_setup", {"action": "status"}))
        self.assertFalse(needs_lease("pua_setup", {"action": "discover"}))
        self.assertFalse(needs_lease("pua_doctor", {}))
        self.assertFalse(needs_lease("pua_apps", {"query": "x"}))
        self.assertFalse(needs_lease("pua_screen", {"action": "wait_auth"}))
        self.assertFalse(needs_lease("pua_screen_frame", {}))
        self.assertFalse(needs_lease("pua_device", {"action": "status"}))
        self.assertTrue(needs_lease("pua_screen_action", {"action": "tap"}))
        self.assertTrue(needs_lease("pua_screen_action", {"action": "home"}))
        self.assertFalse(needs_lease("pua_screen_action", {"action": "refresh"}))
        self.assertFalse(needs_lease("pua_screen_action", {"action": "viewport"}))

    def test_reads_setup_and_user_gestures_are_not_mutations(self):
        self.assertTrue(is_mutation("pua_tap", {}))
        self.assertTrue(is_mutation("pua_batch", {}))
        self.assertTrue(is_mutation("pua_screenshot", {}))
        self.assertFalse(is_mutation("pua_observe", {}))
        self.assertFalse(is_mutation("pua_ready", {}))
        self.assertFalse(is_mutation("pua_setup", {"action": "start"}))
        self.assertFalse(is_mutation("pua_screen_action", {"action": "tap"}))

    def test_lease_directory_follows_the_configured_device(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            saved_root, saved_env = os.environ.get("DEVICE_LEASE_ROOT"), os.environ.pop("IPHONE_USE_LEASE_DIR", None)
            os.environ["DEVICE_LEASE_ROOT"] = str(root / "leases")
            try:
                state_a = root / "a"; state_a.mkdir()
                state_b = root / "b"; state_b.mkdir()
                self.assertEqual(lease_directory(state_a), state_a / "lease")
                (state_a / "config.json").write_text(json.dumps({"udid": "00008888-0000111122223333", "local_port": 18100}))
                (state_b / "config.json").write_text(json.dumps({"udid": "00008888-0000111122223333", "local_port": 18101}))
                self.assertEqual(lease_directory(state_a), root / "leases" / "ios-00008888-0000111122223333")
                self.assertEqual(lease_directory(state_a), lease_directory(state_b), "same phone, same lock")
                (state_b / "config.json").write_text(json.dumps({"udid": "00008120-0011223344556677"}))
                self.assertNotEqual(lease_directory(state_a), lease_directory(state_b), "two phones, two locks")
                (state_b / "config.json").write_text(json.dumps({"udid": "../escape"}))
                self.assertEqual(lease_directory(state_b), state_b / "lease")
                os.environ["IPHONE_USE_LEASE_DIR"] = str(root / "override")
                self.assertEqual(lease_directory(state_a), root / "override")
            finally:
                if saved_root is None:
                    os.environ.pop("DEVICE_LEASE_ROOT", None)
                else:
                    os.environ["DEVICE_LEASE_ROOT"] = saved_root
                os.environ.pop("IPHONE_USE_LEASE_DIR", None)
                if saved_env is not None:
                    os.environ["IPHONE_USE_LEASE_DIR"] = saved_env


class SingleProcessTests(LeaseTestCase):
    def test_phone_call_takes_lease_and_device_tool_reports_it(self):
        runtime = self.make_runtime()
        status = runtime.call("pua_device", {"action": "status"})
        self.assertIsNone(status["holder"])
        self.assertFalse(status["me"]["holding"])
        self.press_home(runtime)
        status = runtime.call("pua_device", {"action": "status"})
        self.assertTrue(status["holder"]["is_me"])
        self.assertEqual(status["holder"]["pid"], os.getpid())
        self.assertEqual(status["holder"]["label"], "未命名任务")
        self.assertEqual(status["holder"]["last_tool"], "pua_press_button")
        self.assertEqual(status["queue"], [])
        self.assertTrue(status["me"]["holding"])
        self.assertFalse(status["me"]["needs_observation"])
        released = runtime.call("pua_device", {"action": "release"})
        self.assertTrue(released["released"])
        self.assertIsNone(released["holder"])
        owner = self.owner()
        self.assertIn("released_at", owner)
        self.assertEqual(owner["release_reason"], "explicit")
        self.assertFalse((self.lease_dir / "device-queue").exists() and any((self.lease_dir / "device-queue").iterdir()))

    def test_quiet_release_hands_back_and_replies_with_an_empty_object(self):
        # A Codex Stop hook calls pua_device at turn end. Codex accepts only empty output or
        # hook JSON from it, so quiet must still release but reply with nothing else than {}.
        runtime = self.make_runtime()
        self.press_home(runtime)
        self.assertTrue(runtime.lease.holding)
        self.assertEqual(runtime.call("pua_device", {"action": "release", "quiet": True}), {})
        self.assertFalse(runtime.lease.holding)
        owner = self.owner()
        self.assertIn("released_at", owner)
        self.assertEqual(owner["release_reason"], "explicit")
        self.assertEqual(runtime.call("pua_device", {"action": "status", "quiet": True}), {})
        reply = tool_result(runtime, {"name": "pua_device", "arguments": {"action": "release", "quiet": True}})
        self.assertEqual(reply, {"content": [{"type": "text", "text": "{}"}], "isError": False})
        refused = tool_result(runtime, {"name": "pua_device", "arguments": {"action": "release", "quiet": "yes"}})
        self.assertTrue(refused["isError"])

    def test_claim_labels_the_task_and_launch_sets_an_automatic_label(self):
        runtime = self.make_runtime()
        status = runtime.call("pua_device", {"action": "claim", "label": "山姆购物车"})
        self.assertTrue(status["holder"]["is_me"])
        self.assertEqual(status["holder"]["label"], "山姆购物车")
        self.assertEqual(self.owner()["label"], "山姆购物车")
        other = self.make_runtime(self.state_dir)  # same process, same lease
        self.assertIs(other.lease, runtime.lease)
        runtime.call("pua_device", {"action": "release"})
        auto = self.make_runtime(Path(self.directory.name) / "state2")
        self.assertIs(auto.lease, runtime.lease, "one lease directory, one instance per process")
        auto.lease.label = None  # without an explicit claim the launched app names the task
        auto.call("pua_launch_app", {"bundle_id": "com.example.shop"})
        self.assertEqual(auto.lease.auto_label, "com.example.shop")
        self.assertEqual(auto.lease.current_label(), "com.example.shop")
        self.assertEqual(self.owner()["label"], "com.example.shop")

    def test_idle_watchdog_releases_and_same_process_reacquires_silently(self):
        runtime = self.make_runtime()
        self.press_home(runtime)
        self.assertTrue(runtime.lease.holding)
        self.assertTrue(wait_for(lambda: not runtime.lease.holding, seconds=4), "idle watchdog did not release")
        self.assertEqual(self.owner()["release_reason"], "idle")
        self.press_home(runtime)  # nobody else held it: no fresh observation demanded
        self.assertTrue(runtime.lease.holding)
        self.assertFalse(runtime.lease.needs_observation)
        self.assertIsNone(runtime.lease.handover)

    def test_two_runtimes_in_one_process_share_one_lease(self):
        first = self.make_runtime()
        second = self.make_runtime()
        self.assertIs(first.lease, second.lease)
        self.press_home(first)
        self.press_home(second)  # no device_held between runtimes of the same process
        self.assertTrue(first.lease.holding)
        first.close()
        self.assertTrue(second.lease.holding, "closing one Runtime must not release the shared lease")
        second.close()
        self.assertFalse(second.lease.holding)
        self.assertIn("released_at", self.owner())
        self.runtimes.clear()


class MultiProcessTests(LeaseTestCase):
    def test_second_chat_queues_then_takes_over_and_must_observe_first(self):
        holder = self.helper("hold", "--seconds", "3", "--label", "山姆")
        self.read_event(holder, "acquired")
        runtime = self.make_runtime()
        with self.assertRaises(WDAError) as caught:
            self.press_home(runtime)
        error = caught.exception
        self.assertEqual(error.code, "device_queued")
        self.assertEqual(error.details["holder"]["label"], "山姆")
        self.assertEqual(error.details["position"], 1)
        self.assertFalse(error.details["action_executed"])
        self.assertIn("山姆", str(error))
        status = runtime.call("pua_device", {"action": "status"})
        self.assertEqual(status["holder"]["label"], "山姆")
        self.assertFalse(status["holder"]["is_me"])
        self.assertTrue(status["me"]["queued"])
        self.assertEqual(status["queue"][0]["position"], 1)
        self.assertTrue(status["queue"][0]["is_me"])
        self.read_event(holder, "released")
        # The next call after the handover is an action: refused until this chat looks at the screen.
        with self.assertRaises(WDAError) as caught:
            self.press_home(runtime)
        self.assertEqual(caught.exception.code, "fresh_observation_required")
        self.assertFalse(caught.exception.details["action_executed"])
        self.assertTrue(runtime.lease.holding)
        self.assertTrue(runtime.lease.handover["clean"])
        runtime.call("pua_observe", {"mode": "tree"})
        self.assertFalse(runtime.lease.needs_observation)
        self.press_home(runtime)
        self.assertEqual(self.owner()["pid"], os.getpid())
        self.assertEqual(runtime.call("pua_device", {"action": "status"})["queue"], [])

    def test_queue_is_first_in_first_out(self):
        holder = self.helper("hold", "--seconds", "2", "--label", "A")
        self.read_event(holder, "acquired")
        second = self.helper("wait", "--wait", "10", "--seconds", "1", "--label", "B")
        self.assertTrue(wait_for(lambda: any(t["label"] == "B" for t in DeviceLease.get(self.lease_dir)._tickets()), seconds=3))
        third = self.helper("wait", "--wait", "10", "--seconds", "0.2", "--label", "C")
        self.assertTrue(wait_for(lambda: len(DeviceLease.get(self.lease_dir)._tickets()) == 2, seconds=3))
        self.read_event(holder, "released")
        acquired_b = self.read_event(second, "acquired")
        acquired_c = self.read_event(third, "acquired")
        self.assertLess(acquired_b["ts"], acquired_c["ts"])
        self.assertEqual(acquired_c["handover"]["from_pid"], acquired_b["pid"])
        self.assertTrue(acquired_c["handover"]["clean"])

    def test_dead_holder_frees_the_lock_and_the_next_chat_settles(self):
        crashed = self.helper("crash", "--label", "崩溃的对话")
        self.read_event(crashed, "crashing")
        crashed.wait(timeout=5)
        runtime = self.make_runtime()
        started = time.monotonic()
        with self.assertRaises(WDAError) as caught:
            self.press_home(runtime)
        self.assertEqual(caught.exception.code, "fresh_observation_required")
        self.assertGreaterEqual(time.monotonic() - started, 0.3, "an unclean handover waits the settle time")
        self.assertFalse(runtime.lease.handover["clean"])
        self.assertEqual(runtime.lease.handover["from_pid"], crashed.pid)

    def test_lock_is_not_inherited_by_long_lived_children(self):
        parent = self.helper("child")
        child_pid = self.read_event(parent, "child")["child_pid"]
        parent.wait(timeout=5)
        try:
            runtime = self.make_runtime()
            self.assertTrue(wait_for(lambda: self.try_acquire(runtime), seconds=3), "the sleep child must not keep the lease")
        finally:
            try:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
            except (ProcessLookupError, ChildProcessError):
                pass

    def try_acquire(self, runtime):
        try:
            runtime.call("pua_observe", {"mode": "tree"})
        except WDAError as error:
            if error.code in ("device_queued", "device_held"):
                return False
            raise
        return True

    def test_stale_tickets_do_not_block_the_queue(self):
        queue = self.lease_dir / "device-queue"
        queue.mkdir(parents=True)
        (queue / "00000000000000000001-999999.json").write_text(json.dumps({"pid": 999999, "refreshed_ts": time.time(), "enqueued_ts": time.time()}))
        sleeper = subprocess.Popen(["sleep", "30"])
        self.addCleanup(lambda: (sleeper.kill(), sleeper.wait()))
        (queue / ("00000000000000000002-%d.json" % sleeper.pid)).write_text(json.dumps({"pid": sleeper.pid, "refreshed_ts": time.time() - 10, "enqueued_ts": time.time() - 10}))
        runtime = self.make_runtime()
        self.press_home(runtime)  # dead pid and expired ticket are pruned; this chat is the head
        self.assertEqual(sorted(p.name for p in queue.iterdir()), [])

    def test_queued_place_survives_model_latency(self):
        holder = self.helper("hold", "--seconds", "6", "--label", "A")
        self.read_event(holder, "acquired")
        runtime = self.make_runtime()
        with self.assertRaises(WDAError) as caught:
            self.press_home(runtime)
        self.assertEqual(caught.exception.code, "device_queued")
        time.sleep(3)  # longer than the 2 second ticket TTL: the watchdog keeps refreshing our ticket
        self.assertTrue(runtime.call("pua_device", {"action": "status"})["me"]["queued"])
        runtime.call("pua_device", {"action": "release"})  # leaving the queue drops the ticket
        self.assertFalse(runtime.call("pua_device", {"action": "status"})["me"]["queued"])

    def test_preview_gesture_from_another_chat_is_refused_without_waiting(self):
        holder = self.helper("hold", "--seconds", "3", "--label", "A")
        self.read_event(holder, "acquired")
        runtime = self.make_runtime()
        started = time.monotonic()
        with self.assertRaises(WDAError) as caught:
            runtime.call("pua_screen_action", {"action": "tap", "x": 10, "y": 10, "width": 390, "height": 844})
        self.assertEqual(caught.exception.code, "device_held")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(runtime.lease.status()["me"]["queued"], "a refused gesture leaves no ticket")

    def test_holder_sees_waiting_count_in_results(self):
        runtime = self.make_runtime()
        self.press_home(runtime)
        waiter = self.helper("wait", "--wait", "10", "--seconds", "0.1", "--label", "B")
        self.assertTrue(wait_for(lambda: len(runtime.lease.waiting()) == 1, seconds=3))
        result = self.press_home(runtime)
        self.assertEqual(result["coordination"]["waiting"], 1)
        runtime.call("pua_device", {"action": "release"})
        self.read_event(waiter, "acquired")


if __name__ == "__main__":
    unittest.main()
