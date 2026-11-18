#!/usr/bin/env python3
"""Pull and resize photos from Nextcloud WebDAV into the local kiosk mirror."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
import xml.etree.ElementTree as ET

from PIL import Image, ImageOps
import requests

DEFAULT_DEST_DIR = pathlib.Path.home() / ".local" / "share" / "kitchen-display" / "photos"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SKIPPED_LOG_EXTENSION = ".heic"
MAX_LONG_SIDE = 1920
MAX_SHORT_SIDE = 1200
JPEG_QUALITY = 80
REQUEST_TIMEOUT = (10, 60)

DAV_NS = "{DAV:}"
PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:">'
    "<d:prop>"
    "<d:resourcetype/>"
    "<d:getetag/>"
    "<d:getlastmodified/>"
    "<d:getcontentlength/>"
    "</d:prop>"
    "</d:propfind>"
)


class SyncError(Exception):
    """Raised when WebDAV listing, download, or safety checks fail."""


@dataclass(frozen=True)
class RemotePhoto:
    rel_path: str
    url: str
    etag: str
    mtime: str
    size: int | None


def _require_env() -> tuple[str, str, str]:
    missing = [
        name
        for name in ("NEXTCLOUD_PHOTO_URL", "NEXTCLOUD_PHOTO_USER", "NEXTCLOUD_PHOTO_APP_PASSWORD")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise ValueError(f"missing required environment variable(s): {', '.join(missing)}")
    url = os.environ["NEXTCLOUD_PHOTO_URL"].strip().rstrip("/") + "/"
    user = os.environ["NEXTCLOUD_PHOTO_USER"].strip()
    password = os.environ["NEXTCLOUD_PHOTO_APP_PASSWORD"].strip()
    return url, user, password


def _local_filename(rel_path: str) -> str:
    digest = hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:10]
    stem = pathlib.PurePosixPath(rel_path).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "photo"
    return f"{digest}-{safe_stem}.jpg"


def _prop_text(prop: ET.Element, tag: str) -> str:
    elem = prop.find(f"{DAV_NS}{tag}")
    if elem is None or elem.text is None:
        return ""
    return elem.text.strip()


def _select_ok_prop(response: ET.Element) -> ET.Element | None:
    propstats = response.findall(f"{DAV_NS}propstat")
    for ps in propstats:
        status_el = ps.find(f"{DAV_NS}status")
        if status_el is None or status_el.text is None or " 200 " in status_el.text:
            prop = ps.find(f"{DAV_NS}prop")
            if prop is not None:
                return prop
    return None


def _rel_path_under(resolved_path: str, base_path: str) -> str | None:
    """Return the path relative to the synced collection, tolerating a proxy prefix.

    Nextcloud behind a reverse proxy reports hrefs like
    /nextcloud/remote.php/dav/files/user/Photos/x.jpg while the configured base is
    /remote.php/dav/files/user/Photos/, so a plain startswith() check drops every
    entry and the mirror silently stays empty.
    """
    base = base_path.rstrip("/") + "/"
    if resolved_path.startswith(base):
        return resolved_path[len(base):]
    marker = base.lstrip("/")
    idx = resolved_path.find("/" + marker)
    if idx >= 0:
        return resolved_path[idx + 1 + len(marker):]
    return None


def _parse_propfind(xml_bytes: bytes, collection_url: str, base_path: str) -> tuple[list[str], list[RemotePhoto], int]:
    """Parse a Depth: 1 PROPFIND XML body into (subcollections, photos, heic_count)."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise SyncError(f"invalid WebDAV XML from {collection_url}: {exc}") from exc

    subdirs: list[str] = []
    photos: list[RemotePhoto] = []
    heic_count = 0

    for response in root.findall(f"{DAV_NS}response"):
        href_el = response.find(f"{DAV_NS}href")
        if href_el is None or not href_el.text:
            continue
        resolved_url = urljoin(collection_url, href_el.text.strip())
        resolved_path = unquote(urlparse(resolved_url).path)
        rel_path = _rel_path_under(resolved_path, base_path)
        if rel_path is None or not rel_path:
            # Outside the synced collection, or the collection itself.
            continue
        rel_path = rel_path.lstrip("/")

        prop = _select_ok_prop(response)
        if prop is None:
            continue

        resourcetype = prop.find(f"{DAV_NS}resourcetype")
        is_dir = resourcetype is not None and resourcetype.find(f"{DAV_NS}collection") is not None
        if is_dir:
            subdirs.append(resolved_url.rstrip("/") + "/")
            continue

        suffix = pathlib.PurePosixPath(rel_path).suffix.lower()
        if suffix == SKIPPED_LOG_EXTENSION:
            heic_count += 1
            continue
        if suffix not in IMAGE_EXTENSIONS:
            continue

        etag = _prop_text(prop, "getetag")
        mtime = _prop_text(prop, "getlastmodified")
        size_text = _prop_text(prop, "getcontentlength")
        size = int(size_text) if size_text.isdigit() else None
        photos.append(RemotePhoto(rel_path=rel_path, url=resolved_url, etag=etag, mtime=mtime, size=size))

    return subdirs, photos, heic_count


