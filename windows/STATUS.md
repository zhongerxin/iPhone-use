# Windows adapter status

Prepared 2026-10-10 from development source and iPhone Use upstream revision
`66385cc35076d18ac8bbbbf3b6794362d0c74ca9`.

Before opening the upstream PR, the branch was merged with the latest upstream
main (0.3.8). The stdio check, three Windows MCP tests and four HTTP probe tests
passed again with those updated shared server modules.

- Persistent stdio MCP exposes 15 tools using shared root server modules.
- The prior development setup exercised physical-device startup, screenshots,
  tapping, dragging, app activation and Unicode typing. Device/account IDs and
  private use cases are intentionally absent from this public record.
- This clean source adds a generic bundle identifier and configurable native
  build paths. Signing/installing that new identifier is not yet physically
  verified. The functioning development installation is kept separate.
- The Windows signer still needs manual native prerequisites. A known Rust
  directory-install failure can be retried with pymobiledevice3 after signing.
- Checks on the clean source passed: subprocess MCP initialize/list/ping and
  argument rejection, three simulated Windows MCP transport tests, four HTTP
  probe tests, PowerShell syntax parsing and locked offline Cargo metadata.
  Python checks used an existing development interpreter; the source imports
  were resolved from this checkout. No signing or live device action was run.
- The native signer was not rebuilt in this clean checkout. Configurable build
  paths and the generic signed bundle ID still need a fresh-machine build and
  signing/install check.
- No compiled binaries, signing material, phone captures, logs, environments or
  private development journals are included.
