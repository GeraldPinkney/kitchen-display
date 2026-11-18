"""Offline specs for tools/sync_photos.py using a local HTTP WebDAV fake."""
from __future__ import annotations

from datetime import date, datetime
import http.server
import io
import json
import os
import pathlib
import random
import socketserver
import threading
import time

from PIL import Image
import pytest

from conftest import load_tool

TODAY = date(2026, 11, 18)


def _make_image_bytes(
    size: tuple[int, int],
    fmt: str = "JPEG",
    mode: str = "RGB",
    color=(120, 45, 200),
    *,
    seed: int | None = None,
    exif_dt: datetime | None = None,
) -> bytes:
    buf = io.BytesIO()
    if seed is not None:
        rng = random.Random(seed)
        grid = Image.new("RGB", (9, 8))
        grid.putdata(
            [(rng.randint(25, 220), rng.randint(25, 220), rng.randint(25, 220)) for _ in range(9 * 8)]
        )
        img = grid.resize(size, Image.Resampling.NEAREST)
        if mode == "RGBA":
            img = img.convert("RGBA")
            img.putalpha(128)
    elif mode == "RGBA":
        img = Image.new("RGBA", size, (*color[:3], 128))
    else:
        img = Image.new(mode, size, color)

    save_kwargs: dict = {"format": fmt}
    if exif_dt is not None:
        exif = Image.Exif()
        exif.get_ifd(0x8769)[36867] = exif_dt.strftime("%Y:%m:%d %H:%M:%S")
        save_kwargs["exif"] = exif
    img.save(buf, **save_kwargs)
    return buf.getvalue()


class _FakeWebDAVState:
    def __init__(self):
        # mapping collection path (e.g. "/Photos/" or "/Photos/2026/") -> list of entry dicts
        # entry: {"href": ..., "is_dir": bool, "etag": str, "mtime": str, "size": int, "data": bytes}
        self.collections: dict[str, list[dict]] = {}
        self.files: dict[str, bytes] = {}
        self.fail_propfind_paths: set[str] = set()
        self.propfind_calls: list[str] = []
        self.get_calls: list[str] = []


