"""Offline specs for tools/sync_photos.py using a local HTTP WebDAV fake."""
from __future__ import annotations

from functools import partial
import http.server
import io
import json
import os
import pathlib
import socketserver
import threading
import xml.etree.ElementTree as ET

from PIL import Image
import pytest

from conftest import load_tool


def _make_image_bytes(size: tuple[int, int], fmt: str = "JPEG", mode: str = "RGB", color=(120, 45, 200)) -> bytes:
    buf = io.BytesIO()
    if mode == "RGBA":
        img = Image.new("RGBA", size, (*color[:3], 128))
    else:
        img = Image.new(mode, size, color)
    img.save(buf, format=fmt)
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
    landscape = _make_image_bytes((2400, 1800), fmt="JPEG")
    portrait = _make_image_bytes((1800, 2400), fmt="PNG", mode="RGBA")
    tiny = _make_image_bytes((8, 8), fmt="WEBP")

    state.collections["/Photos/"] = [
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d1"'},
        {"href": "/Photos/root.jpg", "is_dir": False, "etag": '"e1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(landscape)},
        {"href": "/Photos/skipme.HEIC", "is_dir": False, "etag": '"h1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": 100},
        {"href": "/Photos/readme.txt", "is_dir": False, "etag": '"t1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": 10},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/portrait.png", "is_dir": False, "etag": '"e2"', "mtime": "Tue, 02 Jan 2026 00:00:00 GMT", "size": len(portrait)},
        {"href": "/Photos/2026/tiny.webp", "is_dir": False, "etag": '"e3"', "mtime": "Wed, 03 Jan 2026 00:00:00 GMT", "size": len(tiny)},
    ]
    state.files["/Photos/root.jpg"] = landscape
    state.files["/Photos/2026/portrait.png"] = portrait
    state.files["/Photos/2026/tiny.webp"] = tiny

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "kiosk", "secret", dest_dir=dest)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""

    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["count"] == 3
    assert manifest["skipped_heic"] == 1
    assert manifest["origin"] == "127.0.0.1"
    assert "T" in manifest["generated_at"]
    by_suffix = {item["name"].split("-", 1)[1]: item for item in manifest["items"]}
    assert set(by_suffix) == {"root.jpg", "portrait.jpg", "tiny.jpg"}

    # Landscape 2400x1800 -> fits 1920x1200 => 1600x1200
    assert (by_suffix["root.jpg"]["w"], by_suffix["root.jpg"]["h"]) == (1600, 1200)
    # Portrait 1800x2400 -> fits 1920x1200 => 900x1200
    assert (by_suffix["portrait.jpg"]["w"], by_suffix["portrait.jpg"]["h"]) == (900, 1200)
    # Small 8x8 -> never upscaled
    assert (by_suffix["tiny.jpg"]["w"], by_suffix["tiny.jpg"]["h"]) == (8, 8)

    for item in manifest["items"]:
        with Image.open(dest / item["name"]) as out_img:
            assert out_img.format == "JPEG"
            assert out_img.size == (item["w"], item["h"])
            assert max(out_img.size) <= 1920
            assert min(out_img.size) <= 1200


def test_incremental_sync_skips_unchanged_and_refetches_changed(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    img1 = _make_image_bytes((16, 12))
    img2 = _make_image_bytes((20, 10))
    state.collections["/Photos/"] = [
        {"href": "/Photos/a.jpg", "is_dir": False, "etag": '"a1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(img1)},
        {"href": "/Photos/b.jpg", "is_dir": False, "etag": '"b1"', "mtime": "Mon, 01 Jan 2026 00:00:00 GMT", "size": len(img2)},
    ]
    state.files["/Photos/a.jpg"] = img1
    state.files["/Photos/b.jpg"] = img2

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
    assert len(state.get_calls) == 2

    state.get_calls.clear()
    mirror = json.loads((dest / ".mirror-manifest.json").read_text(encoding="utf-8"))
    a_file = dest / mirror["a.jpg"]["local_name"]
    mtime_before = a_file.stat().st_mtime_ns

    # Second run: identical metadata -> zero GETs and file untouched.
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
    assert state.get_calls == []
    assert a_file.stat().st_mtime_ns == mtime_before

    # Update etag on b.jpg only -> only b.jpg is re-fetched.
    new_b = _make_image_bytes((32, 16))
    state.files["/Photos/b.jpg"] = new_b
    state.collections["/Photos/"][1]["etag"] = '"b2"'
    state.collections["/Photos/"][1]["size"] = len(new_b)

    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
    assert state.get_calls == ["/Photos/b.jpg"]
    assert a_file.stat().st_mtime_ns == mtime_before


def test_collision_free_names_and_prune_removed(sync_mod, webdav_server, tmp_path):
    state, base_url = webdav_server
    img = _make_image_bytes((8, 8))
    state.collections["/Photos/"] = [
        {"href": "/Photos/2025/", "is_dir": True, "etag": '"d25"'},
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d26"'},
        {"href": "/Photos/keep.jpg", "is_dir": False, "etag": '"k1"', "mtime": "m", "size": len(img)},
    ]
    state.collections["/Photos/2025/"] = [
        {"href": "/Photos/2025/same.jpg", "is_dir": False, "etag": '"s25"', "mtime": "m", "size": len(img)},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/same.jpg", "is_dir": False, "etag": '"s26"', "mtime": "m", "size": len(img)},
    ]
    state.files["/Photos/keep.jpg"] = img
    state.files["/Photos/2025/same.jpg"] = img
    state.files["/Photos/2026/same.jpg"] = img

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    names = [item["name"] for item in manifest["items"]]
    assert len(names) == 3
    assert len(set(names)) == 3  # 2025/same.jpg and 2026/same.jpg do not collide

    # Remove 2025/same.jpg (drop is 1/3 = 33% <= 50%, so prune proceeds).
    state.collections["/Photos/2025/"] = []
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
    manifest2 = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert manifest2["count"] == 2
    jpgs_on_disk = {p.name for p in dest.glob("*.jpg")}
    assert jpgs_on_disk == {item["name"] for item in manifest2["items"]}


def test_transient_drop_over_50_percent_aborts_before_delete(sync_mod, webdav_server, tmp_path, monkeypatch, capsys):
    state, base_url = webdav_server
    img = _make_image_bytes((8, 8))
    state.collections["/Photos/"] = [
        {"href": f"/Photos/p{i}.jpg", "is_dir": False, "etag": f'"{i}"', "mtime": "m", "size": len(img)}
        for i in range(4)
    ]
    for i in range(4):
        state.files[f"/Photos/p{i}.jpg"] = img

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
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
    # All 4 existing photos and manifest must remain intact.
    assert len(list(dest.glob("*.jpg"))) == 4
    assert json.loads((dest / "manifest.json").read_text(encoding="utf-8"))["count"] == 4


def test_propfind_failure_aborts_before_prune(sync_mod, webdav_server, tmp_path, monkeypatch, capsys):
    state, base_url = webdav_server
    img = _make_image_bytes((8, 8))
    state.collections["/Photos/"] = [
        {"href": "/Photos/2026/", "is_dir": True, "etag": '"d1"'},
        {"href": "/Photos/a.jpg", "is_dir": False, "etag": '"a1"', "mtime": "m", "size": len(img)},
    ]
    state.collections["/Photos/2026/"] = [
        {"href": "/Photos/2026/b.jpg", "is_dir": False, "etag": '"b1"', "mtime": "m", "size": len(img)},
    ]
    state.files["/Photos/a.jpg"] = img
    state.files["/Photos/2026/b.jpg"] = img

    dest = tmp_path / "photos"
    sync_mod.sync_photos(base_url, "u", "p", dest_dir=dest)
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
    img = _make_image_bytes((8, 8))
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
