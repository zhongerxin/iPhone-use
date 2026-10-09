"""Verified app identifiers from the selected device and Apple's public catalog.

Only matching app metadata leaves this component. The full installed-app list is
kept in a private, short-lived local cache and is never sent to Apple.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request


CATALOG_PATH = Path(__file__).resolve().parent.parent / "skills/iphone-use/references/apps.json"
INSTALLED_TTL = 300
APPLE_TTL = 900
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _normalise(value):
    return " ".join(str(value).casefold().split())


def _read(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=".apps-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _valid_bundle(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", value))


def _api_url(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "itunes.apple.com"
            and not parsed.username and not parsed.password and parsed.port in (None, 443)
            and parsed.path in ("/search", "/lookup") and not parsed.fragment)


class _AppleRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _api_url(newurl):
            raise ValueError("Apple catalog redirected outside the allowed HTTPS API.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_apple(url):
    """Fetch JSON only from the fixed Apple HTTPS endpoints, with a 5s timeout."""
    if not _api_url(url):
        raise ValueError("Only https://itunes.apple.com/search or /lookup is allowed.")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "iPhone-use/0.3"})
    with urllib.request.build_opener(_AppleRedirects()).open(request, timeout=5) as response:
        if not _api_url(response.geturl()):
            raise ValueError("Apple catalog returned an unexpected response URL.")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Apple catalog response exceeded the size limit.")
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("results"), list):
        raise ValueError("Apple catalog returned an unexpected JSON shape.")
    return document


def parse_apple(document, country, source_url, verified_at=None):
    """Keep identifiers and publisher evidence, without promotional assets."""
    rows = []
    seen = set()
    for app in document.get("results", []):
        if not isinstance(app, dict) or app.get("kind") not in (None, "software"):
            continue
        bundle = app.get("bundleId")
        track_id = app.get("trackId")
        name = app.get("trackName")
        if not _valid_bundle(bundle) or not isinstance(name, str) or not name.strip():
            continue
        if isinstance(track_id, bool) or not isinstance(track_id, int) or track_id <= 0:
            continue
        if bundle in seen:
            continue
        seen.add(bundle)
        rows.append({"name": name[:200], "aliases": [], "bundle_id": bundle,
                     "track_id": track_id, "store_name": name[:200], "publisher": str(app.get("artistName", ""))[:200],
                     "country": country, "store_url": f"https://apps.apple.com/{country}/app/id{track_id}",
                     "source_url": source_url, "verified_at": verified_at or _now(),
                     "origin": "apple", "installed_verified": False, "installation_checked": False})
    return rows


class AppCatalog:
    def __init__(self, state_dir: Path, setup_manager):
        self.state_dir = Path(state_dir).expanduser().resolve()
        if any((ancestor / ".git").exists() for ancestor in (self.state_dir, *self.state_dir.parents)):
            raise ValueError("App inventory/cache state must stay outside Git checkouts.")
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.setup_manager = setup_manager

    @staticmethod
    def _installed_apps(document):
        result = document.get("result") if isinstance(document, dict) else None
        apps = result.get("apps", result.get("installedApps")) if isinstance(result, dict) else None
        return apps if isinstance(apps, (list, dict)) else None

    @staticmethod
    def parse_installed(document):
        apps = AppCatalog._installed_apps(document)
        if isinstance(apps, dict):
            apps = [{"bundleIdentifier": key, **value} for key, value in apps.items() if isinstance(value, dict)]
        rows, seen = [], set()
        for app in apps if isinstance(apps, list) else []:
            if not isinstance(app, dict):
                continue
            bundle = app.get("bundleIdentifier", app.get("bundleId", app.get("bundleID")))
            name = app.get("name", app.get("displayName", bundle))
            if not _valid_bundle(bundle) or bundle in seen or not isinstance(name, str):
                continue
            seen.add(bundle)
            rows.append({"name": name[:200], "bundle_id": bundle, "aliases": [],
                         "origin": "installed", "installed_verified": True, "installation_checked": True})
        return rows

    def _catalog(self):
        document = _read(CATALOG_PATH, {})
        rows = []
        for app in document.get("apps", []) if isinstance(document, dict) else []:
            if not isinstance(app, dict) or not _valid_bundle(app.get("bundleId")):
                continue
            rows.append({"name": app["name"], "aliases": app.get("aliases", []), "bundle_id": app["bundleId"],
                         "track_id": app.get("trackId"), "store_name": app.get("storeName", app["name"]),
                         "publisher": app.get("publisher"), "country": app.get("country"),
                         "store_url": app.get("storeUrl"), "source_url": app.get("sourceUrl"),
                         "verified_at": app.get("verifiedAt"), "origin": "catalog",
                         "installed_verified": False, "installation_checked": False})
        return rows

    def _installed(self):
        config = self.setup_manager.config
        udid = config.get("udid") if isinstance(config, dict) else None
        if not isinstance(udid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", udid):
            return [], False, "No selected device. Use pua_setup discover/configure, then retry source=installed."
        device_key = hashlib.sha256(udid.encode()).hexdigest()
        cache_path = self.state_dir / "apps-installed-cache.json"
        cache = _read(cache_path, {})
        if (isinstance(cache, dict) and cache.get("device_key") == device_key
                and isinstance(cache.get("fetched_at"), (int, float))
                and 0 <= time.time() - cache["fetched_at"] < INSTALLED_TTL
                and isinstance(cache.get("apps"), list)):
            return cache["apps"], True, None
        fd, name = tempfile.mkstemp(prefix="apps-device-", suffix=".json", dir=self.state_dir)
        os.close(fd)
        try:
            # --include-all-apps is essential: default devicectl lists developer apps only.
            result = subprocess.run(["xcrun", "devicectl", "device", "info", "apps", "--device", udid,
                                     "--include-all-apps", "--json-output", name, "--timeout", "8"],
                                    text=True, capture_output=True, timeout=10, check=False)
            document = _read(name, {})
            if result.returncode != 0 or self._installed_apps(document) is None:
                return [], False, "Could not list selected-device apps. Unlock/connect the iPhone, verify Xcode pairing and retry; catalog candidates are not installation proof."
            rows = self.parse_installed(document)
            for row in rows:
                row["verified_at"] = _now()
            _write(cache_path, {"device_key": device_key, "fetched_at": time.time(), "apps": rows})
            return rows, True, None
        except (OSError, subprocess.TimeoutExpired):
            return [], False, "Installed-app query unavailable or timed out. Check Xcode/devicectl and device connection."
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)

    def _apple(self, query, country, limit):
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": query, "country": country, "media": "software", "entity": "software", "limit": limit})
        key = hashlib.sha256(url.encode()).hexdigest()
        cache_path = self.state_dir / "apps-apple-cache.json"
        cache = _read(cache_path, {})
        if not isinstance(cache, dict):
            cache = {}
        entry = cache.get(key, {})
        if (isinstance(entry, dict) and isinstance(entry.get("fetched_at"), (int, float))
                and 0 <= time.time() - entry["fetched_at"] < APPLE_TTL and isinstance(entry.get("apps"), list)):
            return entry["apps"], None
        # The official API recommends approximately 20 requests/minute; cap at 18.
        lock_path = self.state_dir / ".apps-apple.lock"
        with open(lock_path, "a") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            rate_path = self.state_dir / "apps-apple-request-times.json"
            times = _read(rate_path, [])
            times = [stamp for stamp in times if isinstance(stamp, (int, float)) and 0 <= time.time() - stamp < 60] if isinstance(times, list) else []
            if len(times) >= 18:
                return [], "Apple catalog request budget reached. Use source=catalog/installed or retry after one minute."
            _write(rate_path, [*times, time.time()])
        try:
            rows = parse_apple(fetch_apple(url), country, url)
            cache = {cache_key: value for cache_key, value in cache.items()
                     if isinstance(value, dict) and isinstance(value.get("fetched_at"), (int, float))
                     and 0 <= time.time() - value["fetched_at"] < APPLE_TTL}
            cache[key] = {"fetched_at": time.time(), "apps": rows}
            _write(cache_path, cache)
            return rows, None
        except (OSError, ValueError, urllib.error.URLError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code in (403, 429):
                return [], "Apple catalog rejected/throttled the request. Do not retry repeatedly; use local catalog/installed discovery."
            return [], "Apple catalog request failed or timed out. Try source=catalog/installed or another storefront; do not guess bundle IDs."

    @staticmethod
    def _score(row, query):
        target = _normalise(query)
        values = [_normalise(value) for value in (row.get("name", ""), row.get("store_name", ""), row["bundle_id"], *row.get("aliases", []))]
        if target in values:
            return 2
        return 1 if any(target in value for value in values if value) else 0

    def lookup(self, query: str, country="cn", source="auto", limit=10):
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 100
                or not isinstance(country, str) or not re.fullmatch(r"[A-Za-z]{2}", country)
                or not isinstance(source, str) or source not in {"auto", "catalog", "installed", "apple"}
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 30):
            return {"ok": False, "error": {"code": "invalid_argument", "message": "query needs 1–100 characters, country two letters, source auto/catalog/installed/apple, and limit 1–30.", "uncertain": False}}
        query, country = query.strip(), country.lower()
        catalog = self._catalog()
        warnings, searched, candidates = [], [], []
        installed, installation_checked = [], False
        if source in {"auto", "installed"}:
            installed, installation_checked, error = self._installed()
            searched.append("installed")
            if error:
                warnings.append(error)
            aliases = {row["bundle_id"]: row for row in catalog}
            for row in installed:
                row = dict(row)
                reference = aliases.get(row["bundle_id"], {})
                row["aliases"] = [*reference.get("aliases", []), reference.get("name", "")]
                if self._score(row, query):
                    candidates.append(row)
        if source in {"auto", "catalog"}:
            searched.append("catalog")
            candidates.extend(row for row in catalog if self._score(row, query))
        apple_error=None
        if source == "apple" or (source == "auto" and not candidates):
            searched.append("apple")
            apple, apple_error = self._apple(query, country, limit)
            candidates.extend(apple)
            if apple_error:
                warnings.append(apple_error)
        installed_ids = {row["bundle_id"] for row in installed}
        merged = {}
        for row in candidates:
            row = dict(row)
            bundle = row["bundle_id"]
            row["installation_checked"] = installation_checked
            row["installed_verified"] = installation_checked and bundle in installed_ids
            if bundle not in merged:
                merged[bundle] = row
            else:
                for key, value in row.items():
                    if key not in merged[bundle] or merged[bundle][key] in (None, "", []):
                        merged[bundle][key] = value
        ranked = sorted(merged.values(), key=lambda row: (-self._score(row, query), -int(row["installed_verified"]), row["name"], row["bundle_id"]))
        # An explicit alias such as CMB must not include unrelated substring matches.
        exact = [row for row in ranked if self._score(row, query) == 2]
        if exact:
            ranked = exact
        ok=not (source == "installed" and not installation_checked) and not (source in {"apple", "auto"} and apple_error is not None)
        result={"ok": ok,
                "query": query, "country": country, "source": source, "candidates": ranked[:limit],
                "total_matches": len(ranked), "searched_sources": searched, "warnings": warnings,
                "next_action": "Choose the matching app/publisher, then pua_launch_app with its bundle_id and verify foreground. App Store/catalog metadata alone does not prove installation."}
        if not ok:
            result["error"]={"code":"app_lookup_unavailable","message":" ".join(warnings) or "App lookup could not be completed.","uncertain":False}
        return result
