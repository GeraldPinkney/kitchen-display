#!/usr/bin/env python3
"""Sync Nextcloud WebDAV photos, select slides, render, and publish manifest.json."""
from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import importlib
import importlib.util
import json
import os
import pathlib
import sys
import time
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
import xml.etree.ElementTree as ET

from PIL import Image
import requests

DEFAULT_DEST_DIR = pathlib.Path.home() / ".local" / "share" / "kitchen-display" / "photos"
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
SKIPPED_LOG_EXTENSION = ".heic"
REQUEST_TIMEOUT = (10, 60)
STALE_TMP_SECONDS = 3600
MAX_ERROR_FRACTION = 0.20
RENDER_MANIFEST_NAME = ".render-manifest.json"


def _load_sibling(name: str):
    """Import a sibling tool module, reusing any already-loaded test or package instance."""
    for modname in (name, f"kd_{name}"):
        if modname in sys.modules:
            return sys.modules[modname]
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        path = pathlib.Path(__file__).resolve().with_name(f"{name}.py")
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod


photo_select = _load_sibling("photo_select")
sys.modules.setdefault("photo_select", photo_select)
photo_cache = _load_sibling("photo_cache")
photo_format = _load_sibling("photo_format")

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


DEFAULT_ENV_FILE = pathlib.Path.home() / ".config" / "kitchen-display" / "env"


def load_env_file(path: pathlib.Path | None = None) -> None:
    """Load KEY=VALUE pairs from the kiosk env file without overriding the environment.

    Cron runs with a bare environment, so the panel's credential file has to be read
    here rather than relying on a shell wrapper in the crontab line.
    """
    env_path = pathlib.Path(path or os.environ.get("KITCHEN_ENV_FILE") or DEFAULT_ENV_FILE)
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip("'\"")


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


