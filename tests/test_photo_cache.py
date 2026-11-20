"""Offline unit tests for tools/photo_cache.py."""
from __future__ import annotations

from datetime import datetime
import io
import json
import pathlib
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from conftest import load_tool

photo_select = load_tool("photo_select")
sys.modules.setdefault("photo_select", photo_select)
photo_cache = load_tool("photo_cache")


def _remote(
    rel_path: str,
    *,
    etag: str = '"e1"',
    mtime: str = "Mon, 01 Jan 2025 12:00:00 GMT",
    size: int | None = 123,
    url: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        rel_path=rel_path,
        url=url or f"https://cloud.example.test/Photos/{rel_path}",
        etag=etag,
        mtime=mtime,
        size=size,
    )


def _make_jpeg(
    size: tuple[int, int] = (400, 300),
    *,
    color: tuple[int, int, int] = (80, 140, 200),
    exif: Image.Exif | None = None,
) -> bytes:
    buf = io.BytesIO()
    img = Image.new("RGB", size, color)
    save_kwargs: dict = {"format": "JPEG"}
    if exif is not None:
        save_kwargs["exif"] = exif
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _make_rgba_png(size: tuple[int, int] = (64, 64)) -> bytes:
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    for y in range(size[1]):
        for x in range(size[0] // 2, size[0]):
            img.putpixel((x, y), (40, 180, 90, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _stub_downloader(payloads: dict[str, bytes | Exception]):
    calls: list[str] = []

    def _download(photo: SimpleNamespace) -> bytes:
        calls.append(photo.rel_path)
        value = payloads[photo.rel_path]
        if isinstance(value, Exception):
            raise value
        return value

    return _download, calls


def test_reuse_skips_download_and_preserves_local_mtime(tmp_path):
    p1 = _make_jpeg((400, 300), color=(10, 20, 30))
    p2 = _make_jpeg((500, 400), color=(200, 100, 50))
    photos = [
        _remote("a.jpg", etag='"a1"', size=len(p1)),
        _remote("b.jpg", etag='"b1"', size=len(p2)),
    ]
    dl, calls = _stub_downloader({"a.jpg": p1, "b.jpg": p2})

    first = photo_cache.refresh(photos, tmp_path, {}, download=dl)
    assert first.downloaded == 2
    assert first.reused == 0
    assert first.errors == []
    assert calls == ["a.jpg", "b.jpg"]

    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    a_path = cache_dir / first.entries["a.jpg"].local_name
    mtime_before = a_path.stat().st_mtime_ns

    calls.clear()
    prev = photo_cache.load_manifest(cache_dir)
    second = photo_cache.refresh(photos, tmp_path, prev, download=dl)
    assert calls == []
    assert second.downloaded == 0
    assert second.reused == 2
    assert second.errors == []
    assert second.entries == first.entries
    assert a_path.stat().st_mtime_ns == mtime_before


def test_etag_change_refetches_only_changed_photo(tmp_path):
    p1 = _make_jpeg((400, 300), color=(10, 20, 30))
    p2_old = _make_jpeg((400, 300), color=(40, 50, 60))
    p2_new = _make_jpeg((600, 400), color=(90, 120, 150))

    photos_v1 = [
        _remote("a.jpg", etag='"a1"', size=len(p1)),
        _remote("b.jpg", etag='"b1"', size=len(p2_old)),
    ]
    dl, calls = _stub_downloader({"a.jpg": p1, "b.jpg": p2_old})
    first = photo_cache.refresh(photos_v1, tmp_path, {}, download=dl)

    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    a_path = cache_dir / first.entries["a.jpg"].local_name
    a_mtime = a_path.stat().st_mtime_ns

    calls.clear()
    dl2, calls2 = _stub_downloader({"a.jpg": p1, "b.jpg": p2_new})
    photos_v2 = [
        _remote("a.jpg", etag='"a1"', size=len(p1)),
        _remote("b.jpg", etag='"b2"', size=len(p2_new)),
    ]
    second = photo_cache.refresh(photos_v2, tmp_path, first.entries, download=dl2)

    assert calls2 == ["b.jpg"]
    assert second.downloaded == 1
    assert second.reused == 1
    assert second.entries["b.jpg"].width == 600
    assert a_path.stat().st_mtime_ns == a_mtime


def test_version_bump_refetches_everything(tmp_path):
    p1 = _make_jpeg((300, 200))
    p2 = _make_jpeg((400, 300))
    photos = [
        _remote("a.jpg", etag='"a1"', size=len(p1)),
        _remote("b.jpg", etag='"b1"', size=len(p2)),
    ]
    dl, calls = _stub_downloader({"a.jpg": p1, "b.jpg": p2})

    first = photo_cache.refresh(photos, tmp_path, {}, download=dl, version=photo_cache.CACHE_VERSION + 1)
    assert first.downloaded == 2
    assert calls == ["a.jpg", "b.jpg"]

    calls.clear()
    prev = photo_cache.load_manifest(tmp_path / photo_cache.CACHE_SUBDIR)
    second = photo_cache.refresh(photos, tmp_path, prev, download=dl)
    assert second.downloaded == 2
    assert second.reused == 0
    assert calls == ["a.jpg", "b.jpg"]

    calls.clear()
    third = photo_cache.refresh(photos, tmp_path, second.entries, download=dl)
    assert third.downloaded == 0
    assert third.reused == 2
    assert calls == []


def test_corrupt_local_file_with_matching_metadata_is_redownloaded(tmp_path):
    p1 = _make_jpeg((400, 300))
    photos = [_remote("a.jpg", etag='"a1"', size=len(p1))]
    dl, calls = _stub_downloader({"a.jpg": p1})
    first = photo_cache.refresh(photos, tmp_path, {}, download=dl)

    local_path = tmp_path / photo_cache.CACHE_SUBDIR / first.entries["a.jpg"].local_name
    local_path.write_bytes(b"corrupted-not-a-jpeg")

    calls.clear()
    second = photo_cache.refresh(photos, tmp_path, first.entries, download=dl)
    assert calls == ["a.jpg"]
    assert second.downloaded == 1
    assert second.reused == 0
    with Image.open(local_path) as restored:
        assert restored.size == (400, 300)

    # Also catch truncated JPEG (valid SOI header, missing EOI footer).
    valid_bytes = local_path.read_bytes()
    local_path.write_bytes(valid_bytes[: len(valid_bytes) // 2])
    calls.clear()
    third = photo_cache.refresh(photos, tmp_path, second.entries, download=dl)
    assert calls == ["a.jpg"]
    assert third.downloaded == 1
    assert third.reused == 0


def test_truncated_payload_becomes_error_while_others_succeed(tmp_path):
    good = _make_jpeg((400, 300))
    photos = [
        _remote("bad.jpg", etag='"bad"', size=12),
        _remote("good.jpg", etag='"good"', size=len(good)),
    ]
    dl, _ = _stub_downloader({"bad.jpg": b"\xff\xd8\xff\xe0truncated", "good.jpg": good})

    res = photo_cache.refresh(photos, tmp_path, {}, download=dl)
    assert list(res.entries.keys()) == ["good.jpg"]
    assert res.downloaded == 1
    assert len(res.errors) == 1
    assert res.errors[0]["rel_path"] == "bad.jpg"
    assert res.errors[0]["error"]


def test_all_downloads_raising_returns_errors_and_empty_entries(tmp_path):
    photos = [_remote("a.jpg"), _remote("b.jpg")]
    dl, _ = _stub_downloader({"a.jpg": RuntimeError("timeout"), "b.jpg": OSError("reset")})

    res = photo_cache.refresh(photos, tmp_path, {}, download=dl)
    assert res.entries == {}
    assert res.downloaded == 0
    assert res.reused == 0
    assert [e["rel_path"] for e in res.errors] == ["a.jpg", "b.jpg"]


def test_extract_captured_at_exif_priority_and_filename_fallback():
    exif_both = Image.Exif()
    exif_both.get_ifd(0x8769)[36867] = "2021:06:15 14:22:33"
    exif_both[306] = "2022:01:02 03:04:05"
    payload_both = _make_jpeg(exif=exif_both)
    assert photo_cache.extract_captured_at(
        payload_both, name_hint="0123456789-25-12-31_23-59-59.jpg"
    ) == datetime(2021, 6, 15, 14, 22, 33)

    exif_ifd0_only = Image.Exif()
    exif_ifd0_only[306] = "2020:03:04 05:06:07"
    assert photo_cache.extract_captured_at(_make_jpeg(exif=exif_ifd0_only)) == datetime(
        2020, 3, 4, 5, 6, 7
    )

    plain_payload = _make_jpeg()
    assert photo_cache.extract_captured_at(
        plain_payload, name_hint="deadbeef01-19-08-23_07-45-12_1.jpg"
    ) == datetime(2019, 8, 23, 7, 45, 12)

    assert photo_cache.extract_captured_at(plain_payload, name_hint="deadbeef01-holiday.jpg") is None
    assert photo_cache.extract_captured_at(b"not-an-image", name_hint="no-timestamp.jpg") is None


def test_webdav_mtime_is_never_used_as_captured_at(tmp_path):
    plain = _make_jpeg((400, 300))
    photos = [_remote("2018/picnic.jpg", mtime="Tue, 14 Oct 2025 09:00:00 GMT", size=len(plain))]
    dl, _ = _stub_downloader({"2018/picnic.jpg": plain})

    res = photo_cache.refresh(photos, tmp_path, {}, download=dl)
    assert res.entries["2018/picnic.jpg"].captured_at is None


def test_normalise_geometry_and_no_upscale():
    land_img, lw, lh = photo_cache.normalise(_make_jpeg((4032, 3024)))
    assert (lw, lh) == (1920, 1440)
    assert land_img.size == (1920, 1440)

    port_img, pw, ph = photo_cache.normalise(_make_jpeg((3024, 4032)))
    assert (pw, ph) == (1440, 1920)
    assert port_img.size == (1440, 1920)

    small_img, sw, sh = photo_cache.normalise(_make_jpeg((800, 600)))
    assert (sw, sh) == (800, 600)
    assert small_img.size == (800, 600)


def test_normalise_applies_exif_orientation():
    exif = Image.Exif()
    exif[274] = 6  # Orientation: 90 degrees CW needed to display upright
    raw = _make_jpeg((4032, 3024), exif=exif)
    img, width, height = photo_cache.normalise(raw)
    assert (width, height) == (1440, 1920)
    assert img.size == (1440, 1920)


def test_normalise_rgba_png_flattens_to_rgb_on_black(tmp_path):
    png_bytes = _make_rgba_png((64, 64))
    img, width, height = photo_cache.normalise(png_bytes)
    assert img.mode == "RGB"
    assert (width, height) == (64, 64)
    assert img.getpixel((4, 4)) == (0, 0, 0)
    assert img.getpixel((48, 4)) == (40, 180, 90)

    photos = [_remote("shot.png", size=len(png_bytes))]
    dl, _ = _stub_downloader({"shot.png": png_bytes})
    res = photo_cache.refresh(photos, tmp_path, {}, download=dl)
    assert res.errors == []
    assert res.entries["shot.png"].width == 64


def test_no_temp_files_left_after_success_or_failure(tmp_path):
    good = _make_jpeg((400, 300))
    photos = [_remote("ok.jpg", size=len(good)), _remote("bad.jpg", size=5)]
    dl, _ = _stub_downloader({"ok.jpg": good, "bad.jpg": b"bad"})

    photo_cache.refresh(photos, tmp_path, {}, download=dl)
    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    leftovers = [
        p.name
        for p in cache_dir.iterdir()
        if p.name.endswith(".tmp") or p.name.startswith(".dl-")
    ]
    assert leftovers == []


def test_cache_name_prevents_path_traversal_and_collisions(tmp_path):
    evil = "../../etc/passwd"
    name = photo_cache.cache_name(evil)
    assert "/" not in name
    assert "\\" not in name
    assert name.endswith("-passwd.jpg")
    assert photo_cache.cache_name("..") != photo_cache.cache_name("a/..")
    assert photo_cache.cache_name("..").endswith("-photo.jpg")
    assert photo_cache.cache_name("2025/same.jpg") != photo_cache.cache_name("2026/same.jpg")

    payload = _make_jpeg((200, 200))
    dl, _ = _stub_downloader({evil: payload})
    res = photo_cache.refresh([_remote(evil, size=len(payload))], tmp_path, {}, download=dl)
    cache_dir = (tmp_path / photo_cache.CACHE_SUBDIR).resolve()
    saved = (cache_dir / res.entries[evil].local_name).resolve()
    assert saved.parent == cache_dir
    assert saved.is_file()


def test_removed_photo_deleted_and_unrelated_files_untouched(tmp_path):
    p1 = _make_jpeg((200, 200))
    p2 = _make_jpeg((200, 200))
    photos = [_remote("keep.jpg", size=len(p1)), _remote("gone.jpg", size=len(p2))]
    dl, _ = _stub_downloader({"keep.jpg": p1, "gone.jpg": p2})
    first = photo_cache.refresh(photos, tmp_path, {}, download=dl)

    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    stray_txt = cache_dir / "notes.txt"
    stray_jpg = cache_dir / "stray.jpg"
    stray_txt.write_text("keep", encoding="utf-8")
    stray_jpg.write_bytes(b"stray")

    gone_name = first.entries["gone.jpg"].local_name
    second = photo_cache.refresh([photos[0]], tmp_path, first.entries, download=dl)

    assert second.removed == [gone_name]
    assert not (cache_dir / gone_name).exists()
    assert (cache_dir / first.entries["keep.jpg"].local_name).is_file()
    assert stray_txt.read_text(encoding="utf-8") == "keep"
    assert stray_jpg.read_bytes() == b"stray"


def test_manifest_round_trip_preserves_entries(tmp_path):
    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    entries = {
        "a.jpg": photo_cache.CacheEntry(
            rel_path="a.jpg",
            local_name="0123456789-a.jpg",
            etag='"e1"',
            mtime="Mon, 01 Jan 2025 00:00:00 GMT",
            size=100,
            width=1920,
            height=1440,
            captured_at=datetime(2024, 5, 6, 7, 8, 9),
            luma=123.5,
            sharpness=45.25,
            dhash=0x123456789ABCDEF0,
        ),
        "b.jpg": photo_cache.CacheEntry(
            rel_path="b.jpg",
            local_name="abcdef0123-b.jpg",
            etag='"e2"',
            mtime="Tue, 02 Jan 2025 00:00:00 GMT",
            size=None,
            width=800,
            height=600,
            captured_at=None,
            luma=64.0,
            sharpness=12.0,
            dhash=0,
        ),
    }

    photo_cache.save_manifest(cache_dir, entries)
    loaded = photo_cache.load_manifest(cache_dir)
    assert loaded == entries


@pytest.mark.parametrize(
    "raw_content",
    [
        None,
        "{not valid json",
        "[]",
        json.dumps({"version": 2, "entries": "not-a-dict"}),
        json.dumps({"version": 2, "entries": {"a.jpg": {"local_name": "only-one-field"}}}),
        json.dumps(
            {
                "version": 2,
                "entries": {
                    "a.jpg": {
                        "local_name": "x.jpg",
                        "etag": '"e"',
                        "mtime": "m",
                        "size": 1,
                        "width": 10,
                        "height": 10,
                        "captured_at": "not-an-iso-date",
                        "luma": 1.0,
                        "sharpness": 1.0,
                        "dhash": 1,
                    }
                },
            }
        ),
    ],
)
def test_unparsable_or_missing_manifest_loads_as_empty_dict(tmp_path, raw_content):
    cache_dir = tmp_path / photo_cache.CACHE_SUBDIR
    cache_dir.mkdir(parents=True)
    if raw_content is not None:
        (cache_dir / photo_cache.CACHE_MANIFEST_NAME).write_text(raw_content, encoding="utf-8")
    assert photo_cache.load_manifest(cache_dir) == {}


def test_entries_sorted_and_metrics_match_photo_select(tmp_path):
    img = Image.new("RGB", (320, 240))
    pixels = [((x * 7 + y * 3) % 256, (y * 5) % 256, (x * 11) % 256) for y in range(240) for x in range(320)]
    img.putdata(pixels)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    payload = buf.getvalue()

    photos = [_remote("c.jpg"), _remote("a.jpg"), _remote("b.jpg")]
    dl, _ = _stub_downloader({"a.jpg": payload, "b.jpg": payload, "c.jpg": payload})
    res = photo_cache.refresh(photos, tmp_path, {}, download=dl)

    assert list(res.entries.keys()) == ["a.jpg", "b.jpg", "c.jpg"]
    norm_img, _, _ = photo_cache.normalise(payload)
    entry = res.entries["a.jpg"]
    assert entry.luma == pytest.approx(photo_select.mean_luma(norm_img))
    assert entry.sharpness == pytest.approx(photo_select.sharpness(norm_img))
    assert entry.dhash == photo_select.dhash(norm_img)


def test_http_downloader_streams_chunks_and_raises_on_non_200():
    class _FakeResp:
        def __init__(self, status_code: int, chunks: list[bytes]):
            self.status_code = status_code
            self._chunks = chunks

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_content(self, chunk_size: int):
            assert chunk_size == 65536
            return iter(self._chunks)

    class _FakeSession:
        def __init__(self, status_code: int, chunks: list[bytes]):
            self.status_code = status_code
            self.chunks = chunks
            self.calls: list[tuple] = []

        def get(self, url: str, *, stream: bool, timeout):
            self.calls.append((url, stream, timeout))
            return _FakeResp(self.status_code, self.chunks)

    ok_session = _FakeSession(200, [b"abc", b"", b"def"])
    dl_ok = photo_cache.http_downloader(ok_session)
    assert dl_ok(_remote("a.jpg")) == b"abcdef"
    assert ok_session.calls == [("https://cloud.example.test/Photos/a.jpg", True, photo_cache.REQUEST_TIMEOUT)]

    err_session = _FakeSession(503, [])
    dl_err = photo_cache.http_downloader(err_session)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        dl_err(_remote("a.jpg"))
