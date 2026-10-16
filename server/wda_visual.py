"""Local Apple Vision OCR with numbered annotation for visual element discovery."""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from wda_client import WDAError

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
OCR_BINARY = PLUGIN_ROOT / "server" / "ocr_tool"
SWIFT_SOURCE = PLUGIN_ROOT / "tools" / "ocr_vision.swift"


def ocr_available() -> bool:
    """Check macOS + compiled Vision OCR binary."""
    import platform as _platform
    if _platform.system() != "Darwin":
        return False
    return OCR_BINARY.is_file() and os.access(OCR_BINARY, os.X_OK)


def compile_ocr_binary() -> bool:
    """Build the Swift OCR tool on macOS; returns True on success."""
    if platform.system() != "darwin" or not shutil.which("swiftc"):
        return False
    if not SWIFT_SOURCE.is_file():
        return False
    OCR_BINARY.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["swiftc", "-O", str(SWIFT_SOURCE), "-o", str(OCR_BINARY),
         "-framework", "Vision", "-framework", "AppKit"],
        capture_output=True, text=True, timeout=60, check=False,
    )
    return result.returncode == 0 and OCR_BINARY.is_file()


def _run_ocr(image_path: Path) -> dict:
    if not ocr_available():
        raise WDAError("ocr_unavailable",
                       "Vision OCR binary is not available. Run install on macOS to compile it.")
    result = subprocess.run(
        [str(OCR_BINARY), str(image_path)],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if result.returncode != 0:
        raise WDAError("ocr_failed", f"Vision OCR exited with code {result.returncode}.")
    try:
        return json.loads(result.stdout)
    except (ValueError, KeyError) as exc:
        raise WDAError("ocr_invalid", "Vision OCR returned invalid JSON.") from exc


def _normalize_ocr_items(document: dict) -> list[dict]:
    """Fix base64-wrapped text fields from Swift serialization."""
    items = []
    for raw in document.get("items", []):
        text = raw.get("text", "")
        if isinstance(text, str) and re.fullmatch(r'[A-Za-z0-9+/=]+', text):
            try:
                decoded = base64.b64decode(text).decode()
                # Swift may double-wrap as JSON array string.
                if decoded.startswith('["') and decoded.endswith('"]'):
                    decoded = decoded[2:-2]
                text = decoded
            except Exception:
                pass
        items.append({
            "text": text,
            "confidence": raw.get("confidence", 0.0),
            "x": raw.get("x", 0.0),
            "y": raw.get("y", 0.0),
            "width": raw.get("width", 0.0),
            "height": raw.get("height", 0.0),
        })
    return items


def annotate_and_observe(screenshot_path: str) -> dict:
    """Run OCR, draw numbered boxes, return annotated image path + structured list."""
    image_path = Path(screenshot_path)
    if not image_path.is_file():
        raise WDAError("screenshot_missing", "Screenshot file not found.")
    document = _run_ocr(image_path)
    items = _normalize_ocr_items(document)
    image_width = document.get("imageWidth", 0)
    image_height = document.get("imageHeight", 0)

    # Draw annotations using PIL.
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(im, "RGBA")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 28)
    except Exception:
        font = ImageFont.load_default()

    for i, item in enumerate(items):
        x, y, w, h = int(item["x"]), int(item["y"]), int(item["width"]), int(item["height"])
        conf = item["confidence"]
        if conf >= 1.0:
            color = (0, 180, 0, 200)
        elif conf >= 0.5:
            color = (200, 160, 0, 200)
        else:
            color = (220, 0, 0, 200)
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        label = str(i)
        bbox = font.getbbox(label)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        badge_y = y - th - 6 if y - th - 6 > 0 else y + h + 4
        draw.rectangle([x, badge_y, x + tw + 10, badge_y + th + 8],
                       fill=(255, 255, 255, 220), outline=color, width=1)
        draw.text((x + 5, badge_y + 4), label, fill=(0, 0, 0), font=font)

    annotated_path = image_path.parent / (image_path.stem + "-annotated.png")
    im.save(str(annotated_path))

    return {
        "image_width": image_width,
        "image_height": image_height,
        "annotated_path": str(annotated_path),
        "blocks": items,
    }


def visual_find(screenshot_path: str, text: str) -> dict:
    """Search OCR results for text; return best match with logical coordinates."""
    image_path = Path(screenshot_path)
    if not image_path.is_file():
        raise WDAError("screenshot_missing", "Screenshot file not found.")
    document = _run_ocr(image_path)
    items = _normalize_ocr_items(document)
    image_width = document.get("imageWidth", 1)
    image_height = document.get("imageHeight", 1)
    query = text.strip().lower()
    if not query:
        raise WDAError("invalid_argument", "Text query cannot be empty.")

    matches = []
    for i, item in enumerate(items):
        block_text = item["text"].strip().lower()
        if query in block_text:
            cx = item["x"] + item["width"] / 2
            cy = item["y"] + item["height"] / 2
            logical_x = cx * 390.0 / image_width
            logical_y = cy * 844.0 / image_height
            matches.append({
                "index": i,
                "text": item["text"],
                "confidence": item["confidence"],
                "x": round(logical_x, 1),
                "y": round(logical_y, 1),
                "pixel_x": round(cx, 0),
                "pixel_y": round(cy, 0),
            })

    if not matches:
        return {"found": False, "query": text,
                "message": f"No OCR block contains '{text}'. Try pua_visual_observe for full page."}

    best = max(matches, key=lambda m: m["confidence"])
    return {
        "found": True,
        "query": text,
        "x": best["x"],
        "y": best["y"],
        "text": best["text"],
        "confidence": best["confidence"],
        "candidates": matches,
        "match_count": len(matches),
    }
