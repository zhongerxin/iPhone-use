# Third-party code in the Windows adapter

The repository's original iPhone Use code remains under its upstream MIT license,
copyright 2026 ZHONG XIN. The Windows adapter reuses the root `server/` modules.
`server/protocol.py` copies tool schemas and validators from the pinned upstream
revision `66385cc35076d18ac8bbbbf3b6794362d0c74ca9`; the original notice is retained
in `server/UPSTREAM_LICENSE`.

New Windows adapter code is MIT, copyright 2026 jebhi. This does not relicense
the following dependencies:

| Directory | Version/source | License | Changes |
| --- | --- | --- | --- |
| vendor/isideload | crates.io isideload 0.1.17; https://github.com/nab138/isideload | MPL-2.0 | Only `.appex` bundles are provisioned as app extensions; `.xctest` remains nested signed code. |
| vendor/nab138_icloud_auth-0.1.5 | crates.io nab138_icloud_auth 0.1.5; https://github.com/nab138/apple-private-apis | MPL-2.0 | Authentication protocol corrections from CrossCode's dependency copy; interactive/example credential tests omitted and test targets removed. |
| vendor/nab138_omnisette-0.1.3 | crates.io nab138_omnisette 0.1.3; https://github.com/nab138/apple-private-apis | MPL-2.0 | Provisioning WebSocket closure/error handling and bounded receive timeouts. |

The complete MPL-2.0 license is in `vendor/LICENSE-MPL-2.0.txt`. Modified covered
source is supplied in this repository and remains MPL-2.0. Original source
notices and author metadata are retained. CrossCode (https://github.com/nab138/CrossCode,
MIT, copyright 2025 nab138) informed the authentication/build integration; the
Windows adapter does not bundle its desktop application or account state.

Other dependencies are declared in `requirements*.txt` and `installer/Cargo.lock`;
they are obtained from their upstream package registries with their own notices.
The Apple root certificate in the authentication library is public trust material,
not a user signing certificate. WDA binaries and Apple signing material are not
distributed by this fork.
