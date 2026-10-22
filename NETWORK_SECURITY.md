# WDA loopback policy

The iPhone Use build of WDA accepts USB-forwarded traffic on `127.0.0.1` only. Both HTTP control and MJPEG use this rule. It has no LAN mode and opens no IPv6 listener.

The upstream source remains pinned to WDA `16.14.0`, commit `d17782422d55ff1e5e0ceb74eb1fd509cc0c35b6`. The original checkout stays unchanged. Setup creates its own `runtime/WebDriverAgent-loopback-<patch hash prefix>` copy for building. It accepts that copy only when its HEAD and full tracked diff match the pinned commit and bundled patch exactly, and there are no extra untracked build inputs. Any other changes cause a failure and remain on disk for inspection.

The patch is `server/patches/wda-16.14.0-loopback.patch`. Its SHA-256 is fixed in `server/wda_setup.py`. A build receipt must include the same hash; a receipt from an older, unpatched build cannot start the runner.

## Compatibility

This contribution requires a new WDA build. Setup will not reuse a reachable WDA service unless its status reports the expected loopback policy for both listeners. It preserves an external service rather than stopping it. Direct connections over Wi-Fi or IPv6 are not supported by this policy; use the USB forward.

## Listener enforcement

- `FBConfiguration.bindingIPAddress` returns the literal IPv4 loopback address.
- The MJPEG socket now uses the same configuration as HTTP.
- The shared `FBTCPSocket` transport rejects every explicit interface other than `127.0.0.1`. A missing interface also uses `127.0.0.1`.
- It uses a Network.framework local endpoint with that literal address, so it cannot become an IPv6 wildcard listener.
- Before passing any accepted connection to HTTP or MJPEG, it checks the effective local endpoint from the actual connection path. A missing or non-loopback address causes cancellation.
- Setup sets `USE_IP`, `TEST_RUNNER_USE_IP`, and the Xcode `USE_IP` setting to `127.0.0.1`. These variables provide a consistent launch configuration. The compiled transport restriction remains effective if Xcode drops a variable.

There is no environment variable, tool parameter, or app setting that enables LAN binding.

## Verification

Run the setup regression tests and the native transport test:

```sh
python3 -m unittest discover -s tests -p 'test_setup.py' -v
IPHONE_USE_WDA_SOURCE="$HOME/.local/share/iphone-use/runtime/WebDriverAgent" python3 -m unittest discover -s tests -p 'test_network_hardening.py' -v
```

The native test uses a temporary copy of the pinned source and a random Mac loopback port. It compiles the patched transport with Apple's Network.framework, exchanges bytes through a real IPv4 connection, checks the effective local endpoint, rejects IPv6 `::1`, and rejects wildcard/LAN interface parameters. It does not start Xcode or use a phone. Without the opt-in source path, only this native test is skipped; the other security tests still run.

For a phone installation:

1. Stop only the plugin-owned old start job. Preserve external WDA processes.
2. Run `pua_setup(action="build")`, poll its job until successful, and run `pua_setup(action="start")` once. Poll that same start job until its service is ready.
3. Run `pua_ready`. Open the phone preview so the USB MJPEG connection is also established.
4. Read setup status. Require `service.network_hardened=true`. In `service.wda.iphoneUseNetwork.listeners`, require ports `8100` and `9100` (or the configured HTTP port), `listenerReady=true`, `bindAddress="127.0.0.1"`, and `ipVersion=4`. After both USB connections have been used, each listener must also report `observedLocalAddress="127.0.0.1"` and `observedLocalPort` equal to its listener port.
5. With USB still working, probe the phone's actual LAN IPv4 and available LAN IPv6 addresses from another host. Both HTTP and MJPEG connections must fail. Record the route, tested address, time, and result. Do not claim a failed probe proves the fix if the same probe also failed before the change because of network isolation or a firewall.

The status evidence distinguishes a listener that reached Network.framework's ready state from an effective local address observed on an accepted connection. It is not a device-wide socket inventory. LAN probes and USB functionality are separate checks.

This change does not add caller authentication to the Mac's local USB forward, and does not authorize screenshot retention or remote data transfer. Those are separate controls.
