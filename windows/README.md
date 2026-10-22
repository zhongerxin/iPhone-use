# iPhone Use for Windows (experimental)

**普通用户请先阅读 [中文上手教程](QUICKSTART.zh-CN.md)，从 [Releases](https://github.com/jebhi/iPhone-use/releases) 下载带预编译安装器的 ZIP，按 01～04 编号运行。下面主要是源码开发说明。**

Windows USB control of an iPhone using a prebuilt WebDriverAgent runner and a
persistent stdio MCP server. This adapter adds Windows lifecycle and Pillow
screenshot processing while reusing the original iPhone Use control modules.
It does not require the CrossCode desktop application.

The original macOS plugin stays at the repository root. For the prebuilt Windows
release, install Python 3.12 and Apple USB drivers, connect and trust one iPhone,
then run `01-setup.cmd`, `02-install-wda.cmd`, `03-register-mcp.cmd` and
`04-start-wda.cmd` in order. No Rust toolchain is needed for that release.
The remaining setup/build instructions below are for source users.

## Prerequisites

- Windows, Python 3.12, Git and Apple's USB device drivers/services.
- One connected iPhone, device trust, Developer Mode and an unlocked screen.
- An Apple account for signing WDA, or an already signed compatible runner.
- For building the signer: a GNU Windows Rust toolchain, MSYS2 GCC/G++, static
  OpenSSL and libclang. Native dependency setup is still manual.

## Setup

Run from this `windows/` directory:

```powershell
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

`requirements-lock.txt` records the full development environment for
reproducibility; `requirements.txt` pins the direct runtime dependencies.

Obtain the official unsigned WDA v16.14.0 runner from
https://github.com/appium/WebDriverAgent/releases/tag/v16.14.0 and unpack it so
`downloads/wda-v16.14.0/WebDriverAgentRunner-Runner.app` exists. Verify the archive
against the release source. No signed app or signing certificate is included.

```powershell
.venv/Scripts/python.exe scripts/prepare_wda.py
```

This copies the unsigned app to `runtime/prepared/`, using the generic identifier
`com.iphoneuse.windows.wda`. Signing appends the selected developer team suffix.
The preparation script refuses to overwrite an existing prepared bundle.

Build the signer, supplying paths that exist on your computer:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_installer.ps1 -ToolchainBin '<Rust GNU bin>' -NativeBin '<MSYS2 GCC bin>' -OpenSSLDir '<static OpenSSL install>' -LibClangDir '<libclang directory>'
```

The script copies the executable and available GNU runtime DLLs to `bin/`.
Run `install-wda.cmd`; enter account details only in its local dialog. Set
`WDA_ANISETTE_URL=https://ani.sidestore.app` to use the previously verified
SideStore endpoint. Signing depends on the external service and Apple account
limits. No existing certificates are revoked or apps uninstalled.

The Rust directory-install path has previously failed near 80% after signing.
`install-wda.cmd` automatically retries the same signed bundle through
pymobiledevice3 and verifies the installed app, without logging in again. The
underlying Rust transfer failure remains unresolved.

Trust the developer certificate on the phone when requested, then run
`start-wda.cmd`. A started process alone is not proof that WDA is ready.

## Register MCP

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register_mcp.ps1
codex mcp get iphone-use-windows --json
```

Start a new Codex session or reload the extension so the 15 `pua_*` tools load.
The MCP client owns the persistent stdio process; do not launch it as a detached
background application. Local paths live only in the user's Codex configuration.
Registering Codex locally does not configure a hosted ChatGPT MCP connection.

Start a phone task with `pua_ready`. Subsequent operations reuse one WDA session
and HTTP connection. Use `pua_apps` to resolve installed bundle IDs, and
`pua_observe` for fresh UI state. Screenshot pixels multiplied by
`image.pixel_to_point` yield iPhone point coordinates. Batch only already known
steps; uncertain actions are not blindly replayed.

## Checks and limitations

```powershell
.venv/Scripts/python.exe scripts/check_mcp.py
.venv/Scripts/python.exe -m unittest discover -s tests -p test_windows_mcp.py -v
.venv/Scripts/python.exe scripts/test_wda_probe.py
.venv/Scripts/python.exe scripts/check_mcp.py --live
```

`--live` reads the phone UI, screenshots and installed apps; it does not tap or
type. A recovery probe can fail before runner startup; diagnostics report those
startup failures separately from errors after ready. Results and screenshots
remain under ignored `runtime/`.

The development installation verified WDA startup, screenshots, taps, drag,
application launch and Unicode input on a physical device. This clean checkout
uses a new generic bundle ID and has not been re-signed or reinstalled on a phone.
It does not ship a live sidebar widget. USB disconnections can require recovery.
Typing defaults to a draft, but line breaks may activate an app's Send action;
for multiline content, verify that app's behavior first. Only send messages when
the device owner has authorized that conversation.

See [STATUS.md](STATUS.md) for current validation and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream licenses.