class _FakeWebDAVHandler(http.server.BaseHTTPRequestHandler):
    state: _FakeWebDAVState

    def log_message(self, format, *args):  # noqa: A002
        return

    def do_PROPFIND(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.endswith("/"):
            path += "/"
        self.server.state.propfind_calls.append(path)

        if path in self.server.state.fail_propfind_paths or path not in self.server.state.collections:
            self.send_response(500 if path in self.server.state.fail_propfind_paths else 404)
            self.end_headers()
            return

        entries = self.server.state.collections[path]
        items_xml = [
            f"""<d:response>
  <d:href>{path}</d:href>
  <d:propstat>
    <d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
</d:response>"""
        ]
        for entry in entries:
            if entry.get("is_dir"):
                items_xml.append(
                    f"""<d:response>
  <d:href>{entry['href']}</d:href>
  <d:propstat>
    <d:prop><d:resourcetype><d:collection/></d:resourcetype><d:getetag>{entry.get('etag', '')}</d:getetag></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
</d:response>"""
                )
            else:
                items_xml.append(
                    f"""<d:response>
  <d:href>{entry['href']}</d:href>
  <d:propstat>
    <d:prop>
      <d:resourcetype/>
      <d:getetag>{entry.get('etag', '')}</d:getetag>
      <d:getlastmodified>{entry.get('mtime', '')}</d:getlastmodified>
      <d:getcontentlength>{entry.get('size', 0)}</d:getcontentlength>
    </d:prop>
    <d:status>HTTP/1.1 200 OK</d:status>
  </d:propstat>
</d:response>"""
                )

        body = ('<?xml version="1.0" encoding="utf-8"?><d:multistatus xmlns:d="DAV:">' + "".join(items_xml) + "</d:multistatus>").encode("utf-8")
        self.send_response(207)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        self.server.state.get_calls.append(path)
        data = self.server.state.files.get(path)
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture
def webdav_server():
    state = _FakeWebDAVState()
    server = _ThreadedHTTPServer(("127.0.0.1", 0), _FakeWebDAVHandler)
    server.state = state
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield state, f"http://{host}:{port}/Photos/"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def sync_mod():
    return load_tool("sync_photos")


@pytest.fixture(autouse=True)
def _hermetic_env_file(monkeypatch):
    """Never read the real ~/.config/kitchen-display/env from the test host."""
    monkeypatch.setenv("KITCHEN_ENV_FILE", "/nonexistent/kitchen-display/env")


def test_env_file_loader_reads_credentials_without_overriding_environment(sync_mod, monkeypatch, tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text(
        "# comment\n"
        "NEXTCLOUD_PHOTO_URL=https://cloud.example.test/remote.php/dav/files/kiosk/Photos\n"
        "NEXTCLOUD_PHOTO_USER='kiosk'\n"
        'NEXTCLOUD_PHOTO_APP_PASSWORD="quoted-secret"\n'
        "ALREADY_SET=from-file\n",
        encoding="utf-8",
    )
    for var in ("NEXTCLOUD_PHOTO_URL", "NEXTCLOUD_PHOTO_USER", "NEXTCLOUD_PHOTO_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALREADY_SET", "from-environment")

    sync_mod.load_env_file(env_file)

    assert os.environ["NEXTCLOUD_PHOTO_URL"].endswith("/Photos")
    assert os.environ["NEXTCLOUD_PHOTO_USER"] == "kiosk"
    assert os.environ["NEXTCLOUD_PHOTO_APP_PASSWORD"] == "quoted-secret"
    assert os.environ["ALREADY_SET"] == "from-environment"
    assert sync_mod._require_env()[1] == "kiosk"


def test_env_file_loader_is_a_noop_when_file_missing(sync_mod, monkeypatch):
    for var in ("NEXTCLOUD_PHOTO_URL", "NEXTCLOUD_PHOTO_USER", "NEXTCLOUD_PHOTO_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    sync_mod.load_env_file("/nonexistent/kitchen-display/env")
    assert "NEXTCLOUD_PHOTO_USER" not in os.environ


def test_missing_credentials_exits_2_with_single_stderr_line(sync_mod, monkeypatch, capsys, tmp_path):
    for var in ("NEXTCLOUD_PHOTO_URL", "NEXTCLOUD_PHOTO_USER", "NEXTCLOUD_PHOTO_APP_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    rc = sync_mod.main(["--dest", str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("sync-photos: ")


def test_parses_propfind_fixture_xml(sync_mod, repo_root):
    xml_bytes = (repo_root / "tests" / "fixtures" / "photo_propfind.xml").read_bytes()
    subdirs, photos, heic_count = sync_mod._parse_propfind(
        xml_bytes,
        "https://cloud.example.test/remote.php/dav/files/kiosk/Photos/",
        "/remote.php/dav/files/kiosk/Photos/",
    )
    assert subdirs == ["https://cloud.example.test/remote.php/dav/files/kiosk/Photos/2026/"]
    assert [p.rel_path for p in photos] == ["garden.jpg"]
    assert photos[0].etag == '"garden-1"'
    assert photos[0].size == 1024
    assert heic_count == 1


def _seed_single_photo(state, sync_mod) -> None:
    """Populate the fake WebDAV with one /Photos/ landscape JPEG."""
    data = _make_image_bytes((1200, 900), fmt="JPEG")
    state.collections["/Photos/"] = [
        {
            "href": "/Photos/one.jpg",
            "is_dir": False,
            "etag": '"e1"',
            "mtime": "Mon, 01 Jan 2026 00:00:00 GMT",
            "size": len(data),
        },
    ]
    state.files["/Photos/one.jpg"] = data


def test_concurrent_sync_is_skipped_when_lock_is_held(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    _seed_single_photo(state, sync_mod)
    dest = tmp_path / "photos"
    dest.mkdir()
    held = sync_mod._acquire_sync_lock(dest)
    assert held is not None
    try:
        assert sync_mod.sync_photos(base_url, "kiosk", "secret", dest_dir=dest) == []
        assert not (dest / "manifest.json").exists()
        assert state.get_calls == [], "a locked-out run must not download anything"
    finally:
        held.close()
    sync_mod.sync_photos(base_url, "kiosk", "secret", dest_dir=dest)
    assert (dest / "manifest.json").is_file()


def test_prune_removes_stale_jpegs_but_keeps_inflight_temp_and_notes(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    _seed_single_photo(state, sync_mod)
    dest = tmp_path / "photos"
    dest.mkdir()
    stale_jpeg = dest / "deadbeef01-gone.jpg"
    stale_jpeg.write_bytes(b"x")
    inflight = dest / ".dl-deadbeef01-gone.jpg.tmp"
    inflight.write_bytes(b"x")
    old_tmp = dest / "leftover.jpg.tmp"
    old_tmp.write_bytes(b"x")
    os.utime(old_tmp, (time.time() - 7200, time.time() - 7200))
    notes = dest / "README.txt"
    notes.write_text("keep me", encoding="utf-8")

    _, base_url = webdav_server
    sync_mod.sync_photos(base_url, "kiosk", "secret", dest_dir=dest)
    assert not stale_jpeg.exists()
    assert inflight.exists(), "in-flight download of a concurrent run must survive prune"
    assert notes.exists()
    assert not old_tmp.exists(), "stale temp files older than an hour should be reclaimed"


def test_parses_propfind_with_reverse_proxy_prefix(sync_mod):
    """Nextcloud behind /nextcloud reports prefixed hrefs; a startswith() base check drops them all."""
    xml = b"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response><d:href>/nextcloud/remote.php/dav/files/kiosk/Photos/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
  <d:response><d:href>/nextcloud/remote.php/dav/files/kiosk/Photos/2026/</d:href>
    <d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
  <d:response><d:href>/nextcloud/remote.php/dav/files/kiosk/Photos/2026/garden.jpg</d:href>
    <d:propstat><d:prop><d:getetag>&quot;garden-1&quot;</d:getetag>
    <d:getcontentlength>2048</d:getcontentlength>
    <d:getlastmodified>Mon, 05 Jan 2026 10:00:00 GMT</d:getlastmodified></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
  <d:response><d:href>/nextcloud/remote.php/dav/files/other/secret.jpg</d:href>
    <d:propstat><d:prop><d:getetag>&quot;s1&quot;</d:getetag></d:prop>
    <d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>
"""
    collection_url = "https://cloud.example.test/remote.php/dav/files/kiosk/Photos/"
    subdirs, photos, heic_count = sync_mod._parse_propfind(
        xml, collection_url, "/remote.php/dav/files/kiosk/Photos/"
    )
    assert subdirs == ["https://cloud.example.test/nextcloud/remote.php/dav/files/kiosk/Photos/2026/"]
    assert [p.rel_path for p in photos] == ["2026/garden.jpg"]
    assert photos[0].etag == '"garden-1"'
    assert photos[0].size == 2048
    assert photos[0].url == "https://cloud.example.test/nextcloud/remote.php/dav/files/kiosk/Photos/2026/garden.jpg"
    assert heic_count == 0


def test_recursive_sync_resizes_writes_manifest_and_skips_heic(sync_mod, webdav_server, tmp_path, capsys):
    state, base_url = webdav_server
    landscape = _make_image_bytes((2400, 1800), fmt="JPEG", seed=1)
    portrait_jpg = _make_image_bytes((1800, 2400), fmt="JPEG", seed=2)
    portrait_png = _make_image_bytes((1800, 2400), fmt="PNG", mode="RGBA", seed=3)
    tiny_webp = _make_image_bytes((8, 8), fmt="WEBP", seed=4)

    state.collections["/Photos/"] = [
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d1"'},
        {"href": "/Photos/root.jpg", "is_dir": False, "etag": '"e1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(landscape)},
        {"href": "/Photos/skipme.HEIC", "is_dir": False, "etag": '"h1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": 100},
        {"href": "/Photos/readme.txt", "is_dir": False, "etag": '"t1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": 10},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/portrait.jpg", "is_dir": False, "etag": '"e2"', "mtime": "Tue, 02 Jan 2026 00:00:00 GMT", "size": len(portrait_jpg)},
        {"href": "/Photos/2026/portrait.png", "is_dir": False, "etag": '"e3"', "mtime": "Tue, 02 Jan 2026 00:00:00 GMT", "size": len(portrait_png)},
        {"href": "/Photos/2026/tiny.webp", "is_dir": False, "etag": '"e4"', "mtime": "Wed, 03 Jan 2026 00:00:00 GMT", "size": len(tiny_webp)},
    ]
    state.files["/Photos/root.jpg"] = landscape
    state.files["/Photos/2026/portrait.jpg"] = portrait_jpg
    state.files["/Photos/2026/portrait.png"] = portrait_png
    state.files["/Photos/2026/tiny.webp"] = tiny_webp

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "kiosk", "secret", dest_dir=dest, today=TODAY)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 2
    assert manifest["count"] == 2
    assert manifest["skipped_heic"] == 1
    assert manifest["origin"] == "127.0.0.1"
    assert "T" in manifest["generated_at"]
    assert manifest["selection"]["photos"] == 4
    assert manifest["selection"]["admitted"] == 2
    assert manifest["selection"]["rejected"]["too_small"] == 1
    assert manifest["selection"]["rejected"]["screenshot"] == 1

    by_source = {item["sources"][0]: item for item in manifest["items"]}
    assert set(by_source) == {"root.jpg", "2026/portrait.jpg"}
    assert by_source["root.jpg"]["kind"] == "single"
    assert by_source["2026/portrait.jpg"]["kind"] == "backdrop"

    for item in manifest["items"]:
        with Image.open(dest / item["name"]) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.size == (1920, 1200)


def test_incremental_sync_skips_unchanged_and_refetches_changed(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    img1 = _make_image_bytes((2400, 1800), seed=10)
    img2 = _make_image_bytes((2400, 1600), seed=20)
    state.collections["/Photos/"] = [
        {"href": "/Photos/a.jpg", "is_dir": False, "etag": '"a1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(img1)},
        {"href": "/Photos/b.jpg", "is_dir": False, "etag": '"b1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(img2)},
    ]
    state.files["/Photos/a.jpg"] = img1
    state.files["/Photos/b.jpg"] = img2

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    assert len(state.get_calls) == 2

    state.get_calls.clear()
    manifest1 = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    by_src = {item["sources"][0]: dest / item["name"] for item in manifest1["items"]}
    a_file = by_src["a.jpg"]
    mtime_before = a_file.stat().st_mtime_ns

    # Second run: identical metadata -> zero GETs and file untouched.
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    assert state.get_calls == []
    assert a_file.stat().st_mtime_ns == mtime_before

    # Update etag on b.jpg only -> only b.jpg is re-fetched.
    new_b = _make_image_bytes((2400, 1600), seed=21)
    state.files["/Photos/b.jpg"] = new_b
    state.collections["/Photos/"][1]["etag"] = '"b2"'
    state.collections["/Photos/"][1]["size"] = len(new_b)

    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    assert state.get_calls == ["/Photos/b.jpg"]
    assert a_file.stat().st_mtime_ns == mtime_before


def test_collision_free_names_and_prune_removed(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    img_keep = _make_image_bytes((2400, 1800), seed=101)
    img_2025 = _make_image_bytes((2400, 1800), seed=102)
    img_2026 = _make_image_bytes((2400, 1800), seed=103)
    state.collections["/Photos/"] = [
        {"href": "/Photos/2025/", "is_dir": True, "etag": '"d25"'},
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d26"'},
        {"href": "/Photos/keep.jpg", "is_dir": False, "etag": '"k1"', "mtime": "m", "size": len(img_keep)},
    ]
    state.collections["/Photos/2025/"] = [
        {"href": "/Photos/2025/same.jpg", "is_dir": False, "etag": '"s25"', "mtime": "m", "size": len(img_2025)},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/same.jpg", "is_dir": False, "etag": '"s26"', "mtime": "m", "size": len(img_2026)},
    ]
    state.files["/Photos/keep.jpg"] = img_keep
    state.files["/Photos/2025/same.jpg"] = img_2025
    state.files["/Photos/2026/same.jpg"] = img_2026

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    names = [item["name"] for item in manifest["items"]]
    assert len(names) == 3
    assert len(set(names)) == 3  # 2025/same.jpg and 2026/same.jpg do not collide

    # Remove 2025/same.jpg (drop is 1/3 = 33% <= 50%, so prune proceeds).
    state.collections["/Photos/2025/"] = []
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest2 = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest2["count"] == 2
    jpgs_on_disk = {p.name for p in dest.glob("*.jpg")}
    assert jpgs_on_disk == {item["name"] for item in manifest2["items"]}


def test_transient_drop_over_50_percent_aborts_before_delete(sync_mod, webdav_server, tmp_path, monkeypatch, capsys):
    state, base_url = webdav_server
    state.collections["/Photos/"] = []
    for i in range(4):
        img = _make_image_bytes((2400, 1800), seed=200 + i)
        state.collections["/Photos/"].append(
            {"href": f"/Photos/p{i}.jpg", "is_dir": False, "etag": f'"{i}"', "mtime": "m", "size": len(img)}
        )
        state.files[f"/Photos/p{i}.jpg"] = img

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    assert len(list(dest.glob("*.jpg"))) == 4

    # Simulate remote returning only 1 of 4 (75% drop > 50%).
    state.collections["/Photos/"] = state.collections["/Photos/"][:1]
    monkeypatch.setenv("NEXTCLOUD_PHOTO_URL", base_url)
    monkeypatch.setenv("NEXTCLOUD_PHOTO_USER", "u")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_APP_PASSWORD", "p")
    rc = sync_mod.main(["--dest", str(dest)])
    captured = capsys.readouterr()
    assert rc == 1
    err_lines = captured.err.strip().splitlines()
    assert len(err_lines) == 1
    assert err_lines[0].startswith("sync-photos: ")
    # All 4 existing slides and manifest must remain intact.
    assert len(list(dest.glob("*.jpg"))) == 4
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["count"] == 4


def test_propfind_failure_aborts_before_prune(sync_mod, webdav_server, tmp_path, monkeypatch, capsys):
    state, base_url = webdav_server
    img_a = _make_image_bytes((2400, 1800), seed=301)
    img_b = _make_image_bytes((2400, 1800), seed=302)
    state.collections["/Photos/"] = [
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d1"'},
        {"href": "/Photos/a.jpg", "is_dir": False, "etag": '"a1"', "mtime": "m", "size": len(img_a)},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/b.jpg", "is_dir": False, "etag": '"b1"', "mtime": "m", "size": len(img_b)},
    ]
    state.files["/Photos/a.jpg"] = img_a
    state.files["/Photos/2026/b.jpg"] = img_b

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    assert len(list(dest.glob("*.jpg"))) == 2

    state.fail_propfind_paths.add("/Photos/2026/")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_URL", base_url)
    monkeypatch.setenv("NEXTCLOUD_PHOTO_USER", "u")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_APP_PASSWORD", "p")
    rc = sync_mod.main(["--dest", str(dest)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip().startswith("sync-photos: ")
    assert len(list(dest.glob("*.jpg"))) == 2


def test_dry_run_prints_changes_and_writes_nothing(sync_mod, webdav_server, tmp_path, monkeypatch, capsys):
    state, base_url = webdav_server
    img = _make_image_bytes((2400, 1800), seed=401)
    state.collections["/Photos/"] = [
        {"href": "/Photos/a.jpg", "is_dir": False, "etag": '"a1"', "mtime": "m", "size": len(img)},
    ]
    state.files["/Photos/a.jpg"] = img

    dest = tmp_path / "absent-photos"
    monkeypatch.setenv("NEXTCLOUD_PHOTO_URL", base_url)
    monkeypatch.setenv("NEXTCLOUD_PHOTO_USER", "u")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_APP_PASSWORD", "p")
    rc = sync_mod.main(["--dest", str(dest), "--dry-run"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "add a.jpg" in captured.out
    assert not dest.exists()
    assert state.get_calls == []


def _add_remote_photo(
    state: _FakeWebDAVState,
    rel_path: str,
    data: bytes,
    *,
    etag: str,
    mtime: str = "Wed, 15 Oct 2025 12:00:00 GMT",
) -> None:
    parts = rel_path.split("/")
    col = "/Photos/"
    for folder in parts[:-1]:
        sub = f"{col}{folder}/"
        entries = state.collections.setdefault(col, [])
        if not any(e.get("is_dir") and e["href"] == sub for e in entries):
            entries.append({"href": sub, "is_dir": True, "etag": f'"dir-{folder}"'})
        col = sub
    state.collections.setdefault(col, []).append(
        {
            "href": f"/Photos/{rel_path}",
            "is_dir": False,
            "etag": etag,
            "mtime": mtime,
            "size": len(data),
        }
    )
    state.files[f"/Photos/{rel_path}"] = data


def test_five_photos_two_days_schema2_manifest_exif_beats_mtime(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    specs = [
        ("2018/a.jpg", datetime(2018, 5, 12, 10, 0, 0), 1),
        ("2018/b.jpg", datetime(2018, 5, 12, 11, 0, 0), 2),
        ("2018/c.jpg", datetime(2018, 5, 12, 12, 0, 0), 3),
        ("2018/d.jpg", datetime(2018, 7, 4, 14, 0, 0), 4),
        ("2018/e.jpg", datetime(2018, 7, 4, 15, 0, 0), 5),
    ]
    for rel_path, exif_dt, seed in specs:
        payload = _make_image_bytes((2400, 1800), seed=seed, exif_dt=exif_dt)
        _add_remote_photo(
            state,
            rel_path,
            payload,
            etag=f'"e{seed}"',
            mtime="Wed, 15 Oct 2025 12:00:00 GMT",
        )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 2
    assert manifest["count"] == len(manifest["items"]) == 5
    assert manifest["errors"] == []
    assert manifest["selection"]["photos"] == 5
    assert manifest["selection"]["admitted"] == 5
    assert manifest["selection"]["slides"] == 5
    assert manifest["selection"]["days_covered"] == 2
    assert manifest["selection"]["days_total"] == 2
    assert manifest["selection"]["undated"] == 0

    assert [item["day_order"] for item in manifest["items"]] == list(range(5))
    expected_dates = {rel: dt.date().isoformat() for rel, dt, _ in specs}
    for item in manifest["items"]:
        src = item["sources"][0]
        assert item["date"] == expected_dates[src]
        assert item["date"].startswith("2018-")
        with Image.open(dest / item["name"]) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.size == (1920, 1200)


def test_undatable_photo_appears_with_null_date(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    _add_remote_photo(
        state,
        "dated1.jpg",
        _make_image_bytes((2400, 1800), seed=11, exif_dt=datetime(2024, 3, 10, 12, 0, 0)),
        etag='"d1"',
    )
    _add_remote_photo(
        state,
        "dated2.jpg",
        _make_image_bytes((2400, 1800), seed=12, exif_dt=datetime(2024, 4, 20, 12, 0, 0)),
        etag='"d2"',
    )
    _add_remote_photo(
        state,
        "mystery.jpg",
        _make_image_bytes((2400, 1800), seed=13),
        etag='"u1"',
        mtime="Wed, 15 Oct 2025 12:00:00 GMT",
    )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 3
    assert manifest["selection"]["undated"] == 1
    by_src = {item["sources"][0]: item for item in manifest["items"]}
    assert by_src["mystery.jpg"]["date"] is None


def test_one_corrupted_payload_publishes_remaining_and_records_error(
    sync_mod, webdav_server, tmp_path, monkeypatch, capsys
):
    state, base_url = webdav_server
    for idx in range(4):
        _add_remote_photo(
            state,
            f"good{idx}.jpg",
            _make_image_bytes((2400, 1800), seed=30 + idx, exif_dt=datetime(2025, 1, 10 + idx, 12, 0, 0)),
            etag=f'"g{idx}"',
        )
    _add_remote_photo(state, "bad.jpg", b"not-a-valid-jpeg", etag='"bad"')

    dest = tmp_path / "photos"
    monkeypatch.setenv("NEXTCLOUD_PHOTO_URL", base_url)
    monkeypatch.setenv("NEXTCLOUD_PHOTO_USER", "u")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_APP_PASSWORD", "p")
    rc = sync_mod.main(["--dest", str(dest)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
    assert captured.err == ""

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 4
    assert len(manifest["errors"]) == 1
    assert manifest["errors"][0]["rel_path"] == "bad.jpg"
    assert all("bad.jpg" not in item["sources"] for item in manifest["items"])


def test_three_of_five_corrupted_aborts_and_leaves_prior_manifest_intact(
    sync_mod, webdav_server, tmp_path, monkeypatch, capsys
):
    state, base_url = webdav_server
    for idx in range(5):
        _add_remote_photo(
            state,
            f"p{idx}.jpg",
            _make_image_bytes((2400, 1800), seed=50 + idx, exif_dt=datetime(2025, 2, 10 + idx, 12, 0, 0)),
            etag=f'"v1-{idx}"',
        )

    # First check: fresh directory aborts without writing manifest.json.
    fresh_dest = tmp_path / "fresh"
    orig_files = dict(state.files)
    for idx in range(3):
        state.files[f"/Photos/p{idx}.jpg"] = b"corrupt"
    with pytest.raises(sync_mod.SyncError, match="3 of 5 photos failed"):
        sync_mod.sync_photos(base_url, "u", "p", dest_dir=fresh_dest, today=TODAY)
    assert not (fresh_dest / "manifest.json").exists()
    assert list(fresh_dest.glob("*.jpg")) == []

    # Second check: existing populated mirror remains untouched on >20% failure.
    state.files.update(orig_files)
    dest = tmp_path / "populated"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    manifest_before = (dest / "manifest.json").read_bytes()
    slides_before = {p.name: p.stat().st_mtime_ns for p in dest.glob("*.jpg")}
    assert len(slides_before) == 5

    # Re-upload 3 of 5 with new etags and corrupted payloads.
    for entry in state.collections["/Photos/"][:3]:
        rel = entry["href"].removeprefix("/Photos/")
        entry["etag"] = entry["etag"].replace("v1", "v2")
        state.files[f"/Photos/{rel}"] = b"\xff\xd8corrupted"

    monkeypatch.setenv("NEXTCLOUD_PHOTO_URL", base_url)
    monkeypatch.setenv("NEXTCLOUD_PHOTO_USER", "u")
    monkeypatch.setenv("NEXTCLOUD_PHOTO_APP_PASSWORD", "p")
    rc = sync_mod.main(["--dest", str(dest)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err.strip().startswith("sync-photos: ")
    assert (dest / "manifest.json").read_bytes() == manifest_before
    assert {p.name: p.stat().st_mtime_ns for p in dest.glob("*.jpg")} == slides_before


def test_second_run_no_changes_downloads_nothing_and_skips_render(
    sync_mod, webdav_server, tmp_path, monkeypatch
):
    state, base_url = webdav_server
    for idx in range(3):
        _add_remote_photo(
            state,
            f"s{idx}.jpg",
            _make_image_bytes((2400, 1800), seed=70 + idx, exif_dt=datetime(2025, 3, 1 + idx, 12, 0, 0)),
            etag=f'"s{idx}"',
        )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    state.get_calls.clear()

    mtimes_before = {p.name: p.stat().st_mtime_ns for p in dest.glob("*.jpg")}
    assert len(mtimes_before) == 3

    render_calls = 0
    orig_render = sync_mod.photo_format.render_slide

    def counting_render(*args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return orig_render(*args, **kwargs)

    monkeypatch.setattr(sync_mod.photo_format, "render_slide", counting_render)
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    assert state.get_calls == []
    assert render_calls == 0
    assert {p.name: p.stat().st_mtime_ns for p in dest.glob("*.jpg")} == mtimes_before


def test_single_reupload_downloads_one_and_rerenders_one(
    sync_mod, webdav_server, tmp_path, monkeypatch
):
    state, base_url = webdav_server
    for idx, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
        _add_remote_photo(
            state,
            name,
            _make_image_bytes((2400, 1800), seed=80 + idx, exif_dt=datetime(2025, 4, 1 + idx, 12, 0, 0)),
            etag=f'"e{idx}"',
        )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest1 = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    slide_by_src = {item["sources"][0]: dest / item["name"] for item in manifest1["items"]}
    a_mtime = slide_by_src["a.jpg"].stat().st_mtime_ns
    c_mtime = slide_by_src["c.jpg"].stat().st_mtime_ns

    state.get_calls.clear()
    new_b = _make_image_bytes((2400, 1800), seed=89, exif_dt=datetime(2025, 4, 2, 12, 0, 0))
    state.files["/Photos/b.jpg"] = new_b
    state.collections["/Photos/"][1]["etag"] = '"e1-new"'
    state.collections["/Photos/"][1]["size"] = len(new_b)

    render_calls = 0
    orig_render = sync_mod.photo_format.render_slide

    def counting_render(*args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return orig_render(*args, **kwargs)

    monkeypatch.setattr(sync_mod.photo_format, "render_slide", counting_render)
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    assert state.get_calls == ["/Photos/b.jpg"]
    assert render_calls == 1
    assert slide_by_src["a.jpg"].stat().st_mtime_ns == a_mtime
    assert slide_by_src["c.jpg"].stat().st_mtime_ns == c_mtime


def test_deleted_photo_prunes_cache_and_slide_keeping_cache_dir(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    for idx, name in enumerate(("a.jpg", "b.jpg", "c.jpg")):
        _add_remote_photo(
            state,
            name,
            _make_image_bytes((2400, 1800), seed=90 + idx, exif_dt=datetime(2025, 5, 1 + idx, 12, 0, 0)),
            etag=f'"e{idx}"',
        )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    cache_dir = dest / ".cache"
    c_cache_name = sync_mod.photo_cache.cache_name("c.jpg")
    assert (cache_dir / c_cache_name).is_file()
    assert (dest / c_cache_name).is_file()

    # Delete c.jpg (1/3 = 33% drop <= 50%).
    state.collections["/Photos/"] = [e for e in state.collections["/Photos/"] if e["href"] != "/Photos/c.jpg"]
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    assert cache_dir.is_dir()
    assert (cache_dir / ".cache-manifest.json").is_file()
    assert not (cache_dir / c_cache_name).exists()
    assert not (dest / c_cache_name).exists()
    render_manifest = json.loads((dest / ".render-manifest.json").read_text(encoding="utf-8"))
    assert c_cache_name not in render_manifest
    assert len(render_manifest) == 2


def test_small_landscape_yields_backdrop_kind(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    _add_remote_photo(
        state,
        "small_land.jpg",
        _make_image_bytes((1280, 960), seed=110, exif_dt=datetime(2025, 6, 1, 12, 0, 0)),
        etag='"sl1"',
    )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 1
    assert manifest["items"][0]["kind"] == "backdrop"
    with Image.open(dest / manifest["items"][0]["name"]) as out_img:
        assert out_img.size == (1920, 1200)


def test_two_portraits_yield_pair_with_earlier_date(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    _add_remote_photo(
        state,
        "trip/p_later.jpg",
        _make_image_bytes((1800, 2400), seed=120, exif_dt=datetime(2018, 6, 12, 16, 0, 0)),
        etag='"pl"',
    )
    _add_remote_photo(
        state,
        "trip/p_earlier.jpg",
        _make_image_bytes((1800, 2400), seed=121, exif_dt=datetime(2018, 6, 10, 9, 0, 0)),
        etag='"pe"',
    )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 1
    item = manifest["items"][0]
    assert item["kind"] == "pair"
    assert item["name"].startswith("pair-")
    assert set(item["sources"]) == {"trip/p_earlier.jpg", "trip/p_later.jpg"}
    assert item["date"] == "2018-06-10"
    with Image.open(dest / item["name"]) as out_img:
        assert out_img.size == (1920, 1200)


def test_dry_run_on_populated_cache_lists_no_downloads_and_writes_nothing(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    for idx in range(3):
        _add_remote_photo(
            state,
            f"p{idx}.jpg",
            _make_image_bytes((2400, 1800), seed=130 + idx, exif_dt=datetime(2025, 8, 1 + idx, 12, 0, 0)),
            etag=f'"p{idx}"',
        )

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    state.get_calls.clear()

    snapshot = {p.relative_to(dest): p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
    actions = sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, dry_run=True, today=TODAY)

    assert not any(line.startswith(("add ", "update ")) for line in actions)
    assert any(line.startswith("deck: ") for line in actions)
    assert state.get_calls == []
    assert {p.relative_to(dest): p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()} == snapshot


def test_render_failure_skips_slide_and_still_publishes(sync_mod, webdav_server, tmp_path, monkeypatch):
    state, base_url = webdav_server
    for idx in range(6):
        _add_remote_photo(
            state,
            f"r{idx}.jpg",
            _make_image_bytes((2400, 1800), seed=140 + idx, exif_dt=datetime(2025, 9, 1 + idx, 12, 0, 0)),
            etag=f'"r{idx}"',
        )

    dest = tmp_path / "photos"
    orig_render = sync_mod.photo_format.render_slide

    def failing_render(kind, images, captured_at, rel_path):
        if rel_path.endswith("r1.jpg"):
            raise OSError("simulated decode failure")
        return orig_render(kind, images, captured_at, rel_path)

    monkeypatch.setattr(sync_mod.photo_format, "render_slide", failing_render)
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)

    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["schema"] == 2
    assert [err["rel_path"] for err in manifest["errors"]] == ["r1.jpg"]
    assert "render:" in manifest["errors"][0]["error"]
    assert manifest["count"] == 5
    assert len(list(dest.glob("*.jpg"))) == 5
    assert not any("r1" in item["sources"][0] for item in manifest["items"])

    # the skipped slide is retried on the next run rather than being remembered as rendered
    monkeypatch.setattr(sync_mod.photo_format, "render_slide", orig_render)
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest, today=TODAY)
    manifest = json.loads((dest / "manifest.json").read_text())
    assert manifest["errors"] == []
    assert manifest["count"] == 6
