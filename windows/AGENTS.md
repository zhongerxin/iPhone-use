# Windows adapter development

- Read README.md and STATUS.md before changing this adapter.
- Preserve the upstream macOS code and its license. Windows imports shared modules from the repository root server directory.
- Keep private device identifiers, signing material, credentials, screenshots, raw logs and local configuration under ignored runtime/.
- Never commit Python environments, downloaded/signed apps or compiled executables.
- Signing, verification codes, trust prompts and unlocking are handled by the device owner. Do not inspect other apps' account caches, revoke certificates or uninstall existing apps.
- Use parameterized toolchain paths; do not embed a contributor's home directory.
- Distinguish simulated protocol checks from physical-device checks. Update STATUS.md with actual results and limitations.
- Do not change an installed app's bundle ID or signed Info.plist to accommodate a code cleanup. Existing local installations stay separate from this clean source checkout.
