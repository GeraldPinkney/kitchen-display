"""Pure slide rendering pass: cropping, backdrop, diptych, captions, and JPEG encoding."""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import hashlib
import os
import pathlib
from pathlib import PurePosixPath
import re
from typing import Literal

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

CANVAS = (1920, 1200)
DIPTYCH_GUTTER = 8
DIPTYCH_HALF = 956                  # (1920 - 8) // 2 — 960+8+960 = 1928 is the spec bug this fixes
COVER_MIN_ASPECT = 1.25
COVER_MAX_ASPECT = 1.85
COVER_CENTRE = (0.5, 0.42)          # 42% from the top: heads survive losing sky or floor
COVER_MIN_LONG_SIDE = 1920          # below this the cover path would upscale; fall back to backdrop
BACKDROP_BLUR_RADIUS = 48
BACKDROP_BRIGHTNESS = 0.55
BACKDROP_SUBJECT_HEIGHT_RATIO = 0.70
BACKDROP_SOURCE_BRIGHTNESS = 0.45   # the blurred base is dimmer than the subject
CAPTION_INSET = 56
CAPTION_FONT_SIZE = 38
CAPTION_FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
CAPTION_FILL = (255, 255, 255)      # drawn at 92% alpha
CAPTION_SHADOW_OFFSET = 2
CAPTION_SHADOW_ALPHA = 102          # 40% black
SCRIM_HEIGHT_RATIO = 0.16
SCRIM_MAX_ALPHA = 115               # 45% black at the bottom edge
JPEG_QUALITY = 82
JPEG_QUALITY_FALLBACK = 72
JPEG_MAX_BYTES = 1_200_000

_CAPTION_FILL_ALPHA = round(255 * 0.92)
_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_GENERIC_FOLDERS = frozenset({"photos", "camera", "dcim", "screenshots"})
_DATE_FOLDER_RE = re.compile(r"^[0-9]{4}(/[0-9]{2})*$")


def to_rgb(image: Image.Image) -> Image.Image:
    """Convert any image mode to RGB, compositing transparency onto a black background."""
    if image.mode == "RGB":
        return image
    if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (0, 0, 0))
        background.paste(rgba, mask=rgba.split()[3])
        return background
    return image.convert("RGB")


def cover_box(src_size: tuple[int, int], dst_size: tuple[int, int], centring: tuple[float, float]) -> tuple[int, int, int, int]:
    """Compute the integer source crop rectangle ImageOps.fit uses without exceeding the source."""
    src_w, src_h = src_size
    dst_w, dst_h = dst_size
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        raise ValueError("source and destination dimensions must be positive")
    cx = max(0.0, min(1.0, centring[0]))
    cy = max(0.0, min(1.0, centring[1]))

    src_ratio = src_w / src_h
    dst_ratio = dst_w / dst_h
    if src_ratio >= dst_ratio:
        crop_w = min(src_w, max(1, round(dst_ratio * src_h)))
        crop_h = src_h
    else:
        crop_w = src_w
        crop_h = min(src_h, max(1, round(src_w / dst_ratio)))

    left = max(0, min(src_w - crop_w, round((src_w - crop_w) * cx)))
    top = max(0, min(src_h - crop_h, round((src_h - crop_h) * cy)))
    return (left, top, left + crop_w, top + crop_h)


def cover(image: Image.Image) -> Image.Image:
    """Scale and crop an image to fill CANVAS using COVER_CENTRE."""
    rgb = to_rgb(image)
    return ImageOps.fit(rgb, CANVAS, method=Image.Resampling.LANCZOS, centering=COVER_CENTRE)


def backdrop(image: Image.Image) -> Image.Image:
    """Render a dimmed blurred cover base with the full subject centred at 70% canvas height."""
    rgb = to_rgb(image)
    base = cover(rgb).filter(ImageFilter.GaussianBlur(BACKDROP_BLUR_RADIUS))
    base = ImageEnhance.Brightness(base).enhance(BACKDROP_BRIGHTNESS)

    ratio = max(0.0, min(1.0, BACKDROP_SUBJECT_HEIGHT_RATIO))
    target_h = max(1, round(CANVAS[1] * ratio))
    target_w = max(1, round(rgb.width * (target_h / rgb.height)))
    subject = rgb.resize((target_w, target_h), Image.Resampling.LANCZOS)

    x = (CANVAS[0] - target_w) // 2
    y = (CANVAS[1] - target_h) // 2
    base.paste(subject, (x, y))
    return base


def diptych(left: Image.Image, right: Image.Image) -> Image.Image:
    """Compose two portraits side-by-side on CANVAS separated by a pure black gutter."""
    canvas = Image.new("RGB", CANVAS, (0, 0, 0))
    left_half = ImageOps.fit(to_rgb(left), (DIPTYCH_HALF, CANVAS[1]), method=Image.Resampling.LANCZOS, centering=COVER_CENTRE)
    right_half = ImageOps.fit(to_rgb(right), (DIPTYCH_HALF, CANVAS[1]), method=Image.Resampling.LANCZOS, centering=COVER_CENTRE)
    canvas.paste(left_half, (0, 0))
    canvas.paste(right_half, (DIPTYCH_HALF + DIPTYCH_GUTTER, 0))
    return canvas


def place_name(rel_path: str) -> str | None:
    """Return the folder name as a display label, or None for root, date, or generic camera paths."""
    cleaned = rel_path.lstrip("/")
    if not cleaned:
        return None
    parent = PurePosixPath(cleaned).parent
    parent_str = parent.as_posix()
    if not parent_str or parent_str == ".":
        return None
    if _DATE_FOLDER_RE.match(parent_str):
        return None
    segment = parent.name
    if not segment or segment.lower() in _GENERIC_FOLDERS:
        return None
    return segment


