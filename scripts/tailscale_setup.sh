#!/bin/sh
# Set up WDA over Tailscale (or another private network) for the configured iPhone.
#   sh scripts/tailscale_setup.sh HOST     install or update (HOST = the phone's Tailscale IP or name)
#   sh scripts/tailscale_setup.sh --remove stop the agent and go back to USB-only
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
  echo "Removed $label and network.json; iPhone Use is back to USB only."
  exit 0
fi
host=${1:?usage: tailscale_setup.sh HOST | --remove}
[ -f "$state/config.json" ] || { echo "Configure the phone over USB first (iphone-use-setup)." >&2; exit 1; }

if [ ! -x "$venv/bin/python" ] || ! "$venv/bin/python" -m pip show pymobiledevice3 2>/dev/null | grep -qx "Version: $PMD3_VERSION"; then
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install -q --disable-pip-version-check "pymobiledevice3==$PMD3_VERSION"
fi

umask 077
printf '{"enabled": true, "host": "%s", "wda_port": 8100, "mjpeg_port": 9100}\n' "$host" > "$state/network.json"
mkdir -p "$state/logs" "$HOME/Library/LaunchAgents"
cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$venv/bin/python</string><string>-I</string>
    <string>$root/scripts/tailscale_wda.py</string><string>run</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>IPHONE_USE_STATE_DIR</key><string>$state</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$state/logs/tailscale.log</string>
  <key>StandardErrorPath</key><string>$state/logs/tailscale.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
echo "Started $label: 127.0.0.1:18100 -> $host:8100, relaunching WDA over the network when it is down."
echo "Log: $state/logs/tailscale.log"
echo "If not paired yet, with the phone on USB run once:"
echo "  $venv/bin/python -I $root/scripts/tailscale_wda.py pair-usb"
