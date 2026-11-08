#!/bin/sh
# Set up WDA over Tailscale (or another private network) for the configured iPhone.
#   sh scripts/tailscale_setup.sh HOST [IDLE_MINUTES]  enable network mode (HOST = phone's Tailscale IP/name)
#   sh scripts/tailscale_setup.sh --remove              go back to USB only
# Nothing runs in the background: the plugin's `pua_setup start` launches WDA over the
# network on demand and shuts it down after IDLE_MINUTES (default 30, 0 = never) without requests.
# Run `pair-usb` once with the phone on USB first (see the end of this script's output).
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
state=${IPHONE_USE_STATE_DIR:-$HOME/.local/share/iphone-use}
venv=$state/runtime/pmd3
label=dev.iphone-use.tailscale
plist=$HOME/Library/LaunchAgents/$label.plist
PMD3_VERSION=11.50.2

if [ "${1:-}" = "--remove" ]; then
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$plist" "$state/network.json"
  echo "Removed network.json; iPhone Use is back to USB only."
  exit 0
fi
host=${1:?usage: tailscale_setup.sh HOST [IDLE_MINUTES] | --remove}
idle=${2:-30}
[ -f "$state/config.json" ] || { echo "Configure the phone over USB first (iphone-use-setup)." >&2; exit 1; }

if [ ! -x "$venv/bin/python" ] || ! "$venv/bin/python" -m pip show pymobiledevice3 2>/dev/null | grep -qx "Version: $PMD3_VERSION"; then
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install -q --disable-pip-version-check "pymobiledevice3==$PMD3_VERSION"
fi

umask 077
printf '{"enabled": true, "host": "%s", "wda_port": 8100, "mjpeg_port": 9100, "idle_minutes": %d}\n' "$host" "$idle" > "$state/network.json"
# Retire the always-on agent from earlier versions of this script.
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
rm -f "$plist"
echo "Network mode on: pua_setup start launches WDA over $host on demand; idle stop after $idle min."
echo "If not paired yet, with the phone on USB run once:"
echo "  $venv/bin/python -I $root/scripts/tailscale_wda.py pair-usb"
