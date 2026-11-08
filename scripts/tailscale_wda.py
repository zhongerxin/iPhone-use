#!/usr/bin/env python3
"""Run WDA over a private network (Tailscale) instead of USB.

Run with the pymobiledevice3 venv created by scripts/tailscale_setup.sh.

  run       Supervisor for launchd: forwards 127.0.0.1:<local_port> to the phone's WDA
            and relaunches the WDA runner over the network whenever WDA is down.
  pair-usb  One-time, phone on USB: create the RemotePairing record without a prompt.
            iOS refuses new pairings over the network.
  probe     Open the developer tunnel over the network and list the installed runner.
  launch    Launch the runner once in the foreground (debugging).

Launching needs the phone's Wi-Fi to be on (any network). iOS only listens for
RemotePairing (port 49152) while Wi-Fi is up. Control and the screen stream work
over cellular too. lockdownd (62078) resets requests over the network on iOS 27,
so this uses RemotePairing plus pymobiledevice3's no-root userspace tunnel.
"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import urllib.request

SERVER = Path(__file__).resolve().parents[1] / "server"
sys.path.insert(0, str(SERVER))
from wda_screen import network_target  # noqa: E402
from wda_setup import state_directory  # noqa: E402

REMOTE_PAIRING_PORT = 49152
CHECK_SECONDS = 15
LAUNCH_GRACE_SECONDS = 120
BACKOFF = (30, 60, 120, 300, 600)
log = logging.getLogger("iphone-use-tailscale")


def settings():
    state = Path(state_directory()).expanduser()
    config = json.loads((state / "config.json").read_text())
    network = network_target(state)
    if not network:
        raise SystemExit(f"Missing or invalid {state / 'network.json'}; run scripts/tailscale_setup.sh HOST.")
    return {"udid": config["udid"], "runner": config["bundle_id"] + ".xctrunner",
            "local_port": config.get("local_port", 18100), **network}


def remote_tunnel(cfg):
    """A UserspaceRsdTunnel whose provider is RemotePairing at host:49152 rather than USB lockdown."""
    from pymobiledevice3.remote import tunnel_service, userspace_tunnel

    async def provider(serial, autopair, remotepairing_fallback=True):
        service = tunnel_service.RemotePairingTunnelService(cfg["udid"], cfg["host"], REMOTE_PAIRING_PORT)
        await service.connect(autopair=False)  # never prompt; pair-usb creates the record
        return service, None

    userspace_tunnel._create_no_root_tunnel_provider = provider
    return userspace_tunnel.UserspaceRsdTunnel(serial=cfg["udid"])


async def launch(cfg):
    """Open the tunnel and run the WDA runner. Returns only when the test session ends."""
    from pymobiledevice3.services.dvt.testmanaged.xcuitest import TestConfig, XCUITestService
    async with remote_tunnel(cfg) as rsd:
        test = await TestConfig.create_for(rsd, cfg["runner"], None)
        test.runner_app_env = {"USE_PORT": str(cfg["wda_port"]), "MJPEG_SERVER_PORT": str(cfg["mjpeg_port"])}
        log.info("launching %s over %s", cfg["runner"], cfg["host"])
        await XCUITestService(rsd).run(test)


async def probe(cfg):
    from pymobiledevice3.services.installation_proxy import InstallationProxyService
    async with remote_tunnel(cfg) as rsd:
        async with InstallationProxyService(lockdown=rsd) as proxy:
            apps = await proxy.get_apps(application_type="User")
        print(json.dumps({"tunnel": True, "ios": rsd.product_version,
                          "runner_installed": cfg["runner"] in apps}))


async def pair_usb(cfg):
    from pymobiledevice3.exceptions import RemotePairingCompletedError
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
    lockdown = await create_using_usbmux(serial=cfg["udid"])
    service = await RemotePairingLockdownService.create(lockdown)
    try:
        await service.connect(autopair=True)
        print("RemotePairing record already valid.")
    except RemotePairingCompletedError:
        print(f"Paired over USB; record saved to {service.pair_record_path}. You can unplug the phone.")
    finally:
        await service.close()
        await lockdown.close()


def wda_ready(cfg):
    try:
        with urllib.request.urlopen(f"http://{cfg['host']}:{cfg['wda_port']}/status", timeout=5) as response:
            return json.load(response).get("value", {}).get("ready") is True
    except Exception:
        return False


async def forward(cfg):
    async def pipe(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def handle(client_reader, client_writer):
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(cfg["host"], cfg["wda_port"]), timeout=10)
        except (OSError, asyncio.TimeoutError) as error:
            log.warning("forward connect failed: %s", error)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, remote_writer), pipe(remote_reader, client_writer))

    # Loopback only: WDA has no authentication.
    server = await asyncio.start_server(handle, "127.0.0.1", cfg["local_port"])
    log.info("forwarding 127.0.0.1:%s -> %s:%s", cfg["local_port"], cfg["host"], cfg["wda_port"])
    return server


async def supervise(cfg):
    server = await forward(cfg)
    task, started, failures, down, retry_at = None, 0.0, 0, 0, 0.0
    loop = asyncio.get_running_loop()
    async with server:
        while True:
            ready = await asyncio.to_thread(wda_ready, cfg)
            now = loop.time()
            down = 0 if ready else down + 1
            if ready:
                failures = 0
            if task and task.done():
                error = None if task.cancelled() else task.exception()
                log.info("runner session ended%s", f": {error!r}" if error else "")
                task = None
                if not ready:
                    failures += 1
                    retry_at = now + BACKOFF[min(failures - 1, len(BACKOFF) - 1)]
            if task and not ready and now - started > LAUNCH_GRACE_SECONDS:
                log.warning("WDA not ready %ss after launch (phone locked or UI automation prompt?); retrying later",
                            LAUNCH_GRACE_SECONDS)
                task.cancel()
            if task is None and down >= 2 and now >= retry_at:
                started = now
                task = asyncio.create_task(launch(cfg))
            await asyncio.sleep(CHECK_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("run", "pair-usb", "probe", "launch"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("pymobiledevice3", "asyncio", "pytcp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.umask(0o077)
    cfg = settings()
    action = {"run": supervise, "pair-usb": pair_usb, "probe": probe, "launch": launch}[args.command]
    asyncio.run(action(cfg))


if __name__ == "__main__":
    main()
