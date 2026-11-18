"""Offline unit tests for tools/photo_format.py."""
from __future__ import annotations

from datetime import datetime
import random
import re

from PIL import Image, ImageDraw, ImageStat
import pytest

from conftest import load_tool

photo_format = load_tool("photo_format")


def _noise_image(width: int, height: int, *, seed: int = 42) -> Image.Image:
    rng = random.Random(seed)
    row_bytes = rng.randbytes(width * 3)
    # Vary each row slightly using a fast cyclic shift so the JPEG cannot trivially RLE-compress it.
    chunks = [row_bytes[(i * 17) % (width * 3) :] + row_bytes[: (i * 17) % (width * 3)] for i in range(height)]
    return Image.frombytes("RGB", (width, height), b"".join(chunks))


def _striped_image(width: int, height: int, *, stripe_width: int = 4) -> Image.Image:
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    for x in range(0, width, stripe_width * 2):
        draw.rectangle((x, 0, min(width - 1, x + stripe_width - 1), height - 1), fill=(80, 80, 80))
    return img


def test_render_slide_every_kind_returns_1920x1200_rgb():
    landscape = Image.new("RGB", (2400, 1600), (80, 120, 160))
    portrait_a = Image.new("RGBA", (1200, 1600), (180, 60, 40, 255))
    portrait_b = Image.new("L", (1200, 1600), 140)
    small = Image.new("RGB", (1200, 900), (90, 150, 110))
    ts = datetime(2024, 5, 12, 10, 30, 0)

    for kind, imgs in (
        ("single", [landscape]),
        ("pair", [portrait_a, portrait_b]),
        ("backdrop", [small]),
    ):
        slide = photo_format.render_slide(kind, imgs, ts, "2024/05/photo.jpg")
        assert slide.size == (1920, 1200)
        assert slide.mode == "RGB"


def test_render_slide_validation_errors():
    img = Image.new("RGB", (1920, 1200), (10, 20, 30))
    with pytest.raises(ValueError):
        photo_format.render_slide("unknown", [img], None, "a.jpg")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        photo_format.render_slide("single", [img, img], None, "a.jpg")
    with pytest.raises(ValueError):
        photo_format.render_slide("backdrop", [], None, "a.jpg")
    with pytest.raises(ValueError):
        photo_format.render_slide("pair", [img], None, "a.jpg")


def test_to_rgb_transparent_modes_composite_on_black():
    rgba = Image.new("RGBA", (4, 4), (200, 100, 50, 0))
    rgba.putpixel((1, 1), (200, 100, 50, 255))
    rgb_from_rgba = photo_format.to_rgb(rgba)
    assert rgb_from_rgba.mode == "RGB"
    assert rgb_from_rgba.getpixel((0, 0)) == (0, 0, 0)
    assert rgb_from_rgba.getpixel((1, 1)) == (200, 100, 50)

    la = Image.new("LA", (4, 4), (200, 0))
    la.putpixel((2, 2), (200, 255))
    rgb_from_la = photo_format.to_rgb(la)
    assert rgb_from_la.getpixel((0, 0)) == (0, 0, 0)
    assert rgb_from_la.getpixel((2, 2)) == (200, 200, 200)

    pal = Image.new("P", (4, 4), 0)
    pal.putpalette([255, 0, 0, 0, 255, 0] + [0] * (254 * 3))
    pal.info["transparency"] = 0
    pal.putpixel((1, 2), 1)
    rgb_from_p = photo_format.to_rgb(pal)
    assert rgb_from_p.getpixel((0, 0)) == (0, 0, 0)
    assert rgb_from_p.getpixel((1, 2)) == (0, 255, 0)


def test_cover_box_42_percent_vertical_bias():
    box = photo_format.cover_box((4032, 3024), (1920, 1200), photo_format.COVER_CENTRE)
    left, top, right, bottom = box
    assert (left, right) == (0, 4032)
    assert bottom - top == 2520
    wasted_h = 3024 - 2520
    assert top == round(wasted_h * 0.42)
    assert top < wasted_h // 2


def test_cover_box_bounds_and_extremes():
    sources = [(4032, 3024), (3024, 4032), (800, 600), (4000, 1500)]
    centrings = [(0.0, 0.0), (1.0, 1.0), (0.5, 0.42), (-0.5, 1.5)]
    for src_w, src_h in sources:
        for centring in centrings:
            box = photo_format.cover_box((src_w, src_h), (1920, 1200), centring)
            assert all(isinstance(v, int) for v in box)
            left, top, right, bottom = box
            assert 0 <= left < right <= src_w
            assert 0 <= top < bottom <= src_h