def list_remote_photos(session: requests.Session, base_url: str) -> tuple[list[RemotePhoto], int]:
    """Walk the remote WebDAV tree using Depth: 1 PROPFIND per collection."""
    base_url = base_url.rstrip("/") + "/"
    base_path = unquote(urlparse(base_url).path).rstrip("/") + "/"
    queue: deque[str] = deque([base_url])
    visited: set[str] = set()
    discovered: list[RemotePhoto] = []
    heic_total = 0

    headers = {
        "Depth": "1",
        "Content-Type": "application/xml; charset=utf-8",
    }

    while queue:
        col_url = queue.popleft()
        norm_path = unquote(urlparse(col_url).path).rstrip("/") + "/"
        if norm_path in visited:
            continue
        visited.add(norm_path)

        try:
            resp = session.request("PROPFIND", col_url, headers=headers, data=PROPFIND_BODY, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise SyncError(f"PROPFIND {col_url} failed: {exc}") from exc

        if resp.status_code not in (200, 207):
            raise SyncError(f"PROPFIND {col_url} returned HTTP {resp.status_code}")

        subdirs, photos, heic_count = _parse_propfind(resp.content, col_url, base_path)
        discovered.extend(photos)
        heic_total += heic_count
        for sub in subdirs:
            sub_norm = unquote(urlparse(sub).path).rstrip("/") + "/"
            if sub_norm not in visited:
                queue.append(sub)

    return discovered, heic_total


def _is_unchanged(photo: RemotePhoto, prev: dict[str, Any] | None, local_path: pathlib.Path, local_name: str) -> bool:
    if not isinstance(prev, dict):
        return False
    if prev.get("local_name") != local_name or not local_path.is_file():
        return False
    return (
        prev.get("etag", "") == photo.etag
        and prev.get("mtime", "") == photo.mtime
        and prev.get("size") == photo.size
    )


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.split()[3])
        return background
    return img.convert("RGB")


