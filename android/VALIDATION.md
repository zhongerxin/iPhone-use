# Android validation

Tested on macOS with a Samsung SM-G9500, Android 9 / API 28, USB, native display 1440×2960. Optional video dependencies: scrcpy 5.0.1 and FFmpeg 9.0.2.

## Real-device acceptance

- Setup, service reuse, READY, Settings launch, Chinese search with exact readback, selectors, batch navigation, four-row collection, swipe-anchor verification and home navigation passed.
- Mixed Chinese, English and emoji passed exact RPC readback. Samsung Settings capped a longer query at 100 characters; `input_mismatch` stopped submission. This is a tested app field limit, not successful unrestricted long-text validation.
- Removing only the test client's ADB forward was recovered by READY using the existing service worker, with no phone action replay.
- Pause cleared cached pixels. Resume restored preview. Owned video processes and forwards were cleaned up on capture shutdown.
- Codex loaded installed 0.1.6 and displayed the native MCP Apps panel. Xiaohongshu launch and an upward swipe were verified; panel screenshots showed the changed feed.
- Eight native-panel samples across 17.6 seconds stayed Live. Producer sequence advanced from 1181 to 1311; sampled capture age was 60–863 ms. These are freshness observations, not end-to-end latency or rendered FPS measurements.

## Transport regression evidence

- A long-lived `adb shell` child inherited MCP stdin and consumed JSON-RPC requests. The same official MCP SDK probe timed out before the fix and passed after assigning the child `DEVNULL` stdin. A hardware-free child-process regression preserves the parent's pending request bytes.
- A 30-second real-phone SDK run completed 585 calls and 237 frames (p95 RPC 1 ms, max 2 ms). This measures local MCP responses, not display latency.
- Static video can stop producing changed frames. A fresh UI Automator screenshot after one idle second proves current pixels; lock checks run before and after capture. A separate 30-second static-screen run had zero unavailable responses after its first frame.
- Browser development preview uses the production widget and official MCP client/stdio server through a loopback adapter. It is source-only and is excluded from both installed plugins.

## Automated checks

PR preparation passed 434 Python tests (including 31 Android tests), 37 widget tests, TypeScript/build checks and both package validators. Android package tests start its isolated stdio runtime without iPhone service modules and read its widget resource. All six extracted protocol helpers retain the original function ASTs. Device-switch cleanup and service-owner handoff have regression coverage.

## Limits

Physical rotation, fold/unfold, wireless pairing, other manufacturers, custom editors and long-duration stability remain unverified. Widget tests cover arbitrary frame proportions and stale gesture geometry. Tests run without real hardware; `scripts/smoke_android.py --exercise-settings` explicitly opts into phone navigation.
