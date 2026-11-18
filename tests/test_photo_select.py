"""Offline unit tests for tools/photo_select.py."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
import hashlib
import math
import random

from PIL import Image, ImageFilter
import pytest

from conftest import load_tool

photo_select = load_tool("photo_select")

TODAY = date(2026, 11, 18)


def _distinct_hash(tag: object) -> int:
    digest = hashlib.sha256(str(tag).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _cand(
    rel_path: str = "2026/11/photo.jpg",
    *,
    width: int = 1600,
    height: int = 1200,
    captured_at: datetime | None = datetime(2026, 11, 10, 12, 0, 0),
    luma: float = 128.0,
    sharpness: float = 100.0,
    dhash: int = 0,
):
    return photo_select.Candidate(
        rel_path=rel_path,
        width=width,
        height=height,
        captured_at=captured_at,
        luma=luma,
        sharpness=sharpness,
        dhash=dhash,
    )


def _gradient_image(width: int, height: int, *, mirror: bool = False) -> Image.Image:
    img = Image.new("RGB", (width, height))
    pixels = []
    for row in range(height):
        for col in range(width):
            effective_col = (width - 1 - col) if mirror else col
            red = (effective_col * 255) // max(1, width - 1)
            green = (row * 255) // max(1, height - 1)
            blue = ((effective_col + row) * 127) // max(1, width + height - 2)
            pixels.append((red, green, blue))
    img.putdata(pixels)
    return img


def test_admission_long_side_threshold():
    too_small = _cand(width=photo_select.ADMIT_MIN_LONG_SIDE - 1, height=900)
    exact = _cand(width=photo_select.ADMIT_MIN_LONG_SIDE, height=900)
    portrait_exact = _cand(width=900, height=photo_select.ADMIT_MIN_LONG_SIDE)
    assert photo_select.admission_reject(too_small) == "too_small"
    assert photo_select.admission_reject(exact) is None
    assert photo_select.admission_reject(portrait_exact) is None


def test_admission_aspect_bounds():
    min_ok_width = round(2000 * photo_select.ADMIT_MIN_ASPECT)
    too_tall_width = round(2000 * (photo_select.ADMIT_MIN_ASPECT - 0.01))
    max_ok_width = round(1000 * photo_select.ADMIT_MAX_ASPECT)
    too_wide_width = round(1000 * (photo_select.ADMIT_MAX_ASPECT + 0.01))

    assert photo_select.admission_reject(_cand(width=min_ok_width, height=2000)) is None
    assert photo_select.admission_reject(_cand(width=too_tall_width, height=2000)) == "aspect"
    assert photo_select.admission_reject(_cand(width=max_ok_width, height=1000)) is None
    assert photo_select.admission_reject(_cand(width=too_wide_width, height=1000)) == "aspect"


def test_admission_screenshot_rules_and_order():
    png_photo = _cand("2026/11/shot.png", width=1600, height=1200, luma=50.0)
    webp_photo = _cand("2026/11/shot.WEBP", width=1600, height=1200, luma=50.0)
    white_16_10 = _cand(
        "2026/11/page.jpg",
        width=1600,
        height=1000,
        luma=photo_select.SCREENSHOT_MIN_LUMA,
    )
    dark_16_10 = _cand(
        "2026/11/night.jpg",
        width=1600,
        height=1000,
        luma=photo_select.SCREENSHOT_MIN_LUMA - 1,
    )
    tiny_png = _cand("2026/11/tiny.png", width=photo_select.ADMIT_MIN_LONG_SIDE - 1, height=400)
    extreme_png = _cand("2026/11/pano.png", width=3000, height=1000)

    assert photo_select.admission_reject(png_photo) == "screenshot"
    assert photo_select.admission_reject(webp_photo) == "screenshot"
    assert photo_select.admission_reject(white_16_10) == "screenshot"
    assert photo_select.admission_reject(dark_16_10) is None
    assert photo_select.admission_reject(tiny_png) == "too_small"
    assert photo_select.admission_reject(extreme_png) == "aspect"


def test_dhash_jpeg_stability_mirror_difference_and_width(tmp_path):
    original = _gradient_image(64, 48, mirror=False)
    mirrored = _gradient_image(64, 48, mirror=True)
    path_hi = tmp_path / "hi.jpg"
    path_lo = tmp_path / "lo.jpg"
    original.save(path_hi, format="JPEG", quality=95)
    original.save(path_lo, format="JPEG", quality=40)

    with Image.open(path_hi) as img_hi, Image.open(path_lo) as img_lo:
        hash_hi = photo_select.dhash(img_hi)
        hash_lo = photo_select.dhash(img_lo)
    hash_mirrored = photo_select.dhash(mirrored)

    assert 0 <= hash_hi < (1 << 64)
    assert 0 <= hash_mirrored < (1 << 64)
    assert (hash_hi ^ hash_lo).bit_count() <= 2
    assert hash_mirrored != hash_hi


def test_sharpness_ranks_noisy_above_blurred_and_mean_luma():
    rng = random.Random(12345)
    noisy = Image.new("L", (320, 240))
    noisy.putdata([rng.randint(0, 255) for _ in range(320 * 240)])
    blurred = noisy.filter(ImageFilter.GaussianBlur(3))

    sharp_val = photo_select.sharpness(noisy)
    blur_val = photo_select.sharpness(blurred)
    assert sharp_val > blur_val
    assert photo_select.sharpness(noisy) == sharp_val

    black = Image.new("RGB", (32, 32), (0, 0, 0))
    white = Image.new("RGB", (32, 32), (255, 255, 255))
    assert photo_select.mean_luma(black) == 0.0
    assert photo_select.mean_luma(white) == 255.0


def test_sharpness_weights_monotonic_bounded_and_order_independent():
    values_a = {"b.jpg": 50.0, "a.jpg": 10.0, "d.jpg": 50.0, "c.jpg": 200.0}
    values_b = {"c.jpg": 200.0, "d.jpg": 50.0, "a.jpg": 10.0, "b.jpg": 50.0}

    weights_a = photo_select.sharpness_weights(values_a)
    weights_b = photo_select.sharpness_weights(values_b)

    assert weights_a == weights_b
    assert weights_a["b.jpg"] == weights_a["d.jpg"]
    assert weights_a["a.jpg"] < weights_a["b.jpg"] < weights_a["c.jpg"]
    for weight in weights_a.values():
        assert 0.6 <= weight <= 1.0
    assert photo_select.sharpness_weights({}) == {}


def test_cluster_hamming_and_time_gap_boundaries():
    base_time = datetime(2026, 11, 10, 12, 0, 0)
    hash_at_limit = (1 << photo_select.CLUSTER_HAMMING) - 1
    # Use disjoint bits from hash_at_limit so c_ham_ok cannot transitively bridge c_ham_far.
    hash_over_limit = ((1 << (photo_select.CLUSTER_HAMMING + 1)) - 1) << 16

    c_base = _cand("a.jpg", captured_at=base_time, dhash=0)
    c_ham_ok = _cand("b.jpg", captured_at=base_time + timedelta(seconds=10), dhash=hash_at_limit)
    c_ham_far = _cand("c.jpg", captured_at=base_time + timedelta(seconds=20), dhash=hash_over_limit)
    ham_ids = photo_select.cluster([c_ham_far, c_ham_ok, c_base])
    assert ham_ids["a.jpg"] == ham_ids["b.jpg"]
    assert ham_ids["c.jpg"] != ham_ids["a.jpg"]

    t_base = _cand("t0.jpg", captured_at=base_time, dhash=0)
    t_gap_ok = _cand(
        "t1.jpg",
        captured_at=base_time + timedelta(seconds=min(60, photo_select.CLUSTER_MAX_GAP_SECONDS)),
        dhash=0,
    )
    ok_ids = photo_select.cluster([t_gap_ok, t_base])
    assert ok_ids["t0.jpg"] == ok_ids["t1.jpg"]

    t_gap_far = _cand(
        "t2.jpg",
        captured_at=base_time + timedelta(seconds=photo_select.CLUSTER_MAX_GAP_SECONDS + 1),
        dhash=0,
    )
    t_week_later = _cand("t3.jpg", captured_at=base_time + timedelta(days=7), dhash=0)
    far_ids = photo_select.cluster([t_week_later, t_gap_far, t_base])
    assert far_ids["t2.jpg"] != far_ids["t0.jpg"]
    assert far_ids["t3.jpg"] != far_ids["t0.jpg"]


def test_cluster_undated_sorts_first_and_never_bridges_dated_clusters():
    base_time = datetime(2026, 11, 10, 12, 0, 0)
    undated = _cand("u.jpg", captured_at=None, dhash=0)
    first_dated = _cand("d1.jpg", captured_at=base_time, dhash=0)
    later_dated = _cand("d2.jpg", captured_at=base_time + timedelta(hours=2), dhash=0)

    ids = photo_select.cluster([later_dated, first_dated, undated])
    assert ids["u.jpg"] == 0
    assert ids["d1.jpg"] != ids["d2.jpg"]


def test_score_recency_exposure_undated_and_on_this_month():
    fresh = _cand("fresh.jpg", captured_at=datetime(2026, 11, 18, 10, 0), luma=120.0)
    older = _cand("older.jpg", captured_at=datetime(2026, 6, 1, 10, 0), luma=120.0)
    undated = _cand("undated.jpg", captured_at=None, luma=120.0)

    score_fresh = photo_select.score(fresh, today=TODAY, sharpness_weight=1.0)
    score_older = photo_select.score(older, today=TODAY, sharpness_weight=1.0)
    score_undated = photo_select.score(undated, today=TODAY, sharpness_weight=1.0)
    assert score_fresh > score_older
    assert math.isclose(score_undated, 0.6, rel_tol=1e-9)

    # Exposure taper at luma 30 and 220 with undated baseline (recency 0.6, no anniversary).
    dark = _cand("dark.jpg", captured_at=None, luma=30.0)
    bright = _cand("bright.jpg", captured_at=None, luma=220.0)
    expected_dark = 0.6 * (0.55 + 0.45 * (30.0 / 70.0))
    expected_bright = 0.6 * (0.55 + 0.45 * ((255.0 - 220.0) / 65.0))
    assert math.isclose(photo_select.score(dark, today=TODAY, sharpness_weight=1.0), expected_dark, rel_tol=1e-9)
    assert math.isclose(photo_select.score(bright, today=TODAY, sharpness_weight=1.0), expected_bright, rel_tol=1e-9)

    # On-this-month boosts earlier-year same-month photos, peaking on same day-of-month, not this year or future.
    last_year_exact = _cand("ly_exact.jpg", captured_at=datetime(2025, 11, 18, 12, 0), luma=120.0)
    last_year_off = _cand("ly_off.jpg", captured_at=datetime(2025, 11, 4, 12, 0), luma=120.0)
    last_year_other_month = _cand("ly_may.jpg", captured_at=datetime(2025, 5, 18, 12, 0), luma=120.0)
    this_year = _cand("ty.jpg", captured_at=datetime(2026, 11, 18, 12, 0), luma=120.0)
    next_year = _cand("ny.jpg", captured_at=datetime(2027, 11, 18, 12, 0), luma=120.0)

    expected_exact = (0.5 ** (365 / photo_select.RECENCY_HALF_LIFE_DAYS)) * photo_select.ON_THIS_MONTH_PEAK
    assert math.isclose(
        photo_select.score(last_year_exact, today=TODAY, sharpness_weight=1.0),
        expected_exact,
        rel_tol=1e-9,
    )
    assert photo_select.score(last_year_exact, today=TODAY, sharpness_weight=1.0) > photo_select.score(
        last_year_off, today=TODAY, sharpness_weight=1.0
    )
    expected_other_month = 0.5 ** ((TODAY - date(2025, 5, 18)).days / photo_select.RECENCY_HALF_LIFE_DAYS)
    assert math.isclose(
        photo_select.score(last_year_other_month, today=TODAY, sharpness_weight=1.0),
        expected_other_month,
        rel_tol=1e-9,
    )
    assert math.isclose(photo_select.score(this_year, today=TODAY, sharpness_weight=1.0), 1.0, rel_tol=1e-9)
    assert math.isclose(photo_select.score(next_year, today=TODAY, sharpness_weight=1.0), 1.0, rel_tol=1e-9)


def test_select_day_ceiling_keeps_top_and_preserves_low_scoring_day():
    # Old day with 20 distinct photos (well past RECENT_RESERVE if newer days exist) -> keeps DAY_CEILING.
    day_count = photo_select.DAY_CEILING + 8
    candidates = []
    # Add RECENT_RESERVE newer photos across separate days so old day is not in reserve.
    for offset in range(photo_select.RECENT_RESERVE):
        day = TODAY - timedelta(days=offset)
        candidates.append(
            _cand(
                f"recent/{offset:02d}.jpg",
                captured_at=datetime(day.year, day.month, day.day, 12, 0),
                dhash=_distinct_hash(f"recent-{offset}"),
                sharpness=100.0 + offset,
            )
        )
    old_day = TODAY - timedelta(days=400)
    for idx in range(day_count):
        candidates.append(
            _cand(
                f"old/{idx:02d}.jpg",
                captured_at=datetime(old_day.year, old_day.month, old_day.day, 10, idx),
                luma=20.0,
                sharpness=10.0 + idx,
                dhash=_distinct_hash(f"old-{idx}"),
            )
        )

    selection = photo_select.select(candidates, today=TODAY)
    old_slides = [slide for slide in selection.slides if slide.key.startswith("old/")]
    assert len(old_slides) == photo_select.DAY_CEILING
    kept_names = {slide.key for slide in old_slides}
    expected_top = {f"old/{idx:02d}.jpg" for idx in range(day_count - photo_select.DAY_CEILING, day_count)}
    assert kept_names == expected_top
    assert selection.rejected["day_ceiling"] == day_count - photo_select.DAY_CEILING


def test_select_days_covered_under_cap_and_month_ceiling():
    month_quota = int(photo_select.SLIDE_CAP * photo_select.MONTH_SHARE_CEILING)
    total_in_month = month_quota + 25
    # Spread across 10 days (<= DAY_CEILING each) in one month -> month_share must trim to month_quota.
    candidates = []
    for idx in range(total_in_month):
        day_num = 1 + (idx % 10)
        hour_num = idx // 10
        candidates.append(
            _cand(
                f"2025/05/img_{idx:03d}.jpg",
                captured_at=datetime(2025, 5, day_num, hour_num, 0),
                sharpness=50.0 + idx,
                dhash=_distinct_hash(f"month-{idx}"),
            )
        )

    selection = photo_select.select(candidates, today=TODAY)
    assert len(selection.slides) == month_quota
    assert selection.rejected["month_share"] == total_in_month - month_quota

    # Under cap with fewer than DAY_CEILING per day: every admitted capture day stays covered.
    small_deck = [
        _cand(
            f"d/{day_offset}.jpg",
            captured_at=datetime(2026, 10, 1 + day_offset, 12, 0),
            dhash=1 << day_offset,
        )
        for day_offset in range(6)
    ]
    small_sel = photo_select.select(small_deck, today=TODAY)
    assert small_sel.days_total == 6
    assert small_sel.days_covered == 6


def test_select_cap_and_recent_reserve_on_one_huge_day():
    # One huge day with RECENT_RESERVE + 15 photos -> all RECENT_RESERVE newest survive DAY_CEILING.
    huge_total = photo_select.RECENT_RESERVE + 15
    huge_day = [
        _cand(
            f"huge/{idx:02d}.jpg",
            captured_at=datetime(2026, 11, 18, idx // 60, idx % 60),
            sharpness=100.0 + (huge_total - idx),  # older frames sharper so reserve is tested against score
            dhash=_distinct_hash(f"huge-{idx}"),
        )
        for idx in range(huge_total)
    ]
    huge_sel = photo_select.select(huge_day, today=TODAY)
    expected_recent = {f"huge/{idx:02d}.jpg" for idx in range(huge_total - photo_select.RECENT_RESERVE, huge_total)}
    actual_names = {name for slide in huge_sel.slides for name in slide.names}
    assert expected_recent.issubset(actual_names)
    assert len(huge_sel.slides) == photo_select.RECENT_RESERVE

    # Overall SLIDE_CAP: build SLIDE_CAP + 40 landscape singles across many months so month ceiling does not fire.
    many_total = photo_select.SLIDE_CAP + 40
    many_candidates = []
    start_day = date(2020, 1, 1)
    for idx in range(many_total):
        day = start_day + timedelta(days=idx * 5)
        many_candidates.append(
            _cand(
                f"many/{idx:04d}.jpg",
                captured_at=datetime(day.year, day.month, day.day, 12, 0),
                sharpness=100.0 + idx,
                dhash=_distinct_hash(f"many-{idx}"),
            )
        )
    capped_sel = photo_select.select(many_candidates, today=date(2026, 12, 31))
    assert len(capped_sel.slides) == photo_select.SLIDE_CAP
    assert capped_sel.rejected["cap"] == 40
    # Lowest-scoring oldest entries were dropped, highest-scoring survived.
    surviving_keys = {slide.key for slide in capped_sel.slides}
    assert "many/0000.jpg" not in surviving_keys
    assert f"many/{many_total - 1:04d}.jpg" in surviving_keys


def test_select_pairing_backdrops_and_determinism():
    p1 = _cand(
        "trip/b_first.jpg",
        width=900,
        height=1200,  # 0.75
        captured_at=datetime(2026, 9, 1, 10, 0),
        sharpness=80.0,
        dhash=1,
    )
    p2 = _cand(
        "other/a_second.jpg",
        width=960,
        height=1200,  # 0.80, within PAIR_ASPECT_TOLERANCE and PAIR_MAX_DAY_GAP
        captured_at=datetime(2026, 9, 1 + photo_select.PAIR_MAX_DAY_GAP, 10, 0),
        sharpness=160.0,
        dhash=1 << 20,
    )
    leftover_portrait = _cand(
        "trip/solo.jpg",
        width=900,
        height=1200,
        captured_at=datetime(2026, 9, 20, 10, 0),
        sharpness=120.0,
        dhash=1 << 40,
    )
    wide_landscape = _cand(
        "trip/pano.jpg",
        width=2760,
        height=1200,  # 2.30 (> 2.2, <= ADMIT_MAX_ASPECT)
        captured_at=datetime(2026, 9, 21, 10, 0),
        sharpness=140.0,
        dhash=1 << 50,
    )
    normal_landscape = _cand(
        "trip/normal.jpg",
        width=1800,
        height=1200,  # 1.50
        captured_at=datetime(2026, 9, 22, 10, 0),
        sharpness=150.0,
        dhash=1 << 60,
    )

    inputs = [normal_landscape, wide_landscape, leftover_portrait, p2, p1]
    sel_one = photo_select.select(inputs, today=TODAY)
    sel_two = photo_select.select(list(reversed(inputs)), today=TODAY)
    assert sel_one == sel_two

    by_key = {slide.key: slide for slide in sel_one.slides}
    pair_key = "other/a_second.jpg|trip/b_first.jpg"
    assert pair_key in by_key
    pair_slide = by_key[pair_key]
    assert pair_slide.kind == "pair"
    assert pair_slide.captured_at == p1.captured_at
    assert set(pair_slide.names) == {"trip/b_first.jpg", "other/a_second.jpg"}

    weights = photo_select.sharpness_weights({c.rel_path: c.sharpness for c in inputs})
    expected_mean = (
        photo_select.score(p1, today=TODAY, sharpness_weight=weights[p1.rel_path])
        + photo_select.score(p2, today=TODAY, sharpness_weight=weights[p2.rel_path])
    ) / 2.0
    assert math.isclose(pair_slide.score, expected_mean, rel_tol=1e-9)

    assert by_key["trip/solo.jpg"].kind == "backdrop"
    assert by_key["trip/pano.jpg"].kind == "backdrop"
    assert by_key["trip/normal.jpg"].kind == "single"


def test_day_order_permutation_determinism_and_cluster_repair():
    # Craft slides so every bucket gets slides and cluster 0 repeats back-to-back after round-robin,
    # while distinct clusters remain available to swap in.
    slides = tuple(
        photo_select.Slide(
            key=f"s{idx:02d}.jpg",
            kind="single",
            names=(f"s{idx:02d}.jpg",),
            captured_at=datetime(2026, 11, 1, 10, idx),
            score=100.0 - idx,
            cluster=0 if idx in (0, 2, 4) else idx,
        )
        for idx in range(15)
    )
    selection = photo_select.Selection(slides=slides, rejected={}, days_covered=1, days_total=1)

    order_a = photo_select.day_order(selection, seed=date(2026, 11, 18))
    order_b = photo_select.day_order(selection, seed=date(2026, 11, 18))
    order_c = photo_select.day_order(selection, seed=date(2026, 11, 19))

    assert order_a == order_b
    assert order_a != order_c
    assert sorted(order_a) == sorted(slide.key for slide in slides)

    slide_by_key = {slide.key: slide for slide in slides}
    clusters_in_order = [slide_by_key[key].cluster for key in order_a]
    for pos in range(1, len(clusters_in_order)):
        assert clusters_in_order[pos] != clusters_in_order[pos - 1]


def test_day_order_repairs_every_avoidable_adjacency():
    def _assert_valid_order(clusters: list[int], *, seed: date) -> None:
        slides = tuple(
            photo_select.Slide(
                key=f"s{idx:02d}.jpg",
                kind="single",
                names=(f"s{idx:02d}.jpg",),
                captured_at=datetime(2026, 11, 1, 10, idx),
                score=100.0 - idx,
                cluster=cluster_id,
            )
            for idx, cluster_id in enumerate(clusters)
        )
        selection = photo_select.Selection(slides=slides, rejected={}, days_covered=1, days_total=1)
        order = photo_select.day_order(selection, seed=seed)
        assert sorted(order) == sorted(slide.key for slide in slides)

        cluster_by_key = {slide.key: slide.cluster for slide in slides}
        ordered_clusters = [cluster_by_key[key] for key in order]
        for pos in range(1, len(ordered_clusters)):
            curr = ordered_clusters[pos]
            prev = ordered_clusters[pos - 1]
            assert curr < 0 or curr != prev

    # Explicit regression case from issue #17 (previously ended ..., 2, 0, 0).
    _assert_valid_order([2, 0, 1, 0, 0, 1, 0, 2, 0, 1, 1, 1, 0, 0, 1, 1, 2], seed=TODAY)

    rng = random.Random(20261118)
    checked = 0
    step = 0
    while checked < 40:
        size = rng.randint(6, 30)
        num_clusters = rng.randint(2, size // 2)
        clusters = [rng.randrange(num_clusters) for _ in range(size)]
        step += 1
        if max(Counter(clusters).values()) > math.ceil(size / 2):
            continue
        _assert_valid_order(clusters, seed=TODAY + timedelta(days=step))
        checked += 1