def _resize_and_save_jpeg(src_path: pathlib.Path, dest_path: pathlib.Path) -> tuple[int, int]:
    tmp_out = dest_path.with_name(f"{dest_path.name}.tmp")
    try:
        with Image.open(src_path) as opened:
            img = ImageOps.exif_transpose(opened)
            img = _to_rgb(img)
            img.thumbnail((MAX_LONG_SIDE, MAX_SHORT_SIDE), Image.Resampling.LANCZOS)
            width, height = img.size
            img.save(tmp_out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        os.replace(tmp_out, dest_path)
        return width, height
    finally:
        if tmp_out.exists():
            tmp_out.unlink()


def _download_and_convert(session: requests.Session, url: str, dest_path: pathlib.Path) -> tuple[int, int]:
    dl_tmp = dest_path.with_name(f".dl-{dest_path.name}.tmp")
    try:
        try:
            with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status_code != 200:
                    raise SyncError(f"GET {url} returned HTTP {resp.status_code}")
                with open(dl_tmp, "wb") as handle:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            handle.write(chunk)
        except requests.RequestException as exc:
            raise SyncError(f"GET {url} failed: {exc}") from exc

        try:
            return _resize_and_save_jpeg(dl_tmp, dest_path)
        except Exception as exc:
            raise SyncError(f"image processing failed for {url}: {exc}") from exc
    finally:
        if dl_tmp.exists():
            dl_tmp.unlink()


def _atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _load_mirror_manifest(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SyncError(f"invalid mirror manifest structure in {path}")
    return data


def sync_photos(
    base_url: str,
    user: str,
    password: str,
    dest_dir: pathlib.Path = DEFAULT_DEST_DIR,
    *,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> list[str]:
    """Synchronise photos from WebDAV to dest_dir. Returns dry-run action lines if dry_run=True."""
    mirror_path = dest_dir / ".mirror-manifest.json"
    manifest_path = dest_dir / "manifest.json"
    prev_manifest = _load_mirror_manifest(mirror_path)

    owns_session = session is None
    if session is None:
        session = requests.Session()
    session.auth = (user, password)

    try:
        discovered, heic_count = list_remote_photos(session, base_url)

        prev_count = len(prev_manifest)
        if prev_count > 0 and len(discovered) < prev_count * 0.5:
            raise SyncError(
                f"discovered count dropped from {prev_count} to {len(discovered)} (>50%); aborting before prune"
            )

        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)

        actions: list[str] = []
        new_mirror: dict[str, dict[str, Any]] = {}
        manifest_items: list[dict[str, Any]] = []
        expected_locals: set[str] = set()

        for photo in sorted(discovered, key=lambda p: p.rel_path):
            local_name = _local_filename(photo.rel_path)
            local_path = dest_dir / local_name
            expected_locals.add(local_name)
            prev_entry = prev_manifest.get(photo.rel_path)

            if _is_unchanged(photo, prev_entry, local_path, local_name):
                width = prev_entry.get("w")
                height = prev_entry.get("h")
                if not isinstance(width, int) or not isinstance(height, int):
                    with Image.open(local_path) as existing:
                        width, height = existing.size
            elif dry_run:
                verb = "update" if photo.rel_path in prev_manifest else "add"
                actions.append(f"{verb} {photo.rel_path} -> {local_name}")
                width, height = 0, 0
            else:
                width, height = _download_and_convert(session, photo.url, local_path)

            new_mirror[photo.rel_path] = {
                "etag": photo.etag,
                "mtime": photo.mtime,
                "size": photo.size,
                "local_name": local_name,
                "w": width,
                "h": height,
            }
            manifest_items.append({"name": local_name, "w": width, "h": height})

        # Determine files to prune after the full listing succeeded.
        for remote_path, entry in sorted(prev_manifest.items()):
            old_local = entry.get("local_name") if isinstance(entry, dict) else None
            if remote_path not in new_mirror and old_local and old_local not in expected_locals:
                if dry_run:
                    actions.append(f"prune {old_local}")

        if dry_run:
            if heic_count > 0:
                actions.append(f"skipped {heic_count} heic file(s)")
            return actions

        manifest_items.sort(key=lambda item: item["name"])
        parsed = urlparse(base_url)
        origin = parsed.hostname or parsed.netloc
        manifest_payload = {
            "count": len(manifest_items),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "origin": origin,
            "skipped_heic": heic_count,
            "items": manifest_items,
        }
        _atomic_write_json(mirror_path, new_mirror)
        _atomic_write_json(manifest_path, manifest_payload)

        if dest_dir.is_dir():
            keep_names = expected_locals | {"manifest.json", ".mirror-manifest.json"}
            for child in dest_dir.iterdir():
                if child.is_file() and child.name not in keep_names:
                    child.unlink()

        # HEIC skips are recorded in manifest.json ("skipped_heic"), never on stderr:
        # cron runs must stay silent on success and HEIC is a standing share condition.
        return []
    finally:
        if owns_session:
            session.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror Nextcloud Photos over WebDAV for kitchen-display.")
    parser.add_argument(
        "--dest",
        type=pathlib.Path,
        default=DEFAULT_DEST_DIR,
        help="Destination directory for resized photos and manifest.json.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without downloading or writing files.",
    )
    args = parser.parse_args(argv)

    try:
        base_url, user, password = _require_env()
    except ValueError as exc:
        msg = str(exc).replace("\n", " ")
        print(f"sync-photos: {msg}", file=sys.stderr)
        return 2

    try:
        actions = sync_photos(base_url, user, password, dest_dir=args.dest, dry_run=args.dry_run)
        if args.dry_run:
            for line in actions:
                print(line)
        return 0
    except Exception as exc:
        msg = str(exc).replace("\n", " ")
        print(f"sync-photos: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
