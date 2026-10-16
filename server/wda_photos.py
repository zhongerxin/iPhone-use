"""Host-side photo validation, request journal, and WDA import client."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from wda_client import WDAError


MAX_PHOTO_BYTES = 20 * 1024 * 1024
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{6,126}[A-Za-z0-9]")
STATUSES = frozenset({"waiting_for_authorization", "processing", "succeeded", "failed", "uncertain"})
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "uncertain"})


def validate_photo(path: str) -> dict[str, Any]:
    try:
        source = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WDAError("photo_not_found", "Photo path does not resolve to a readable file.") from exc
    try:
        stat = source.stat()
        if not source.is_file() or not os.access(source, os.R_OK):
            raise PermissionError(source)
        if stat.st_size > MAX_PHOTO_BYTES:
            raise WDAError("photo_too_large", "Photo exceeds the 20 MiB limit.")
        data = source.read_bytes()
    except OSError as exc:
        raise WDAError("photo_unreadable", "Photo could not be read from the Mac filesystem.") from exc
    actual = image_format(data)
    if actual is None:
        raise WDAError("invalid_photo", "Only actual JPEG or PNG images are supported.")
    dimensions = image_dimensions(actual, data)
    if dimensions is None:
        raise WDAError("invalid_photo", "Image dimensions could not be read.")
    if len(data) > MAX_PHOTO_BYTES:
        raise WDAError("photo_too_large", "Photo exceeds the 20 MiB limit.")
    return {
        "path": str(source),
        "format": actual,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "width": dimensions[0],
        "height": dimensions[1],
    }


def image_format(data: bytes) -> str | None:
    # Validate structure actually used by the dimension parser instead of trusting extensions.
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return "png"
    if len(data) >= 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
        return "jpeg"
    return None


def image_dimensions(kind: str, data: bytes) -> tuple[int, int] | None:
    try:
        if kind == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
            width = int.from_bytes(data[16:20], "big")
            height = int.from_bytes(data[20:24], "big")
            return width, height
        if kind == "jpeg":
            offset = 2
            while offset + 9 < len(data):
                if data[offset] != 0xFF:
                    offset += 1
                    continue
                marker = data[offset + 1]
                offset += 2
                if marker in {0xD8, 0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD9}:
                    continue
                if offset + 2 > len(data):
                    break
                length = int.from_bytes(data[offset:offset + 2], "big")
                if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
                    if offset + 7 <= len(data):
                        height = int.from_bytes(data[offset + 3:offset + 5], "big")
                        width = int.from_bytes(data[offset + 5:offset + 7], "big")
                        return width, height
                offset += length
    except (IndexError, ValueError):
        pass
    return None


class PhotoImportManager:
    """Persist import intents before transport, as HTTP alone cannot provide once-only semantics."""

    def __init__(self, state_dir: Path, client):
        self.state_dir = Path(state_dir).resolve()
        self.requests_dir = self.state_dir / "photo-imports"
        self.requests_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.requests_dir.chmod(0o700)
        self.client = client

    @staticmethod
    def validate_request_id(request_id: str) -> str:
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise WDAError("invalid_request_id", "request_id must be 8-128 characters from letters, digits, dot, colon, underscore, or hyphen, and start/end with a letter or digit.")
        return request_id

    def path_for(self, request_id: str) -> Path:
        request_id = self.validate_request_id(request_id)
        if request_id in {".", ".."} or "/" in request_id or "\\" in request_id:
            raise WDAError("invalid_request_id", "request_id cannot be a path segment.")
        return self.requests_dir / (request_id + ".json")

    def read(self, request_id: str) -> dict[str, Any]:
        path = self.path_for(request_id)
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise WDAError("photo_import_not_found", "No local record exists for this request_id.") from exc
        if not isinstance(value, dict):
            raise WDAError("photo_import_corrupt", "The local import record is invalid; preserve it and inspect manually.")
        return value

    def write(self, value: dict[str, Any]) -> None:
        path = self.path_for(value["request_id"])
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
                stream.write("\n")
            os.replace(temp, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()

    def prepare(self, request_id: str, photo: dict[str, Any]) -> dict[str, Any]:
        existing = None
        path = self.path_for(request_id)
        if path.exists():
            existing = self.read(request_id)
            if existing.get("photo_sha256") != photo["sha256"]:
                raise WDAError("photo_import_conflict", "This request_id was already used for a different photo.")
            return existing
        record = {
            "request_id": request_id,
            "status": "processing",
            "created_at": time.time(),
            "updated_at": time.time(),
            "photo_format": photo["format"],
            "photo_size_bytes": photo["size_bytes"],
            "photo_sha256": photo["sha256"],
            "width": photo["width"],
            "height": photo["height"],
            "remote_status": None,
            "asset_id": None,
            "error": None,
        }
        self.write(record)
        return record

    def mark(self, request_id: str, **changes: Any) -> dict[str, Any]:
        record = self.read(request_id)
        status = changes.get("status", record.get("status"))
        if status not in STATUSES:
            raise ValueError("invalid photo import status")
        # Once transport has started, a timeout or lost receipt cannot be replayed safely.
        if (record.get("transport_started") and status not in TERMINAL_STATUSES
                and "transport_started" not in changes):
            status = "uncertain"
        record.update(changes, status=status, updated_at=time.time())
        self.write(record)
        return record

    @staticmethod
    def _payload(photo: dict[str, Any], request_id: str, data: bytes) -> dict[str, Any]:
        import base64
        return {
            "requestId": request_id,
            "format": photo["format"],
            "sha256": photo["sha256"],
            "sizeBytes": photo["size_bytes"],
            "width": photo["width"],
            "height": photo["height"],
            "data": base64.b64encode(data).decode("ascii"),
        }

    def upload(self, path: str, request_id: str) -> dict[str, Any]:
        request_id = self.validate_request_id(request_id)
        photo = validate_photo(path)
        record = self.prepare(request_id, photo)
        if record.get("status") in TERMINAL_STATUSES and record.get("status") != "uncertain":
            return record
        # If a previous call may have delivered the request, only query. A query
        # miss after transport start is still uncertain, never a fresh upload.
        if record.get("transport_started"):
            try:
                return self.status(request_id, mark_uncertain_on_miss=False)
            except WDAError as exc:
                if exc.code not in {"photo_import_not_found", "unknown command"}:
                    raise
                return self.mark(request_id, status="uncertain", error="Remote import record is unavailable after a prior upload attempt.")
        data = Path(photo["path"]).read_bytes()
        self.mark(request_id, transport_started=True)
        try:
            response = self.client.request("POST", "/wda/photos/import", self._payload(photo, request_id, data), timeout=90)
        except WDAError as exc:
            if exc.uncertain or exc.code in {"action_uncertain", "pua_timeout", "pua_unreachable", "pua_transport_error"}:
                return self.mark(request_id, status="uncertain", error=exc.as_dict())
            return self.mark(request_id, status="failed", error=exc.as_dict())
        value = response.get("value") if isinstance(response, dict) else None
        if not isinstance(value, dict):
            return self.mark(request_id, status="uncertain", error="WDA returned an invalid import response.")
        status = value.get("status")
        if status not in STATUSES:
            return self.mark(request_id, status="uncertain", error="WDA returned an unknown import status.")
        return self.mark(
            request_id,
            remote_status=status,
            asset_id=value.get("assetId") if isinstance(value.get("assetId"), str) else None,
            status=status,
            error=value.get("error") if isinstance(value.get("error"), str) else None,
        )

    def status(self, request_id: str, mark_uncertain_on_miss: bool = True) -> dict[str, Any]:
        local = self.read(request_id)
        try:
            response = self.client.request("GET", f"/wda/photos/import/{request_id}", timeout=15)
        except WDAError as exc:
            if exc.code == "unknown command" and not local.get("transport_started"):
                return self.mark(request_id, status="failed", error={"code": "wda_upgrade_required"})
            if exc.code == "photo_import_not_found":
                if not local.get("transport_started"):
                    return self.mark(request_id, status="failed", error={"code": "photo_import_not_found"})
                if mark_uncertain_on_miss:
                    return self.mark(request_id, status="uncertain", error={"code": "remote_record_missing"})
            raise
        value = response.get("value") if isinstance(response, dict) else None
        if not isinstance(value, dict) or value.get("status") not in STATUSES:
            return self.mark(request_id, status="uncertain", error="WDA returned an invalid status response.")
        return self.mark(
            request_id,
            remote_status=value["status"],
            asset_id=value.get("assetId") if isinstance(value.get("assetId"), str) else None,
            status=value["status"],
            error=value.get("error") if isinstance(value.get("error"), str) else None,
        )
