# Browser development preview

This harness uses the real Android widget and the real stdio MCP server through
the official TypeScript MCP client. Only the widget's host adapter is replaced
with a loopback HTTP connection. It is not installed in the Android or iPhone
plugin and does not claim to exercise Codex's MCP Apps host implementation.

With the configured phone connected, unlocked and READY:

```sh
npm --prefix ui ci
node scripts/android-preview/build.mjs /tmp/android-preview.html
node scripts/android-preview/server.mjs ~/.local/share/android-use/venv/bin/python /tmp/android-preview.html
```

Open the printed URL in Codex Browser. Rebuild and refresh the page for UI edits.
For backend changes restart only this development server, optionally specifying
the previous port and token as its third and fourth arguments to retain the URL.
Ctrl-C stops the server and its owned MCP/video processes. Only loopback is bound;
the random path and Host/Origin checks restrict access to the selected local page.

Read-only real-device protocol smoke check (requires the explicitly selected
serial and does not start or recover UI Automator):

```sh
node scripts/android-preview/smoke.mjs ~/.local/share/android-use/venv/bin/python SELECTED_SERIAL 30
```

Metrics report rendered image loads, request round-trip time, current JSON payload
size and capture age. Static screens may produce fewer changed frames. Capture
age is not a measurement of full phone-to-display latency.

The lost-request regression is also covered without hardware by
`tests/test_android_video_stdin.py`: a real child process is offered a pending MCP
input pipe and must see EOF while the parent retains the entire request.
