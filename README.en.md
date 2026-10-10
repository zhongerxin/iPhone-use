# iPhone Use

> Windows users: this fork includes an experimental [Windows USB + MCP adapter](windows/README.md). The original macOS plugin remains at the repository root.

[中文](README.md) · **English**

![iPhone Use controlling a real iPhone in Codex with a live screen preview](assets/iphone-use-demo.png)

Let Codex operate your real iPhone over USB. Describe a task in natural language: open apps, read screens, tap, scroll, enter text, collect lists, and watch the phone in a sidebar widget.

The plugin connects to [WebDriverAgent](https://github.com/appium/WebDriverAgent) (WDA). It includes a local MCP server, setup and usage skills, and a live screen widget. It reuses healthy connections and existing builds; when element targeting fails, it guides the model to inspect a screenshot and try coordinates.

The plugin sends anonymous usage statistics to its own PostHog project by default: starts, tool calls, connection results, durations and error categories. It uses a random installation ID and never uploads phone images, input text or device identifiers. Set `IPHONE_USE_ANALYTICS=0` or `DO_NOT_TRACK=1` and restart the MCP server to disable it. See [analytics documentation](ANALYTICS.md).

**Before using iPhone Use, install, sign, and start WDA Runner on your own iPhone.** WDA is the on-device execution service. The prompt and setup workflow below can help Codex complete the initial installation; a healthy existing WDA can be reused.

## Ask Codex to install it

Paste this prompt into Codex running on your Mac:

```text
Install and configure iPhone Use for me:
https://github.com/zhongerxin/iPhone-use

Read the repository README and installation script first. Check Codex CLI,
Python, Node.js, npm, full Xcode, and the iPhone connected over USB.
Clone the project into a suitable local directory and run sh scripts/install.sh.

If this chat cannot load the new tools, tell me to reconnect or start a new chat
and continue there. Once tools are available, read the iphone-use-setup skill.
Inspect existing configuration and reuse healthy WDA. For initial setup,
discover my device, use my own Apple development team and signable bundle ID,
fetch the pinned WDA, configure signing, build and start it. Continue until
pua_ready returns ready=true, then show the phone screen. Do not reuse the
author's device identifiers or signing configuration.

Help resolve missing dependencies. If Apple account login, device trust,
Developer Mode, or unlocking needs my input, explain the exact steps and wait
for me to complete them before continuing.
```

A reconnect may be required to load newly installed tools. Apple account login, device trust, Developer Mode, and unlocking must be completed by the device owner in the system UI. The repository is currently private; GitHub access or a source package is required.

## Requirements

| Requirement | Purpose |
| --- | --- |
| macOS and Codex desktop / CLI | Local plugin installation, MCP, and the screen sidebar |
| Full Xcode with first-launch setup completed | Build, sign, deploy, and run WDA; Command Line Tools alone are insufficient |
| An Apple account and development team available in Xcode | Sign the on-device WDA Runner |
| A real iPhone connected over USB | Trust the Mac, enable Developer Mode when required, and keep it unlocked during installation / startup |
| Python 3.9+ | Run the MCP server using Python's standard library |
| Node.js 20.19+, 22.12+, or 24+; npm 10+ | USB forwarding and screen streaming; see the project engines and doctor checks |

Xcode must support the phone's iOS version. No jailbreak or separate Appium Server is required. Setup uses a verified, pinned WDA 16.14.0 commit and manages downloads, dependencies, signing, and builds locally.

## Install and start WDA on your own iPhone

The phone-side service comes from [Appium's WebDriverAgent](https://github.com/appium/WebDriverAgent). First-time setup signs and installs `WebDriverAgentRunner` using your own Apple account and development team.

The installation prompt and `iphone-use-setup` workflow fetch the project's pinned WDA version, configure signing, build, deploy, and start it. Codex will explain any steps requiring your input.

For manual Xcode setup:

1. Connect your iPhone over USB, trust the Mac, enable Developer Mode when required, and configure your Apple account in Xcode.
2. Open `WebDriverAgent.xcodeproj`, select the `WebDriverAgentRunner` scheme and your iPhone, and configure your Team and a signable Bundle Identifier in the Runner target's **Signing & Capabilities**.
3. Use **Product → Test** to build, install, and start the runner. Complete any trust prompts on the phone. The WDA service must remain running.

After installing the plugin and configuring USB connectivity, start phone tasks only when `pua_ready` returns `ready=true`. See [Appium's device preparation guide](https://appium.github.io/appium-xcuitest-driver/latest/getting-started/device-setup/) for device and signing requirements.

## Install and connect

```sh
git clone https://github.com/zhongerxin/iPhone-use.git
cd iPhone-use
sh scripts/install.sh
```

The installer validates and stages the source, registers a local Codex marketplace, installs the plugin and skills, and registers the installed server as standard MCP `iphone_use`. Plugin and standard configuration share the same namespace to avoid duplicate tools. An extracted source package uses the same installation command.

Reconnect or start a new chat, enable **iPhone Use**, and ask:

> Use iphone-use-setup to configure my USB-connected iPhone, install and start WDA, verify READY, and show the phone screen.

Setup covers diagnostics, device discovery, signing configuration, download, background build, and startup. Existing configurations and builds are reused. Phone tasks start only after `pua_ready` returns `ready=true`.

## Features

| Feature | Behavior |
| --- | --- |
| App navigation | Launch apps and navigate search, details, settings, or drafts |
| Screen reading | Compact accessibility trees, text, positions, and actual screenshots |
| Taps and gestures | Element or coordinate taps, swipes, drags, and bounded waits |
| Unicode and long text | Complete text input, chunked long-text entry, and continuation |
| Batch workflows | Combine known actions into one call to reduce model round trips |
| List search and collection | Bounded scrolling, overlap, deduplication, and explicit coverage |
| Visual recovery | Inspect a fresh screenshot when targets are occluded, unfocused, or missing from the tree |
| Live preview | An iPhone frame, status, edge effects, and visible tap / drag indicators |
| Connection recovery | Reuse startup jobs, recreate invalid sessions, and reconnect preview with Refresh |

Example prompts:

```text
Open Notes, create a draft with the following text, verify it, and leave it there.

Open the target app, search for this name, and summarize the first few results.

Read this list while scrolling with overlap. Deduplicate entries and state the
covered range and any remaining uncertainty.
```

Text input and submission are separate; text is not submitted by default. The model checks key screens, recipients, counts, and final results. Passwords, verification codes, and Face ID are handled by the user; preview can be paused during handoff.

Repeated preview opens in the same chat reuse the existing widget. Its toolbar offers Refresh, Home, and Screenshot. Without an image, the iPhone frame shows a dark screen with a centered status icon; the top badge displays the connection or pause state. The preview is for the user; model decisions use actual tool observations.

## Technical details

- **Local USB transport:** Python accesses WDA through a loopback-only forwarder; runtime and signing data stay on the Mac.
- **Reuse:** persistent HTTP connections, WDA sessions, healthy services, and builds reduce repeated work.
- **Compact observations and compound actions:** omit duplicate tree fields, skip XML for screenshot-only reads, and reduce turns with batch / scroll-search / list collection.
- **Background setup:** downloads, builds, and startup return queryable job IDs; repeated setup reuses matching active jobs.
- **Screenshot fallback:** inspect the image and multiply pixels by `image.pixel_to_point` to obtain iPhone point coordinates for `pua_tap`.
- **Explicit failure semantics:** a shared operation lock guards concurrent processes; uncertain mutations require reading the actual state before retrying.
- **Preview lifecycle:** streamed frames are not saved to disk; lock-induced pauses and explicit pauses are distinguished, and Refresh can reconnect.

These changes reduce duplicate requests and model round trips. Overall task speed still depends on the app, USB / WDA state, and model response time. Engineering regressions and device acceptance checks are recorded separately in local development notes.

## Tools

There are 17 model tools and 2 additional widget-only tools.

| Tools | Purpose |
| --- | --- |
| `pua_doctor`, `pua_setup`, `pua_ready` | Diagnostics, configuration, background installation / startup, readiness |
| `pua_observe`, `pua_find` | Trees, screenshots, and target queries |
| `pua_apps`, `pua_launch_app` | App identifiers, installation evidence, app launch |
| `pua_tap`, `pua_swipe`, `pua_press_button` | Taps, swipes, and device buttons |
| `pua_type_text`, `pua_wait` | Unicode input and bounded waits |
| `pua_batch`, `pua_scroll_find`, `pua_collect_list` | Compound actions, search, collection |
| `pua_screen`, `pua_metrics` | Preview controls and bounded timing statistics |

Abnormal UI states return a screenshot for the model to inspect before choosing another action. Scroll search performs at most one swipe per call and stops if the target remains unreachable; occlusion, unproven scroll progress, input mismatch, and failed page expectations use the same fallback. Existing screenshots are reused, without automatic extra gestures or action replay.

## Runtime data and updates

New installations use `~/.local/share/iphone-use/`. Set `IPHONE_USE_STATE_DIR` for another external directory; `WDA_STATE_DIR` remains supported. If the new default directory does not exist, an existing older configuration directory is reused so device configuration and builds are retained.

Runtime directories use mode 700 and private configuration / image files use 600. Explicit screenshots have retention limits; preview streams stay in memory. Signing material, device data, logs, and dependency directories are excluded from source packages. `WDA_URL` accepts a local address; the default forwarded port is 18100, and remote addresses are rejected.

```sh
git pull --ff-only
sh scripts/install.sh
```

Reconnect the chat after updating. For connection failures, check USB, unlocking, Developer Mode, signing, and the specific setup error. Query an existing startup job before restarting again.

## Development and documentation

```sh
npm ci --prefix ui --no-audit --no-fund
npm run build --prefix ui
sh scripts/check.sh
python3 scripts/package.py
```

The UI builds into self-contained HTML. Source packages are written to `dist/iphone-use-<version>-source.zip`; normal installation and use do not require rebuilding the UI. Installed plugins retain the complete widget source, styles, build scripts, configuration, dependency lockfile, and widget tests for local maintenance.

See [screen preview and recovery](skills/iphone-use/references/screen.md), [app identifiers](skills/iphone-use/references/apps.md), [troubleshooting](skills/iphone-use-setup/references/troubleshooting.md), and [changelog](CHANGELOG.md).

## Dependencies and acknowledgements

This project relies on the [Appium](https://github.com/appium/appium) ecosystem and [WebDriverAgent](https://github.com/appium/WebDriverAgent). WDA provides the on-device automation service; [appium-ios-device](https://github.com/appium/appium-ios-device) provides USB device communication, port forwarding, and screen-stream connectivity. iPhone Use adds the Codex plugin, MCP tools, setup guidance, and screen widget. A separate Appium Server is not required.

Thanks to the maintainers and contributors of Appium, WebDriverAgent, and related projects for making real iPhone automation possible.

MIT License. See [third-party notices](THIRD_PARTY_NOTICES.md).
