"""Install an existing signed WDA through pymobiledevice3 and verify its presence."""
import argparse
import asyncio
import json
from pathlib import Path
import plistlib
import re

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.usbmux import select_devices_by_connection_type

ROOT = Path(__file__).resolve().parents[1]


def signed_bundle():
    app = ROOT / "runtime/prepared/WebDriverAgentRunner-Runner.app"
    info = plistlib.loads((app / "Info.plist").read_bytes())
    bundle_id = info["CFBundleIdentifier"]
    if not re.fullmatch(r"com\.iphoneuse\.windows\.wda\.[A-Za-z0-9]+", bundle_id):
        raise RuntimeError("Expected a signed project WDA bundle with team suffix")
    if not (app / "embedded.mobileprovision").is_file():
        raise RuntimeError("Signed WDA has no provisioning profile")
    return app, bundle_id


async def run(verify_only):
    app, bundle_id = signed_bundle()
    devices = await select_devices_by_connection_type(connection_type="USB")
    if len(devices) != 1:
        raise RuntimeError(f"Expected one USB device; found {len(devices)}")
    lockdown = await create_using_usbmux(serial=devices[0].serial, autopair=False)
    try:
        async with InstallationProxyService(lockdown=lockdown) as service:
            if not verify_only:
                await service.install_from_local(app, developer=True)
            apps = await service.get_apps(bundle_identifiers=[bundle_id])
            if bundle_id not in apps:
                raise RuntimeError("WDA was not found in the installed app lookup")
        (ROOT / "runtime/install-result.json").write_text(
            json.dumps({"ok": True, "bundle_id": bundle_id, "verified_installed": True}), encoding="utf-8"
        )
        print("Signed WDA confirmed installed on the connected device.")
    finally:
        await lockdown.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--check-signed", action="store_true", help="Check local bundle only; no phone connection")
    args = parser.parse_args()
    if args.check_signed:
        try:
            signed_bundle()
        except (RuntimeError, OSError, KeyError, plistlib.InvalidFileException):
            raise SystemExit(3)
        print("Existing signed WDA bundle found; account login is unnecessary for install retry.")
    else:
        asyncio.run(run(args.verify_only))