def test_cover_preserves_upper_subject_with_42_percent_bias():
    src = Image.new("RGB", (4032, 3024), (0, 200, 0))
    draw = ImageDraw.Draw(src)
    # 250 top rows are red: 42% crop starts at y=212 (keeps red), whereas 50% starts at y=252 (drops all red).
    draw.rectangle((0, 0, 4031, 249), fill=(220, 0, 0))
    draw.rectangle((0, 2770, 4031, 3023), fill=(0, 0, 220))
    out = photo_format.cover(src)
    top_pixel = out.getpixel((960, 0))
    assert top_pixel[0] > 150 and top_pixel[1] < 50


def test_diptych_gutter_and_half_placement():
    left_img = Image.new("RGB", (1200, 1600), (210, 20, 20))
    right_img = Image.new("RGB", (1200, 1600), (20, 20, 210))
    out = photo_format.diptych(left_img, right_img)

    gutter = out.crop((956, 0, 964, 1200))
    assert ImageStat.Stat(gutter).extrema == [(0, 0), (0, 0), (0, 0)]
    assert out.getpixel((100, 600)) == (210, 20, 20)
    assert out.getpixel((955, 600)) == (210, 20, 20)
    assert out.getpixel((964, 600)) == (20, 20, 210)
    assert out.getpixel((1800, 600)) == (20, 20, 210)

    swapped = photo_format.diptych(right_img, left_img)
    assert swapped.getpixel((100, 600)) == (20, 20, 210)
    assert swapped.getpixel((1800, 600)) == (210, 20, 20)


def test_backdrop_subject_height_dimming_and_blur():
    solid = Image.new("RGB", (1200, 900), (220, 220, 220))
    out_solid = photo_format.backdrop(solid)
    bright_rows = sum(1 for y in range(1200) if out_solid.getpixel((960, y))[0] >= 200)
    assert bright_rows == round(1200 * 0.70)
    assert out_solid.getpixel((0, 0))[0] < out_solid.getpixel((960, 600))[0]

    striped = _striped_image(1200, 900, stripe_width=4)
    src_var = ImageStat.Stat(striped).var[0]
    out_striped = photo_format.backdrop(striped)
    border_var = ImageStat.Stat(out_striped.crop((0, 0, 350, 1200))).var[0]
    assert border_var < src_var * 0.1


def test_place_name_rules():
    assert photo_format.place_name(".") is None
    assert photo_format.place_name("photo.jpg") is None
    assert photo_format.place_name("2026/09/photo.jpg") is None
    assert photo_format.place_name("2026/photo.jpg") is None
    assert photo_format.place_name("Photos/Cornwall/a.jpg") == "Cornwall"
    assert photo_format.place_name("Cornwall 2024/a.jpg") == "Cornwall 2024"
    assert photo_format.place_name("DCIM/a.jpg") is None
    assert photo_format.place_name("dcim/a.jpg") is None
    assert photo_format.place_name("Camera/a.jpg") is None
    assert photo_format.place_name("Screenshots/a.jpg") is None


def test_caption_lines_formatting():
    assert photo_format.caption_lines(None, "Photos/Cornwall/a.jpg") == ()
    assert photo_format.caption_lines(datetime(2024, 5, 1), "2024/05/a.jpg") == ("1 May 2024",)
    assert photo_format.caption_lines(datetime(2024, 5, 12), "2024/05/a.jpg") == ("12 May 2024",)
    assert photo_format.caption_lines(datetime(2024, 5, 12), "Photos/Cornwall/a.jpg") == ("12 May 2024", "Cornwall")


def test_draw_caption_empty_lines_noop():
    canvas = Image.new("RGB", (1920, 1200), (123, 45, 67))
    out = photo_format.draw_caption(canvas, ())
    assert out.tobytes() == canvas.tobytes()


def test_draw_caption_scrim_and_text_visibility():
    white = Image.new("RGB", (1920, 1200), (255, 255, 255))
    out_white = photo_format.draw_caption(white, ("12 May 2024",))
    assert out_white.getpixel((960, 0)) == (255, 255, 255)
    assert out_white.getpixel((960, 1199))[0] < 255

    black = Image.new("RGB", (1920, 1200), (0, 0, 0))
    out_black = photo_format.draw_caption(black, ("12 May 2024",))
    caption_area = out_black.crop((40, 1050, 500, 1160))
    assert ImageStat.Stat(caption_area).extrema[0][1] > 200


