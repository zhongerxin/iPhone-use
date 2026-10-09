# Android Use

Android companion plugin for this repository. Uses ADB for device connections and UI Automator for native UI actions; shares the existing MCP response utilities and screen widget. iPhone registration and settings remain independent.

## Install on macOS

From the repository root:

```sh
sh scripts/install_android.sh
```

This creates an isolated Python environment under `~/.local/share/android-use`, installs pinned UI Automator dependencies, downloads official Google Platform Tools if needed, stages the plugin, and registers `android_use` in Codex. Reconnect the chat to load the new tools, skills and native screen panel.

Enable USB debugging on your phone and accept the computer authorization. Run `pua_setup(action="status")`, select a serial with `configure` if needed, then `start`. Poll a returned `job_id` using `status`; after startup call `pua_ready`. Setup can deploy the bundled `u2.jar`; no root, Xcode or Android Studio is needed.

Runtime data, configuration and screenshots stay outside the repository. Android Use does not instantiate the iPhone analytics client or send usage events. `ANDROID_USE_HOME`, `ANDROID_USE_STATE_DIR` and `ANDROID_USE_ADB` provide path overrides.

## Tools

19 tools: doctor, setup, ready, observe, find, tap, swipe, type_text, press_button, launch_app, wait, scroll_find, collect_list, apps, batch, metrics, screen, screen_frame and screen_action. The last two are widget-only. Tool names retain `pua_`, in the independent `android_use` namespace.

Selectors use `text`, `text_contains`, `resource_id`, `content_desc`, `class_name`, `package`, boolean states and optional `index`. Action coordinates use display pixels. Scaled screenshot results include `pixel_to_display` for coordinate conversion.

All clicks, input and key requests are single-attempt. UI Automator's default action replay wrapper is deliberately bypassed. READY and the refresh toolbar can recover the read-only service channel; `recover=false` disables READY recovery. Recovery never replays an uncertain action. Semantic failures attach a fresh screenshot where possible. Batch prevalidates all steps and stops on errors, unverified submission or its time budget.

`type_text` sets the whole string (up to 10,000 characters) in one request. Unicode JSON escapes avoid Android 9's embedded HTTP charset issue. Exact readback is optional, and mandatory before submit. Empty Android fields can expose their hint as text; strict verification fails conservatively when emptiness cannot be established. Password fields pause the preview and require user takeover. Apps can impose their own input limits: Samsung Settings search truncated a 960-character test to 100 characters; exact readback correctly rejected it. A 26-character Chinese/English/emoji string passed exact RPC readback (the XML tree may sanitize emoji). Generic custom-rendered editors/IME fallback are not implemented.

Setup uses a persistent owned worker because UI Automator's Python library terminates services it started at process exit. Healthy services are reused. `stop` drops local bindings and asks this installation's worker to exit; external services are preserved. A disconnected channel is recovered with `start`, followed by `ready` and a fresh observation.

The native widget uses the same frame/cursor/pause protocol as iPhone Use. With optional scrcpy and FFmpeg dependencies it captures H.264 video and delivers the latest decoded JPEG through MCP; otherwise it falls back to UI Automator JPEG, then ADB PNG. Each frame supplies its actual width and height, so portrait, landscape, tablet and foldable proportions fit the available panel without stretching or cropping. Android uses a neutral frame without fabricated camera cutouts; rotation and fold/unfold update preview geometry even without a new model observation. Capture stops when no widget lease remains. Lock or authentication pauses erase cached pixels.

## Compatibility and current limits

- Verified target: macOS, Samsung SM-G9500, Android 9 / API 28, USB.
- Android 11+ wireless `pair`/`connect` endpoints are implemented but not verified on this Android 9 phone. Legacy unauthenticated `tcpip 5555` is not enabled automatically.
- App lookup verifies installed package IDs, with a small alias map; arbitrary display-name inventory is not yet equivalent to the iPhone catalog.
- Lists use explicit Android row selectors, bounded pages and conservative deduplication. They never claim complete business coverage automatically.
- Rotated screenshots reject mismatched display dimensions; hardware rotation and other manufacturers still need validation.
- Native Codex MCP Apps preview was verified after installation with Xiaohongshu launch, scrolling and continued frame updates.
- Windows support, automatic wireless onboarding, custom editor input, and exhaustive vendor testing remain future work. This is a usable first Android release, not a claim of complete maturity parity.

## Validation

```sh
python3 -m pip install -r android/requirements.txt
sh scripts/check.sh
npm test --prefix ui
# Isolated virtualenv, real device. Does not run in ordinary CI:
~/.local/share/android-use/venv/bin/python scripts/smoke_android.py
# Explicitly exercises Samsung Chinese Settings UI:
~/.local/share/android-use/venv/bin/python scripts/smoke_android.py --exercise-settings
```

CLI fallback before reconnecting Codex:

```sh
sh scripts/android_mcp.sh --tool pua_setup --arguments '{"action":"status"}'
sh scripts/android_mcp.sh --tool pua_ready --arguments '{"screenshot":false}'
```

The CLI returns MCP content, including base64 image blocks for screenshot calls. Consumers should render images or use the provided local image path rather than print image data.

Implementation sources: [Android ADB](https://developer.android.com/tools/adb), [openatx/uiautomator2](https://github.com/openatx/uiautomator2), [OpenAI plugin packaging](https://developers.openai.com/plugins/build/plugins).


## Continuous preview

When `scrcpy` and `ffmpeg` are installed, preview uses scrcpy's device H.264 encoder and a bounded host JPEG decoder. On macOS install the optional video dependencies with `brew install scrcpy ffmpeg`. The server JAR must match the installed scrcpy executable; the plugin discovers Homebrew's paired files and does not bundle either binary. It disables audio and scrcpy input control; UI Automator continues to handle phone actions.

Android widget polling runs at up to 20 requests/second (50 ms), requesting only the latest frame. Video preview is scaled to a maximum 1600-pixel edge; phone operations retain native display coordinates. Actual display rate depends on the phone, decoding and MCP transport. No local HTTP server or network exposure is required by the installed plugin. If video capture is unavailable, preview falls back to UI Automator JPEG, then ADB PNG. Closing the widget expires the capture lease; pause stops capture, and only this stream's processes/forward/JAR are cleaned up. Lock state is checked throughout streaming, and authentication pause discards buffered pixels.

A fresh UI Automator snapshot after one second without video frames keeps static-screen freshness verifiable. The widget stops retrying after repeated transport timeouts until the user refreshes, preventing a stalled host queue from growing. Actual sidebar playback is not guaranteed to reach 30 FPS.


## Recovery

READY starts or reuses bounded service recovery when its read-only observation channel fails. `recover=false` remains diagnostic-only. The refresh toolbar can start recovery too. Neither path replays phone actions, and authentication pauses remain user-controlled. A closed host MCP transport cannot be repaired by a tool running over that same closed transport; host reconnection is distinct from USB/service recovery. Reload the host connection after installing a new plugin version; ordinary phone use does not require restarting Codex.

## Code boundaries

Android transport, actions, setup, video and presentation live in `server/android_*.py`; manifests and skills live under `android/`. `server/phone_protocol.py` shares schema validation, error/result encoding and clipboard handling. Android subclasses the existing `ScreenHub` for leases, pause state and gesture events, replacing its capture loop. Both platforms share the widget; Android-specific geometry and transport behavior are gated by its platform marker.

Android packaging explicitly includes only its runtime and these shared modules. The iPhone installation excludes Android runtime files. The source-only `scripts/android-preview/` harness supports UI/transport development without repeatedly restarting Codex.