def _acquire_sync_lock(dest_dir: pathlib.Path):
    """Take an exclusive non-blocking lock so cron and manual runs cannot overlap.

    Two concurrent runs are destructive: each prunes files the other is mid-download
    on, which surfaced as "No such file or directory: .../.dl-*.tmp".
    """
    import fcntl

    handle = open(dest_dir / ".sync.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _prune_dest(dest_dir: pathlib.Path, keep_names: set[str], now: float | None = None) -> list[str]:
    """Remove stale mirror outputs, leaving dot-files, notes and in-flight downloads alone."""
    removed: list[str] = []
    stamp = time.time() if now is None else now
    for child in dest_dir.iterdir():
        if not child.is_file() or child.name in keep_names or child.name.startswith("."):
            continue
        if child.suffix.lower() == ".jpg":
            child.unlink()
            removed.append(child.name)
        elif child.suffix == ".tmp" and stamp - child.stat().st_mtime > STALE_TMP_SECONDS:
            child.unlink()
            removed.append(child.name)
    return removed


def _load_render_manifest(path: pathlib.Path) -> dict[str, str]:
    """Load the per-slide render key manifest, returning {} if missing or corrupt."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _render_key(
    kind: str,
    names: Sequence[str],
    etags: Sequence[str],
    caption_date_iso: str | None,
) -> str:
    """Return a deterministic SHA-256 hex digest covering slide format, inputs, and caption date."""
    payload = json.dumps(
        [photo_format.FORMAT_VERSION, kind, list(names), list(etags), caption_date_iso],
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _plan_dry_run(
    sorted_discovered: Sequence[RemotePhoto],
    prev_cache: Mapping[str, Any],
    cache_dir: pathlib.Path,
    heic_count: int,
    today: date,
) -> list[str]:
    """Compute dry-run action lines without touching the network cache or writing files."""
    actions: list[str] = []
    expected_cache_names: set[str] = set()
    all_reusable = True

    for photo in sorted_discovered:
        expected_cache_names.add(photo_cache.cache_name(photo.rel_path))
        prev_entry = prev_cache.get(photo.rel_path)
        if not photo_cache.is_reusable(prev_entry, photo, cache_dir):
            all_reusable = False
            verb = "update" if photo.rel_path in prev_cache else "add"
            actions.append(f"{verb} {photo.rel_path}")

    current_rel_paths = {photo.rel_path for photo in sorted_discovered}
    for prev_rel, prev_entry in sorted(prev_cache.items()):
        if prev_rel not in current_rel_paths and prev_entry.local_name not in expected_cache_names:
            actions.append(f"prune {prev_entry.local_name}")

    if heic_count > 0:
        actions.append(f"skipped {heic_count} heic file(s)")

    if all_reusable:
        candidates = photo_select.candidates_from_cache(
            [prev_cache[photo.rel_path] for photo in sorted_discovered]
        )
        selection = photo_select.select(candidates, today=today)
        actions.append(
            f"deck: {len(selection.slides)} slide(s) from {len(candidates)} photo(s) "
            f"({selection.days_covered}/{selection.days_total} day(s))"
        )

    return actions


def _render_slides(
    dest_dir: pathlib.Path,
    cache_dir: pathlib.Path,
    entries: Mapping[str, Any],
    selection: Any,
    order: Sequence[str],
    prev_render_manifest: Mapping[str, str],
) -> tuple[list[dict[str, Any]], dict[str, str], set[str], list[dict[str, Any]]]:
    """Render any uncached or changed slides in playlist order and return manifest metadata."""
    slides_by_key = {slide.key: slide for slide in selection.slides}
    items: list[dict[str, Any]] = []
    new_render_manifest: dict[str, str] = {}
    keep_slides: set[str] = set()
    render_errors: list[dict[str, Any]] = []

    for day_idx, slide_key in enumerate(order):
        slide = slides_by_key[slide_key]
        cache_names = [entries[name].local_name for name in slide.names]
        etags = [entries[name].etag for name in slide.names]
        caption_date_iso = slide.captured_at.date().isoformat() if slide.captured_at is not None else None
        dest_name = photo_format.slide_name(slide.kind, cache_names)
        dest_path = dest_dir / dest_name
        key = _render_key(slide.kind, slide.names, etags, caption_date_iso)

        if prev_render_manifest.get(dest_name) != key or not dest_path.is_file():
            src_paths = [cache_dir / cname for cname in cache_names]
            opened: list[Image.Image] = []
            try:
                for src_path in src_paths:
                    opened.append(Image.open(src_path))
                rendered = photo_format.render_slide(
                    slide.kind,
                    opened,
                    slide.captured_at,
                    slide.names[0],
                )
                try:
                    photo_format.save_jpeg(rendered, dest_path)
                finally:
                    rendered.close()
            except Exception as exc:  # one unreadable slide must never abort the deck
                render_errors.append({"rel_path": slide.names[0], "error": f"render: {exc}"})
                continue
            finally:
                for im in opened:
                    im.close()

        new_render_manifest[dest_name] = key
        keep_slides.add(dest_name)
        items.append(
            {
                "name": dest_name,
                "kind": slide.kind,
                "date": caption_date_iso,
                "day_order": day_idx,
                "sources": list(slide.names),
            }
        )

    return items, new_render_manifest, keep_slides, render_errors


def _build_manifest_payload(
    *,
    origin: str,
    heic_count: int,
    errors: Sequence[dict[str, Any]],
    candidates: Sequence[Any],
    selection: Any,
    items: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Assemble the schema-2 manifest.json dictionary."""
    admission_rejects = sum(
        selection.rejected.get(reason, 0) for reason in ("too_small", "aspect", "screenshot")
    )
    undated_slides = sum(1 for slide in selection.slides if slide.captured_at is None)
    return {
        "schema": 2,
        "count": len(items),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "origin": origin,
        "skipped_heic": heic_count,
        "errors": list(errors),
        "selection": {
            "photos": len(candidates),
            "admitted": len(candidates) - admission_rejects,
            "rejected": dict(selection.rejected),
            "slides": len(selection.slides),
            "days_covered": selection.days_covered,
            "days_total": selection.days_total,
            "undated": undated_slides,
        },
        "items": list(items),
    }


def sync_photos(
    base_url: str,
    user: str,
    password: str,
    dest_dir: pathlib.Path = DEFAULT_DEST_DIR,
    *,
    dry_run: bool = False,
    session: requests.Session | None = None,
    today: date | None = None,
) -> list[str]:
    """Synchronise photos from WebDAV to dest_dir. Returns dry-run action lines if dry_run=True."""
    dest_dir = pathlib.Path(dest_dir)
    effective_today = date.today() if today is None else today

    cache_dir = dest_dir / photo_cache.CACHE_SUBDIR
    cache_manifest_path = cache_dir / photo_cache.CACHE_MANIFEST_NAME
    prev_cache = photo_cache.load_manifest(cache_dir)
    if cache_manifest_path.is_file():
        prev_count = len(prev_cache)
    else:
        prev_count = len(_load_mirror_manifest(dest_dir / ".mirror-manifest.json"))

    owns_session = session is None
    if session is None:
        session = requests.Session()
    session.auth = (user, password)
    lock = None

    try:
        discovered, heic_count = list_remote_photos(session, base_url)

        if prev_count > 0 and len(discovered) < prev_count * 0.5:
            raise SyncError(
                f"discovered count dropped from {prev_count} to {len(discovered)} (>50%); aborting before prune"
            )

        sorted_discovered = sorted(discovered, key=lambda p: p.rel_path)

        if dry_run:
            return _plan_dry_run(sorted_discovered, prev_cache, cache_dir, heic_count, effective_today)

        dest_dir.mkdir(parents=True, exist_ok=True)
        lock = _acquire_sync_lock(dest_dir)
        if lock is None:
            # Another sync owns the mirror; cron overlap is expected and harmless.
            return []

        refresh = photo_cache.refresh(
            sorted_discovered,
            dest_dir,
            prev_cache,
            download=photo_cache.http_downloader(session),
        )

        if len(refresh.errors) > MAX_ERROR_FRACTION * len(discovered):
            raise SyncError(
                f"{len(refresh.errors)} of {len(discovered)} photos failed (>20%); aborting before publish"
            )

        candidates = photo_select.candidates_from_cache(refresh.entries.values())
        selection = photo_select.select(candidates, today=effective_today)
        order = photo_select.day_order(selection, seed=effective_today)

        manifest_items, new_render_manifest, keep_slides, render_errors = _render_slides(
            dest_dir,
            cache_dir,
            refresh.entries,
            selection,
            order,
            _load_render_manifest(dest_dir / RENDER_MANIFEST_NAME),
        )

        all_errors = list(refresh.errors) + render_errors
        if len(all_errors) > MAX_ERROR_FRACTION * len(discovered):
            raise SyncError(
                f"{len(all_errors)} of {len(discovered)} photos failed (>20%); aborting before publish"
            )

        parsed = urlparse(base_url)
        origin = parsed.hostname or parsed.netloc
        manifest_payload = _build_manifest_payload(
            origin=origin,
            heic_count=heic_count,
            errors=all_errors,
            candidates=candidates,
            selection=selection,
            items=manifest_items,
        )
        _atomic_write_json(dest_dir / "manifest.json", manifest_payload)
        _atomic_write_json(dest_dir / RENDER_MANIFEST_NAME, new_render_manifest)
        _prune_dest(dest_dir, keep_slides | {"manifest.json"})

        # HEIC skips are recorded in manifest.json ("skipped_heic"), never on stderr:
        # cron runs must stay silent on success and HEIC is a standing share condition.
        return []
    finally:
        if lock is not None:
            lock.close()
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

    load_env_file()
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
