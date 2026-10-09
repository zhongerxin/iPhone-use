# Android-specific dependencies

Android Use installs these third-party packages into a separate local virtualenv. Their original licenses and notices remain in the distributions:

- openatx/uiautomator2 3.7.0 — MIT. Includes the upstream Android UI Automator server JAR and its upstream dependencies. https://github.com/openatx/uiautomator2
- openatx/adbutils 2.12.0 — MIT. https://github.com/openatx/adbutils
- Pillow 11.3.0 — HPND / Pillow license. https://python-pillow.github.io/
- Google Android SDK Platform Tools — distributed separately by Google under its SDK terms; downloaded from Google's official repository, not included in the plugin archive. https://developer.android.com/tools/releases/platform-tools

The shared widget retains its bundled MCP Apps SDK legal comments. See the bundled SHARED_NOTICES.md for the original widget dependencies.

Optional, separately installed video dependencies (not bundled):
- Genymobile scrcpy — Apache-2.0. https://github.com/Genymobile/scrcpy
- FFmpeg — LGPL/GPL depending on the installed build and enabled codecs. https://ffmpeg.org/legal.html
