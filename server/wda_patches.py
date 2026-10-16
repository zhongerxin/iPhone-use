"""Reproducible patches applied to the pinned upstream WebDriverAgent checkout."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parent.parent
PATCH = PLUGIN_ROOT / "server/native/photos-import.patch"
MAX_HTTP_BODY_BYTES = 20 * 1024 * 1024 + 1024


def patch_bytes() -> bytes:
    return PATCH.read_bytes()


def patch_fingerprint() -> str:
    return hashlib.sha256(patch_bytes()).hexdigest()


PATCH_FINGERPRINT = patch_fingerprint()


def apply(source_dir: Path) -> bool:
    """Return whether this call installed the patch; false means already applied."""
    source_dir = Path(source_dir).expanduser().resolve()
    probe = subprocess.run(
        ["git", "-C", str(source_dir), "apply", "--check", "--reverse", str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return False
    subprocess.run(
        ["git", "-C", str(source_dir), "apply", "--check", str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(source_dir), "apply", str(PATCH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return True
