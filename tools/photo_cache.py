"""Local photo cache: download, capture-date extraction, normalisation, and image metrics.

The kiosk display re-encodes photos to JPEG without EXIF so the browser renders fast
from local storage. Because the stored JPEG loses EXIF metadata and WebDAV mtime only
reflects Nextcloud upload time rather than when a photo was taken, this cache layer
captures the original capture timestamp and perceptual metrics once per remote change.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import sys
from typing import Any

from PIL import Image, ImageOps
import requests

try:
    import photo_select
except ModuleNotFoundError:
    _select_path = pathlib.Path(__file__).resolve().with_name("photo_select.py")
    _select_spec = importlib.util.spec_from_file_location("photo_select", _select_path)
    if _select_spec is None or _select_spec.loader is None:
        raise
    photo_select = importlib.util.module_from_spec(_select_spec)
    sys.modules["photo_select"] = photo_select
    _select_spec.loader.exec_module(photo_select)

CACHE_VERSION = 2
CACHE_SUBDIR = ".cache"
CACHE_MANIFEST_NAME = ".cache-manifest.json"
CACHE_MAX_LONG_SIDE = 1920              # long side only: no short-side cap, so a 1920x1200 canvas never upscales
CACHE_JPEG_QUALITY = 90                 # high: this copy is the render source, not the display copy

REQUEST_TIMEOUT = (10, 60)
_DOWNLOAD_CHUNK_SIZE = 65536
_EXIF_IFD_TAG = 0x8769
_EXIF_DATETIME_ORIGINAL_TAG = 36867
_EXIF_DATETIME_TAG = 306
_FILENAME_TS_RE = re.compile(r"^[0-9a-f]{10}-(\d{2})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})")


@dataclass(frozen=True)
class CacheEntry:
    rel_path: str
    local_name: str
    etag: str
    mtime: str
    size: int | None
    width: int
    height: int
    captured_at: datetime | None
    luma: float
    sharpness: float
    dhash: int


@dataclass(frozen=True)
class CacheRefresh:
    entries: dict[str, CacheEntry]
    errors: list[dict]
    downloaded: int
    reused: int
    removed: list[str]


def cache_name(rel_path: str) -> str:
    """Return a flat, collision-resistant local JPEG filename for a remote relative path."""
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:10]
    stem = pathlib.PurePosixPath(rel_path).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "photo"
    return f"{digest}-{safe_stem}.jpg"


def _parse_exif_datetime(raw: object) -> datetime | None:
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", errors="ignore")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().rstrip("\x00").strip()
    if not cleaned:
        return None
    try:
        return datetime.strptime(cleaned, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def extract_captured_at(payload: bytes, name_hint: str = "") -> datetime | None:
    """Extract naive local capture time from EXIF first, then phone-style cache filename.

    Never falls back to WebDAV mtime: WebDAV mtime reflects upload time (e.g. a 2018
    photo uploaded in 2025 carries a 2025 mtime), which would corrupt recency scoring
    and On-This-Month anniversary matching. Never raises on corrupt input.
    """
    try:
        with Image.open(io.BytesIO(payload)) as img:
            exif = img.getexif()
            if exif:
                # DateTimeOriginal (36867) lives inside the Exif IFD (0x8769), not top-level IFD0.
                exif_ifd = exif.get_ifd(_EXIF_IFD_TAG)
                dt = _parse_exif_datetime(exif_ifd.get(_EXIF_DATETIME_ORIGINAL_TAG))
                if dt is not None:
                    return dt
                dt = _parse_exif_datetime(exif.get(_EXIF_DATETIME_TAG))
                if dt is not None:
                    return dt
    except Exception:
        pass

    match = _FILENAME_TS_RE.match(name_hint)
    if match:
        yy, mm, dd, hh, mi, ss = (int(part) for part in match.groups())
        try:
            return datetime(2000 + yy, mm, dd, hh, mi, ss)
        except ValueError:
            return None
    return None


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.split()[3])
        return background
    return img.convert("RGB")


def normalise(payload: bytes) -> tuple[Image.Image, int, int]:
    """Decode original bytes, orient upright, flatten alpha on black, and cap long side."""
    with Image.open(io.BytesIO(payload)) as opened:
        img = ImageOps.exif_transpose(opened)
        img = _to_rgb(img)
        img.thumbnail((CACHE_MAX_LONG_SIDE, CACHE_MAX_LONG_SIDE), Image.Resampling.LANCZOS)
        if img is opened:
            img = img.copy()
    width, height = img.size
    return img, width, height


def _is_valid_cached_jpeg(path: pathlib.Path) -> bool:
    """Cheaply verify a cached JPEG is non-empty and bracketed by SOI/EOI markers without decoding."""
    try:
        st = path.stat()
    except OSError:
        return False
    if not path.is_file() or st.st_size < 4:
        return False
    try:
        with open(path, "rb") as handle:
            head = handle.read(2)
            handle.seek(-2, os.SEEK_END)
            tail = handle.read(2)
    except OSError:
        return False
    return head == b"\xff\xd8" and tail == b"\xff\xd9"


def _read_manifest_version(cache_dir: pathlib.Path) -> int | None:
    manifest_path = cache_dir / CACHE_MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, int) and not isinstance(version, bool) else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load_manifest(cache_dir: pathlib.Path) -> dict[str, CacheEntry]:
    """Load cached entries from CACHE_MANIFEST_NAME, returning {} if missing or corrupt."""
    manifest_path = pathlib.Path(cache_dir) / CACHE_MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, dict):
        return {}

    entries: dict[str, CacheEntry] = {}
    for rel_path in sorted(raw_entries):
        raw = raw_entries[rel_path]
        if not isinstance(rel_path, str) or not isinstance(raw, dict):
            return {}
        try:
            local_name = raw["local_name"]
            etag = raw["etag"]
            mtime = raw["mtime"]
            size = raw["size"]
            width = raw["width"]
            height = raw["height"]
            raw_captured = raw["captured_at"]
            luma = raw["luma"]
            sharpness = raw["sharpness"]
            dhash_val = raw["dhash"]
        except KeyError:
            return {}

        if not (
            isinstance(local_name, str)
            and isinstance(etag, str)
            and isinstance(mtime, str)
            and (size is None or _is_int(size))
            and _is_int(width)
            and _is_int(height)
            and _is_int(dhash_val)
            and isinstance(luma, (int, float))
            and not isinstance(luma, bool)
            and isinstance(sharpness, (int, float))
            and not isinstance(sharpness, bool)
        ):
            return {}

        if raw_captured is None:
            captured_at = None
        elif isinstance(raw_captured, str):
            try:
                captured_at = datetime.fromisoformat(raw_captured)
            except ValueError:
                return {}
        else:
            return {}

        entries[rel_path] = CacheEntry(
            rel_path=rel_path,
            local_name=local_name,
            etag=etag,
            mtime=mtime,
            size=size,
            width=width,
            height=height,
            captured_at=captured_at,
            luma=float(luma),
            sharpness=float(sharpness),
            dhash=dhash_val,
        )
    return entries


def save_manifest(
    cache_dir: pathlib.Path,
    entries: dict[str, CacheEntry],
    *,
    version: int = CACHE_VERSION,
) -> None:
    """Atomically persist the cache manifest JSON inside cache_dir."""
    cache_dir = pathlib.Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / CACHE_MANIFEST_NAME
    tmp_path = cache_dir / f".{CACHE_MANIFEST_NAME}.tmp"
    payload = {
        "version": version,
        "entries": {
            rel_path: {
                "local_name": entry.local_name,
                "etag": entry.etag,
                "mtime": entry.mtime,
                "size": entry.size,
                "width": entry.width,
                "height": entry.height,
                "captured_at": entry.captured_at.isoformat() if entry.captured_at is not None else None,
                "luma": entry.luma,
                "sharpness": entry.sharpness,
                "dhash": entry.dhash,
            }
            for rel_path, entry in sorted(entries.items())
        },
    }
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, manifest_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _write_cached_jpeg(image: Image.Image, dest_path: pathlib.Path) -> None:
    tmp_path = dest_path.with_name(f".{dest_path.name}.tmp")
    try:
        image.save(tmp_path, format="JPEG", quality=CACHE_JPEG_QUALITY, optimize=True)
        os.replace(tmp_path, dest_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def http_downloader(session: requests.Session) -> Callable[[object], bytes]:
    """Return a downloader callable backed by a requests.Session."""
    def _download(photo: Any) -> bytes:
        url = photo.url
        with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status_code != 200:
                raise RuntimeError(f"GET {url} returned HTTP {resp.status_code}")
            buf = io.BytesIO()
            for chunk in resp.iter_content(chunk_size=_DOWNLOAD_CHUNK_SIZE):
                if chunk:
                    buf.write(chunk)
            return buf.getvalue()

    return _download


def is_reusable(
    prev_entry: CacheEntry | None,
    photo: Any,
    cache_dir: pathlib.Path,
    *,
    version: int = CACHE_VERSION,
) -> bool:
    """Return True when prev_entry matches photo metadata, cache version, and a valid cached JPEG."""
    if prev_entry is None:
        return False
    cache_path = pathlib.Path(cache_dir)
    if _read_manifest_version(cache_path) != version:
        return False
    local_name = cache_name(photo.rel_path)
    if (
        prev_entry.local_name != local_name
        or prev_entry.etag != photo.etag
        or prev_entry.mtime != photo.mtime
        or prev_entry.size != photo.size
    ):
        return False
    return _is_valid_cached_jpeg(cache_path / local_name)


def refresh(
    photos: Iterable[Any],
    dest_dir: pathlib.Path,
    prev_entries: Mapping[str, CacheEntry],
    *,
    download: Callable[[Any], bytes],
    version: int = CACHE_VERSION,
) -> CacheRefresh:
    """Incrementally update the local photo cache, reusing valid unchanged entries."""
    cache_dir = pathlib.Path(dest_dir) / CACHE_SUBDIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    resolved_cache_dir = cache_dir.resolve()

    sorted_photos = sorted(photos, key=lambda p: p.rel_path)
    entries: dict[str, CacheEntry] = {}
    errors: list[dict] = []
    downloaded = 0
    reused = 0

    for photo in sorted_photos:
        local_name = cache_name(photo.rel_path)
        local_path = cache_dir / local_name
        prev = prev_entries.get(photo.rel_path)

        if is_reusable(prev, photo, cache_dir, version=version):
            assert prev is not None
            entries[photo.rel_path] = prev
            reused += 1
            continue

        try:
            payload = download(photo)
            captured_at = extract_captured_at(payload, name_hint=local_name)
            image, width, height = normalise(payload)
            try:
                _write_cached_jpeg(image, local_path)
                luma_val = photo_select.mean_luma(image)
                sharpness_val = photo_select.sharpness(image)
                dhash_val = photo_select.dhash(image)
            finally:
                image.close()
            entries[photo.rel_path] = CacheEntry(
                rel_path=photo.rel_path,
                local_name=local_name,
                etag=photo.etag,
                mtime=photo.mtime,
                size=photo.size,
                width=width,
                height=height,
                captured_at=captured_at,
                luma=luma_val,
                sharpness=sharpness_val,
                dhash=dhash_val,
            )
            downloaded += 1
        except Exception as exc:
            errors.append({"rel_path": photo.rel_path, "error": str(exc) or type(exc).__name__})

    current_rel_paths = {photo.rel_path for photo in sorted_photos}
    active_local_names = {entry.local_name for entry in entries.values()}
    removed: list[str] = []
    for prev_rel, prev_entry in sorted(prev_entries.items()):
        if prev_rel in current_rel_paths or prev_entry.local_name in active_local_names:
            continue
        victim = (cache_dir / prev_entry.local_name).resolve()
        if victim.parent == resolved_cache_dir and victim.is_file():
            victim.unlink()
            removed.append(prev_entry.local_name)

    save_manifest(cache_dir, entries, version=version)
    return CacheRefresh(
        entries=entries,
        errors=errors,
        downloaded=downloaded,
        reused=reused,
        removed=removed,
    )
