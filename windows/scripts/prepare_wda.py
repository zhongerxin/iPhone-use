"""Copy official unsigned WDA to a dedicated working bundle before signing."""
import json
from pathlib import Path
import plistlib
import shutil

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / "downloads/wda-v16.14.0/WebDriverAgentRunner-Runner.app"
target = ROOT / "runtime/prepared/WebDriverAgentRunner-Runner.app"
if target.exists():
    raise SystemExit("Prepared bundle already exists; preserve it and explicitly choose a fresh directory before preparing again.")
if not (source / "PlugIns/WebDriverAgentRunner.xctest/WebDriverAgentRunner").is_file():
    raise SystemExit("Official WDA source is missing its test executable")
if (source / "embedded.mobileprovision").exists():
    raise SystemExit("Expected an unsigned official WDA source")
shutil.copytree(source, target)
info_path = target / "Info.plist"
with info_path.open("rb") as stream:
    info = plistlib.load(stream)
info["CFBundleIdentifier"] = "com.iphoneuse.windows.wda"
info["CFBundleDisplayName"] = "Windows iPhone Control"
with info_path.open("wb") as stream:
    plistlib.dump(info, stream, fmt=plistlib.FMT_BINARY)
# Symbols are build artifacts, not runnable plugins; retain original downloads.
symbols = target / "PlugIns/WebDriverAgentRunner.xctest.dSYM"
if symbols.exists():
    shutil.rmtree(symbols)
print(json.dumps({"prepared": str(target), "bundle_id": info["CFBundleIdentifier"]}))
