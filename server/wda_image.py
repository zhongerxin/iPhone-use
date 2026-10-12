"""Prepare a WDA screenshot for a model: bounded pixel size, JPEG, exact point scale.

WDA always answers /screenshot with a native-resolution PNG, often several megabytes.
Vision models downscale large images themselves, and each retained screenshot is sent
again with every later request. Scaling once here, to a size the common model limits
accept unchanged, keeps coordinates exact and removes most of the transferred bytes.
"""
import os
import datetime as dt
import uuid
from pathlib import Path
import struct
import subprocess

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Common vision limits: the short side is kept at 768 px and the long side at 1568 px.
MAX_SHORT_SIDE = 768
MAX_LONG_SIDE = 1568
JPEG_QUALITY = 80
SIPS = "/usr/bin/sips"


def png_size(data):
    """Width and height from the PNG header, or None when it is not a complete PNG header."""
    if len(data) < 24 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if 0 < width <= 20000 and 0 < height <= 20000 else None


def fit_size(width, height, max_short=MAX_SHORT_SIDE, max_long=MAX_LONG_SIDE):
    """Largest size no bigger than the source that fits both model limits."""
    short, long_side = sorted((width, height))
    scale = min(1.0, max_short / short, max_long / long_side)
    return max(1, round(width * scale)), max(1, round(height * scale))


def enabled():
    """WDA_IMAGE=original returns the native PNG, for debugging legibility."""
    return os.environ.get("WDA_IMAGE", "").strip().lower() != "original"


def model_image(source, quality=JPEG_QUALITY, timeout=10):
    """Write a downscaled JPEG next to the PNG at `source`.

    Returns (path, mime type, width, height). Any failure returns the original PNG:
    a screenshot is never lost because the local conversion was unavailable.
    """
    source = Path(source)
    size = png_size(source.read_bytes()[:32])
    if size is None:
        return source, "image/png", None, None
    width, height = size
    target = fit_size(width, height)
    # An image already inside the limits stays the lossless original.
    if target == (width, height) or not enabled() or not os.access(SIPS, os.X_OK):
        return source, "image/png", width, height
    destination = source.with_suffix(".jpg")
    try:
        # sips takes the resampled height before the width.
        completed = subprocess.run(
            [SIPS, "-s", "format", "jpeg", "-s", "formatOptions", str(quality),
             "-z", str(target[1]), str(target[0]), str(source), "--out", str(destination)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False)
        if completed.returncode != 0 or not destination.is_file() or destination.stat().st_size < 4:
            raise OSError("sips did not produce an image")
        with destination.open("rb") as stream:
            if stream.read(2) != b"\xff\xd8":
                raise OSError("sips output is not JPEG")
        destination.chmod(0o600)
        return destination, "image/jpeg", target[0], target[1]
    except (OSError, subprocess.SubprocessError):
        try:
            destination.unlink()
        except OSError:
            pass
        return source, "image/png", width, height


def save_capture(state_dir, data):
    """Store native evidence using one retention scheme for PUA and Midscene."""
    artifacts = Path(state_dir) / "artifacts"
    artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = artifacts / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8] + ".png")
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def prune_captures(state_dir):
    artifacts = Path(state_dir) / "artifacts"
    for pattern in ("*.png", "*.jpg"):
        # mtime also ages out midscene-* files left by older plugin versions.
        for old in sorted(artifacts.glob(pattern), key=lambda p: (p.stat().st_mtime_ns, p.name))[:-100]:
            old.unlink(missing_ok=True)