def caption_lines(captured_at: datetime | None, rel_path: str) -> tuple[str, ...]:
    """Return locale-independent date and optional place caption lines, or () when undated."""
    if captured_at is None:
        return ()
    date_line = f"{captured_at.day} {_MONTH_NAMES[captured_at.month]} {captured_at.year}"
    place = place_name(rel_path)
    if place:
        return (date_line, place)
    return (date_line,)


def _load_caption_font() -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(CAPTION_FONT_PATH, CAPTION_FONT_SIZE)
    except OSError:
        return ImageFont.load_default(size=CAPTION_FONT_SIZE)


def _font_line_height(font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> int:
    if hasattr(font, "getmetrics"):
        ascent, descent = font.getmetrics()
        return ascent + descent
    bbox = font.getbbox("Ag")
    return bbox[3] - bbox[1]


def draw_caption(canvas: Image.Image, lines: Sequence[str]) -> Image.Image:
    """Composite a bottom gradient scrim and shadowed bottom-left caption lines onto canvas."""
    if not lines:
        return canvas

    width, height = canvas.size
    scrim_h = max(1, round(height * SCRIM_HEIGHT_RATIO))
    ramp = Image.new("L", (1, scrim_h))
    ramp.putdata([round(SCRIM_MAX_ALPHA * row / max(1, scrim_h - 1)) for row in range(scrim_h)])
    alpha_band = ramp.resize((width, scrim_h), Image.Resampling.NEAREST)
    scrim_band = Image.new("RGBA", (width, scrim_h), (0, 0, 0, 0))
    scrim_band.putalpha(alpha_band)
    scrim_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    scrim_overlay.paste(scrim_band, (0, height - scrim_h))

    composed = Image.alpha_composite(to_rgb(canvas).convert("RGBA"), scrim_overlay)

    font = _load_caption_font()
    line_height = _font_line_height(font)
    shadow_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow_overlay)
    text_overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    text_draw = ImageDraw.Draw(text_overlay)
    shadow_fill = (0, 0, 0, CAPTION_SHADOW_ALPHA)
    text_fill = (*CAPTION_FILL, _CAPTION_FILL_ALPHA)

    total_lines = len(lines)
    for idx, line in enumerate(lines):
        y = height - CAPTION_INSET - (total_lines - idx) * line_height
        shadow_draw.text((CAPTION_INSET + CAPTION_SHADOW_OFFSET, y + CAPTION_SHADOW_OFFSET), line, font=font, fill=shadow_fill)
        text_draw.text((CAPTION_INSET, y), line, font=font, fill=text_fill)

    composed = Image.alpha_composite(composed, shadow_overlay)
    composed = Image.alpha_composite(composed, text_overlay)
    return composed.convert("RGB")


def render_slide(
    kind: Literal["single", "pair", "backdrop"],
    images: Sequence[Image.Image],
    captured_at: datetime | None,
    rel_path: str,
) -> Image.Image:
    """Render a full 1920x1200 RGB slide for the given kind and apply its caption once."""
    if kind == "single":
        if len(images) != 1:
            raise ValueError(f"single slide requires 1 image, got {len(images)}")
        canvas = cover(images[0])
    elif kind == "backdrop":
        if len(images) != 1:
            raise ValueError(f"backdrop slide requires 1 image, got {len(images)}")
        canvas = backdrop(images[0])
    elif kind == "pair":
        if len(images) != 2:
            raise ValueError(f"pair slide requires 2 images, got {len(images)}")
        canvas = diptych(images[0], images[1])
    else:
        raise ValueError(f"unknown slide kind: {kind!r}")

    return draw_caption(canvas, caption_lines(captured_at, rel_path))


def output_name(kind: str, rel_paths: Sequence[str]) -> str:
    """Return the deterministic output JPEG filename for a rendered slide."""
    if kind in ("single", "backdrop"):
        if len(rel_paths) != 1:
            raise ValueError(f"{kind} output requires 1 rel_path, got {len(rel_paths)}")
        stem = PurePosixPath(rel_paths[0]).stem
        if not stem:
            raise ValueError(f"empty stem for rel_path: {rel_paths[0]!r}")
        return f"{stem}.jpg"
    if kind == "pair":
        if len(rel_paths) != 2:
            raise ValueError(f"pair output requires 2 rel_paths, got {len(rel_paths)}")
        first, second = sorted(rel_paths)
        hash_a = hashlib.sha1(first.encode("utf-8")).hexdigest()[:12]
        hash_b = hashlib.sha1(second.encode("utf-8")).hexdigest()[:12]
        return f"pair-{hash_a}-{hash_b}.jpg"
    raise ValueError(f"unknown slide kind: {kind!r}")


def save_jpeg(image: Image.Image, path: pathlib.Path) -> int:
    """Atomically write an RGB image as a stripped progressive 4:2:0 JPEG and return its byte size."""
    dest_path = pathlib.Path(path)
    tmp_path = dest_path.with_name(f"{dest_path.name}.tmp")
    rgb = to_rgb(image)
    try:
        rgb.save(tmp_path, format="JPEG", quality=JPEG_QUALITY, progressive=True, subsampling="4:2:0", optimize=True)
        size = tmp_path.stat().st_size
        if size > JPEG_MAX_BYTES:
            rgb.save(tmp_path, format="JPEG", quality=JPEG_QUALITY_FALLBACK, progressive=True, subsampling="4:2:0", optimize=True)
            size = tmp_path.stat().st_size
        os.replace(tmp_path, dest_path)
        return size
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
