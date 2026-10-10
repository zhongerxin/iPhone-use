"""Explicit Japanese presentation; unset/unsupported locales retain upstream text."""
import json
import os
from pathlib import Path

LANGUAGE = "ja" if os.environ.get("IPHONE_USE_LANGUAGE", "").strip().lower().replace("_", "-").split("-")[0] == "ja" else "default"
_catalog = json.loads((Path(__file__).resolve().parents[1] / "locales/ja/mcp.json").read_text()) if LANGUAGE == "ja" else {}


def localize(value):
    if LANGUAGE != "ja":
        return value
    if isinstance(value, dict):
        return {key: localize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [localize(item) for item in value]
    if isinstance(value, str):
        return _catalog.get("strings", {}).get(value, value)
    return value


def instructions(default):
    return _catalog.get("instructions", default)
