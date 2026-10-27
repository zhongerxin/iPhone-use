"""iPhone Use's policy on top of the vendored Device Lease library.

``device_lease.py`` is a byte-identical copy of the reference implementation and is never
edited here. This module decides what is specific to iPhone Use: where this phone's lease
lives, which tool calls occupy the shared iPhone, which of them act on the current screen,
which count as a fresh look at it, and how queue errors read for a chat.
"""
import json
import os
from pathlib import Path
import re

from device_lease import device_directory

TRANSPORT = "wda"
# Reads of the current screen: they still take the lease, but a handover never refuses them.
READ_TOOLS = frozenset({"pua_ready", "pua_observe", "pua_find", "pua_wait"})
# A successful call clears "another chat used the phone since this chat last looked".
# pua_wait is not one: presence of one selector is not a look at the page.
OBSERVING_TOOLS = frozenset({"pua_ready", "pua_observe", "pua_find"})
# Never need the lease: diagnostics, catalogs, preview polling, the authentication wait and the
# lease tool itself, none of which act on the shared screen.
UNLEASED_TOOLS = frozenset({"pua_doctor", "pua_apps", "pua_metrics", "pua_screen", "pua_screen_frame", "pua_device"})
LEASED_SETUP_ACTIONS = frozenset({"start", "stop", "build", "configure", "fetch"})
UNLEASED_SCREEN_ACTIONS = frozenset({"refresh", "viewport"})


def lease_directory(state_dir):
    """The lease lives per physical iPhone, not per state directory.

    ``IPHONE_USE_LEASE_DIR`` overrides it. Otherwise the configured UDID selects the shared
    Device Lease directory ``ios-<udid>``, so two state directories that drive the same phone share
    one lock, another channel on that phone joins the same queue, and two phones never block each
    other. Before configuration the state directory itself holds the lease.
    """
    override = os.environ.get("IPHONE_USE_LEASE_DIR")
    if override:
        return Path(override).expanduser()
    udid = None
    try:
        udid = json.loads((Path(state_dir) / "config.json").read_text()).get("udid")
    except (OSError, ValueError, AttributeError):
        pass
    if isinstance(udid, str) and re.fullmatch(r"[A-Za-z0-9-]{8,64}", udid):
        return device_directory("ios", udid)
    return Path(state_dir) / "lease"


def needs_lease(name, args):
    """Which calls occupy the phone: everything behind the operation lock plus service lifecycle."""
    if name in UNLEASED_TOOLS:
        return False
    if name == "pua_setup":
        return isinstance(args, dict) and args.get("action") in LEASED_SETUP_ACTIONS
    if name == "pua_screen_action":
        return isinstance(args, dict) and args.get("action") not in UNLEASED_SCREEN_ACTIONS
    return True


def is_mutation(name, args):
    """Calls that act on whatever page is on screen; refused after a handover until a fresh look.

    The preview App's own gestures are the user's clicks on a live picture, so they are not
    refused. Service lifecycle does not touch the page either.
    """
    return not (name in READ_TOOLS or name in ("pua_setup", "pua_screen_action"))


def message(error):
    """A chat-facing sentence for a LeaseError: who holds the phone and what to do next."""
    details = error.details
    holder = details.get("holder") or {}
    who = (f"'{holder.get('label')}' (pid {holder.get('pid')}, {holder.get('transport')}, "
           f"idle {holder.get('idle_seconds')}s)") if holder else "another process"
    if error.code == "device_queued":
        return (f"The iPhone is in use by {who}; this chat is number {details.get('position')} in the queue after "
                f"waiting {details.get('waited_seconds')}s. Call any phone tool again within "
                f"{details.get('keep_place_seconds')}s to keep the place, or pua_device(action=release) to leave "
                "the queue. Tell the user who holds the phone; nothing was executed.")
    if error.code == "device_held":
        return f"The iPhone is in use by {who}; nothing was sent."
    if error.code == "fresh_observation_required":
        return ("Another chat used the iPhone since this chat last looked at it. Take a fresh "
                "pua_ready/pua_observe/pua_find first, then decide the action; nothing was executed.")
    return error.message