def test_draw_caption_shadow_darker_than_scrim():
    white = Image.new("RGB", (1920, 1200), (255, 255, 255))
    out = photo_format.draw_caption(white, ("12 May 2024",))
    scrim_only_min = round(255 * (1 - photo_format.SCRIM_MAX_ALPHA / 255))
    caption_band = out.crop((0, 1000, 1920, 1200))
    darkest = ImageStat.Stat(caption_band).extrema[0][0]
    assert darkest < scrim_only_min - 15


def test_draw_caption_missing_font_fallback(monkeypatch):
    monkeypatch.setattr(photo_format, "CAPTION_FONT_PATH", "/nonexistent/missing-font.ttf")
    black = Image.new("RGB", (1920, 1200), (0, 0, 0))
    out = photo_format.draw_caption(black, ("12 May 2024",))
    caption_area = out.crop((40, 1050, 500, 1160))
    assert ImageStat.Stat(caption_area).extrema[0][1] > 200


def test_render_slide_pair_draws_caption_once():
    left = Image.new("RGB", (1200, 1600), (0, 0, 0))
    right = Image.new("RGB", (1200, 1600), (0, 0, 0))
    out = photo_format.render_slide("pair", [left, right], datetime(2024, 5, 12), "2024/05/a.jpg")
    left_caption = out.crop((40, 1050, 500, 1160))
    right_half_same_spot = out.crop((964 + 40, 1050, 964 + 500, 1160))
    assert ImageStat.Stat(left_caption).extrema[0][1] > 200
    assert ImageStat.Stat(right_half_same_spot).extrema[0][1] == 0


def test_output_name_single_backdrop_and_pair():
    assert photo_format.output_name("single", ["2026/09/IMG_1234.HEIC"]) == "IMG_1234.jpg"
    assert photo_format.output_name("backdrop", ["photo.png"]) == "photo.jpg"

    pair_ab = photo_format.output_name("pair", ["2026/09/a.jpg", "2026/09/b.jpg"])
    pair_ba = photo_format.output_name("pair", ["2026/09/b.jpg", "2026/09/a.jpg"])
    assert pair_ab == pair_ba
    assert re.match(r"^pair-[0-9a-f]{12}-[0-9a-f]{12}\.jpg$", pair_ab)


def test_save_jpeg_format_metadata_and_determinism(tmp_path):
    img = _striped_image(1920, 1200, stripe_width=8)
    img.info["exif"] = b"Exif\x00\x00dummy"
    path_a = tmp_path / "a.jpg"
    path_b = tmp_path / "b.jpg"

    size_a = photo_format.save_jpeg(img, path_a)
    size_b = photo_format.save_jpeg(img, path_b)

    assert size_a == path_a.stat().st_size
    assert size_a == size_b
    assert path_a.read_bytes() == path_b.read_bytes()
    with Image.open(path_a) as opened:
        assert opened.format == "JPEG"
        assert opened.size == (1920, 1200)
        assert opened.info.get("progressive") == 1
        assert "exif" not in opened.info
        assert "icc_profile" not in opened.info


def test_save_jpeg_quality_ladder_fallback(tmp_path, monkeypatch):
    # Upscaled 2x2 noise produces ~500 KB at q82 (below 1.2 MB) and shrinks significantly at q72.
    noisy = _noise_image(960, 600, seed=7).resize((1920, 1200), Image.Resampling.NEAREST)
    monkeypatch.setattr(photo_format, "JPEG_MAX_BYTES", 10_000_000)
    q82_size = photo_format.save_jpeg(noisy, tmp_path / "probe.jpg")

    limit = q82_size - 1
    monkeypatch.setattr(photo_format, "JPEG_MAX_BYTES", limit)
    fallback_path = tmp_path / "fallback.jpg"
    written = photo_format.save_jpeg(noisy, fallback_path)

    assert written <= limit
    assert written == fallback_path.stat().st_size
    with Image.open(fallback_path) as opened:
        assert opened.format == "JPEG"
        assert opened.mode == "RGB"
        assert opened.size == (1920, 1200)
