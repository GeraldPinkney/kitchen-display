"""Static contract specs for kiosk/photo.html and tests/fixtures/photo_manifest.json."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.visible_text: list[str] = []
        self._in_ignored = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag.lower(), dict(attrs)))
        if tag.lower() in ("style", "script", "title"):
            self._in_ignored += 1

    def handle_endtag(self, tag):
        if tag.lower() in ("style", "script", "title") and self._in_ignored > 0:
            self._in_ignored -= 1

    def handle_data(self, data):
        if self._in_ignored == 0 and data.strip():
            self.visible_text.append(data.strip())


def test_photo_page_is_self_contained_with_no_external_assets(repo_root):
    html_path = repo_root / "kiosk" / "photo.html"
    assert html_path.is_file()
    body = html_path.read_text(encoding="utf-8")

    parser = _PageParser()
    parser.feed(body)

    # No external stylesheets, scripts, fonts, or visible text chrome.
    for tag, attrs in parser.tags:
        if tag == "script":
            assert "src" not in attrs, f"external script disallowed: {attrs}"
        if tag == "link":
            raise AssertionError(f"external <link> disallowed: {attrs}")
    assert parser.visible_text == [], f"unexpected visible text chrome: {parser.visible_text}"

    # Only external URL literal permitted is the dashboard fallback URL.
    urls = re.findall(r"https?://[^\s'\"<>]+", body)
    assert urls == ["http://192.168.178.84/kitchen-display/home?kiosk"], urls


def test_photo_page_styling_is_full_bleed_black_contain_no_cursor(repo_root):
    body = (repo_root / "kiosk" / "photo.html").read_text(encoding="utf-8")
    assert "background: #000" in body
    assert "object-fit: contain" in body
    assert "cursor: none" in body
    transition_match = re.search(r"transition:\s*([^;]+);", body)
    assert transition_match is not None
    assert "opacity" in transition_match.group(1)
    assert "1200" in transition_match.group(1)
    assert "0.8s" not in body


def test_photo_page_logic_covers_manifest_order_preload_error_skip_and_fallback(repo_root):
    body = (repo_root / "kiosk" / "photo.html").read_text(encoding="utf-8")
    assert "photos/manifest.json" in body
    assert "location.replace" in body
    assert '"return"' in body
    assert "30000" in body
    assert "new Image()" in body
    assert "onerror" in body
    assert "Math.random()" not in body
    assert "items[index]" in body
    assert "% items.length" in body


def test_photo_page_persists_and_resumes_index_via_local_storage(repo_root):
    body = (repo_root / "kiosk" / "photo.html").read_text(encoding="utf-8")
    assert "localStorage" in body
    assert '"kd-photo-index"' in body
    assert re.search(
        r"try\s*\{(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*localStorage\.getItem\([^)]*\)(?:[^{}]|\{(?:[^{}]|\{[^{}]*\})*\})*\}\s*catch",
        body,
        re.DOTALL,
    )
    assert re.search(
        r"try\s*\{[^{}]*localStorage\.setItem\([^)]*\)[^{}]*\}\s*catch",
        body,
        re.DOTALL,
    )
    assert re.search(r"let\s+index\s*=\s*[^;]*%\s*items\.length\s*;", body)


def test_photo_page_returns_to_the_dashboard_on_first_touch(repo_root):
    """The compositor's swayidle resume does not fire for touch on this panel, so the
    page must navigate back itself: document-level listeners, guarded from a bounce
    immediately after load, using location.replace so the frame is not in history."""
    body = (repo_root / "kiosk" / "photo.html").read_text(encoding="utf-8")

    for event_type in ("pointerdown", "touchstart", "mousedown", "keydown"):
        assert f'"{event_type}"' in body, event_type
    assert "document.addEventListener(type, leave" in body
    assert "LEAVE_GUARD_MS" in body
    assert "window.location.replace(getReturnUrl())" in body


def test_photo_page_cache_busts_slide_urls_with_generated_at(repo_root):
    body = (repo_root / "kiosk" / "photo.html").read_text(encoding="utf-8")
    assert '"?v="' in body
    assert "encodeURIComponent(data.generated_at)" in body
    assert '"photos/" + encodeURIComponent(item.name) + versionQuery' in body


def test_photo_manifest_fixture_matches_contract(repo_root):
    manifest = json.loads((repo_root / "tests" / "fixtures" / "photo_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == 2
    assert manifest["count"] == len(manifest["items"])
    assert isinstance(manifest["generated_at"], str) and "T" in manifest["generated_at"]
    assert isinstance(manifest["origin"], str) and manifest["origin"]
    assert isinstance(manifest["skipped_heic"], int) and not isinstance(manifest["skipped_heic"], bool)
    assert manifest["skipped_heic"] >= 0
    assert isinstance(manifest["errors"], list)

    selection = manifest["selection"]
    assert isinstance(selection, dict)
    assert set(selection) == {
        "photos",
        "admitted",
        "rejected",
        "slides",
        "days_covered",
        "days_total",
        "undated",
    }
    for key in ("photos", "admitted", "slides", "days_covered", "days_total", "undated"):
        assert isinstance(selection[key], int) and not isinstance(selection[key], bool)
        assert selection[key] >= 0

    rejected = selection["rejected"]
    assert isinstance(rejected, dict)
    assert set(rejected) == {
        "too_small",
        "aspect",
        "screenshot",
        "duplicate",
        "day_ceiling",
        "month_share",
        "cap",
    }
    for count in rejected.values():
        assert isinstance(count, int) and not isinstance(count, bool)
        assert count >= 0

    for expected_day_order, item in enumerate(manifest["items"]):
        assert set(item) == {"name", "kind", "date", "day_order", "sources"}
        assert isinstance(item["name"], str) and item["name"].endswith(".jpg")
        assert item["kind"] in {"single", "pair", "backdrop"}
        assert item["date"] is None or (isinstance(item["date"], str) and len(item["date"]) > 0)
        assert isinstance(item["day_order"], int) and not isinstance(item["day_order"], bool)
        assert item["day_order"] == expected_day_order
        assert isinstance(item["sources"], list)
        expected_sources_len = 2 if item["kind"] == "pair" else 1
        assert len(item["sources"]) == expected_sources_len
        assert all(isinstance(src, str) and len(src) > 0 for src in item["sources"])
