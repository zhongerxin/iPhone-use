# Windows adapter status

Updated 2026-10-10 for `windows-v0.1.0-alpha.1`, after merging upstream main
(0.3.8). This is an experimental prerelease; fresh-device installation of the
generic bundle identifier has not been physically verified.

- Persistent stdio MCP exposes 15 tools using shared root server modules.
- The prior development setup exercised physical-device startup, screenshots,
  tapping, dragging, app activation and Unicode typing. Device/account IDs and
  private use cases are intentionally absent from this public record.
- This clean source adds a generic bundle identifier and configurable native
  build paths. Signing/installing that new identifier is not yet physically
  verified. The functioning development installation is kept separate.
- The prebuilt release includes the native installer, GNU runtime DLLs and
  dependency licenses. Source builds still need manual native prerequisites.
  The known Rust directory-install failure automatically retries with
  pymobiledevice3 after signing, then checks the installed app record.
- Checks on the clean source passed: subprocess MCP initialize/list/ping and
  argument rejection, three simulated Windows MCP transport tests, four HTTP
  probe tests, PowerShell syntax parsing and locked offline Cargo metadata.
  A new Python 3.12 environment also completed setup and protocol checks with
  pinned dependencies and the hash-verified official unsigned WDA download.
- The generic-ID native signer was rebuilt in this checkout. Its `--version`
  and `--help` are smoke checked without login. The Windows exe is not
  Authenticode signed. No fresh signing or live device action was run.
- The Chinese quickstart and numbered scripts explain the complete user flow.
  Apple account limits, external signing-service availability and device
  trust/developer mode remain installation dependencies.
- Public source and release archives exclude account details, device IDs,
  certificates, signed apps, phone captures, logs, environments and private
  development journals. WDA is downloaded locally during setup.
