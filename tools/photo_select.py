"""Pure photo selection pass: admission, deduplication, pairing, scoring, and ordering."""
from __future__ import annotations

import calendar
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import math
from pathlib import PurePosixPath
import random
from typing import Literal

from PIL import Image, ImageFilter, ImageStat

ADMIT_MIN_LONG_SIDE = 1200      # below this a photo cannot fill a 1200 px tall panel without upscaling
ADMIT_MIN_ASPECT = 0.55         # 9:16
ADMIT_MAX_ASPECT = 2.4          # wider is a panorama; only usable as a backdrop
SCREENSHOT_MIN_LUMA = 235       # near-white page background
SCREENSHOT_ASPECT_TOLERANCE = 0.01
SCREENSHOT_TARGET_ASPECT = 1.6
CLUSTER_HAMMING = 8             # dHash distance for "same moment"
CLUSTER_MAX_GAP_SECONDS = 90    # EXIF capture times within this are one moment
PAIR_ASPECT_TOLERANCE = 0.15    # diptych halves must have similar aspect or the pair looks ragged
PAIR_MAX_DAY_GAP = 3            # a pair may span days, but only days inside one event
SINGLE_MAX_ASPECT = 2.2         # landscapes wider than this need backdrop treatment rather than full-bleed
DAY_CEILING = 12                # the capture day is the event unit (owner decision, 2026-11-18)
MONTH_SHARE_CEILING = 0.25      # of the deck; insurance for a growing library, inert today
SLIDE_CAP = 300
RECENT_RESERVE = 25             # newest photos are always shown, whatever the ceilings say
RECENCY_HALF_LIFE_DAYS = 548    # 18 months
ON_THIS_MONTH_PEAK = 1.6
ON_THIS_MONTH_SIGMA_DAYS = 10.0

_SCREENSHOT_EXTENSIONS = (".png", ".webp")
_DAY_ORDER_BUCKETS = 5


@dataclass(frozen=True)
class Candidate:
    """Measured candidate photo with upright dimensions and image metrics."""

    rel_path: str
    width: int
    height: int
    captured_at: datetime | None
    luma: float
    sharpness: float
    dhash: int

    @property
    def aspect(self) -> float:
        return self.width / self.height


RejectReason = Literal["too_small", "aspect", "screenshot"]


@dataclass(frozen=True)
class Slide:
    """Selected display entry representing a single, portrait pair, or backdrop."""

    key: str
    kind: Literal["single", "pair", "backdrop"]
    names: tuple[str, ...]
    captured_at: datetime | None
    score: float
    cluster: int


@dataclass(frozen=True)
class Selection:
    """Final deck of slides along with rejection counts and day coverage."""

    slides: tuple[Slide, ...]
    rejected: Mapping[str, int]
    days_covered: int
    days_total: int


def admission_reject(candidate: Candidate) -> RejectReason | None:
    """Filter out photos too small, extreme in aspect, or likely screenshots before scoring."""
    if max(candidate.width, candidate.height) < ADMIT_MIN_LONG_SIDE:
        return "too_small"
    aspect = candidate.aspect
    if aspect < ADMIT_MIN_ASPECT or aspect > ADMIT_MAX_ASPECT:
        return "aspect"
    lower_path = candidate.rel_path.lower()
    is_screen_ext = lower_path.endswith(_SCREENSHOT_EXTENSIONS)
    is_white_16_10 = (
        abs(aspect - SCREENSHOT_TARGET_ASPECT) <= SCREENSHOT_ASPECT_TOLERANCE
        and candidate.luma >= SCREENSHOT_MIN_LUMA
    )
    if is_screen_ext or is_white_16_10:
        return "screenshot"
    return None


def dhash(image: Image.Image) -> int:
    """Compute a 64-bit difference hash to catch burst duplicates regardless of JPEG quality."""
    grey = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = grey.tobytes()
    bits = 0
    for row in range(8):
        row_offset = row * 9
        for col in range(8):
            bits = (bits << 1) | int(pixels[row_offset + col] > pixels[row_offset + col + 1])
    return bits


def mean_luma(image: Image.Image) -> float:
    """Measure average greyscale brightness (0..255) for exposure scoring and screenshot detection."""
    grey = image.convert("L")
    return float(ImageStat.Stat(grey).mean[0])


def sharpness(image: Image.Image) -> float:
    """Estimate focus using 4-neighbour Laplacian variance at a fixed 256 px long side."""
    grey = image.convert("L")
    width, height = grey.size
    if width >= height:
        target_size = (256, max(1, round(height * 256 / width)))
    else:
        target_size = (max(1, round(width * 256 / height)), 256)
    if grey.size != target_size:
        grey = grey.resize(target_size, Image.Resampling.LANCZOS)
    laplacian = grey.filter(ImageFilter.Kernel((3, 3), (0, -1, 0, -1, 4, -1, 0, -1, 0), scale=1))
    return float(ImageStat.Stat(laplacian).var[0])


def sharpness_weights(values: Mapping[str, float]) -> dict[str, float]:
    """Map raw sharpness values to rank percentiles in [0.6, 1.0] because variance scales with scene texture."""
    total = len(values)
    if total == 0:
        return {}
    if total == 1:
        only_name = next(iter(values))
        return {only_name: 1.0}

    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    weights: dict[str, float] = {}
    idx = 0
    while idx < total:
        group_end = idx + 1
        while group_end < total and ordered[group_end][1] == ordered[idx][1]:
            group_end += 1
        mean_rank = (idx + group_end - 1) / 2.0
        percentile = mean_rank / (total - 1)
        weight = 0.6 + 0.4 * percentile
        for pos in range(idx, group_end):
            weights[ordered[pos][0]] = weight
        idx = group_end
    return weights


def cluster(candidates: Sequence[Candidate]) -> dict[str, int]:
    """Group burst frames by perceptual hash and capture gap; undated photos never bridge dated bursts."""
    if not candidates:
        return {}

    ordered = sorted(candidates, key=lambda cand: (cand.captured_at or datetime.min, cand.rel_path))
    count = len(ordered)
    parent = list(range(count))
    has_dated = [cand.captured_at is not None for cand in ordered]

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            return
        if root_left > root_right:
            root_left, root_right = root_right, root_left
        parent[root_right] = root_left
        has_dated[root_left] = has_dated[root_left] or has_dated[root_right]

    dated_indices = [idx for idx, cand in enumerate(ordered) if cand.captured_at is not None]
    undated_indices = [idx for idx, cand in enumerate(ordered) if cand.captured_at is None]

    # Dated-to-dated unions first so dated burst structure is settled before undated attachment.
    for pos, left_idx in enumerate(dated_indices):
        left_cand = ordered[left_idx]
        assert left_cand.captured_at is not None
        for right_idx in dated_indices[pos + 1 :]:
            right_cand = ordered[right_idx]
            assert right_cand.captured_at is not None
            if (right_cand.captured_at - left_cand.captured_at).total_seconds() > CLUSTER_MAX_GAP_SECONDS:
                break
            if (left_cand.dhash ^ right_cand.dhash).bit_count() <= CLUSTER_HAMMING:
                union(left_idx, right_idx)

    # Undated candidates can attach to at most one dated cluster and may never merge two dated clusters.
    for undated_idx in undated_indices:
        undated_cand = ordered[undated_idx]
        for dated_idx in dated_indices:
            if (undated_cand.dhash ^ ordered[dated_idx].dhash).bit_count() <= CLUSTER_HAMMING:
                root_u = find(undated_idx)
                root_d = find(dated_idx)
                if root_u != root_d and not (has_dated[root_u] and has_dated[root_d]):
                    union(root_u, root_d)
                    break

    for pos, left_idx in enumerate(undated_indices):
        left_cand = ordered[left_idx]
        for right_idx in undated_indices[pos + 1 :]:
            if (left_cand.dhash ^ ordered[right_idx].dhash).bit_count() <= CLUSTER_HAMMING:
                root_left = find(left_idx)
                root_right = find(right_idx)
                if root_left != root_right and not (has_dated[root_left] and has_dated[root_right]):
                    union(root_left, root_right)

    root_to_id: dict[int, int] = {}
    result: dict[str, int] = {}
    for idx, cand in enumerate(ordered):
        root = find(idx)
        if root not in root_to_id:
            root_to_id[root] = len(root_to_id)
        result[cand.rel_path] = root_to_id[root]
    return result


def _exposure_term(luma: float) -> float:
    if 70.0 <= luma <= 190.0:
        return 1.0
    if luma < 70.0:
        clamped = max(0.0, luma)
        return 0.55 + 0.45 * (clamped / 70.0)
    clamped = min(255.0, luma)
    return 0.55 + 0.45 * ((255.0 - clamped) / 65.0)


def _on_this_month_term(captured_at: datetime | None, today: date) -> float:
    if captured_at is None or captured_at.year >= today.year or captured_at.month != today.month:
        return 1.0
    days_in_month = calendar.monthrange(captured_at.year, captured_at.month)[1]
    raw_delta = abs(captured_at.day - today.day)
    delta = min(raw_delta, days_in_month - raw_delta)
    exponent = -(delta * delta) / (2.0 * ON_THIS_MONTH_SIGMA_DAYS * ON_THIS_MONTH_SIGMA_DAYS)
    return 1.0 + (ON_THIS_MONTH_PEAK - 1.0) * math.exp(exponent)


def score(candidate: Candidate, *, today: date, sharpness_weight: float) -> float:
    """Rank candidates by recency, relative sharpness, exposure, and same-month anniversary boost."""
    if candidate.captured_at is None:
        recency = 0.6
    else:
        age_days = max(0, (today - candidate.captured_at.date()).days)
        recency = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    exposure = _exposure_term(candidate.luma)
    anniversary = _on_this_month_term(candidate.captured_at, today)
    return recency * sharpness_weight * exposure * anniversary


def _can_pair(left: Candidate, right: Candidate) -> bool:
    if right.aspect >= 1.0:
        return False
    if abs(left.aspect - right.aspect) > PAIR_ASPECT_TOLERANCE:
        return False
    same_folder = PurePosixPath(left.rel_path).parent == PurePosixPath(right.rel_path).parent
    if same_folder:
        return True
    if left.captured_at is not None and right.captured_at is not None:
        return abs((right.captured_at.date() - left.captured_at.date()).days) <= PAIR_MAX_DAY_GAP
    return False


def _build_slides(
    survivors: Sequence[Candidate],
    scores: Mapping[str, float],
    cluster_ids: Mapping[str, int],
) -> list[Slide]:
    ordered = sorted(survivors, key=lambda cand: (cand.captured_at or datetime.min, cand.rel_path))
    used: set[int] = set()
    slides: list[Slide] = []

    for idx, cand in enumerate(ordered):
        if idx in used:
            continue
        if cand.aspect >= 1.0:
            kind: Literal["single", "backdrop"] = "single" if cand.aspect <= SINGLE_MAX_ASPECT else "backdrop"
            slides.append(
                Slide(
                    key=cand.rel_path,
                    kind=kind,
                    names=(cand.rel_path,),
                    captured_at=cand.captured_at,
                    score=scores[cand.rel_path],
                    cluster=cluster_ids[cand.rel_path],
                )
            )
            used.add(idx)
            continue

        partner_idx: int | None = None
        for probe_idx in range(idx + 1, len(ordered)):
            if probe_idx in used:
                continue
            if _can_pair(cand, ordered[probe_idx]):
                partner_idx = probe_idx
                break

        if partner_idx is None:
            slides.append(
                Slide(
                    key=cand.rel_path,
                    kind="backdrop",
                    names=(cand.rel_path,),
                    captured_at=cand.captured_at,
                    score=scores[cand.rel_path],
                    cluster=cluster_ids[cand.rel_path],
                )
            )
            used.add(idx)
            continue

        partner = ordered[partner_idx]
        used.add(idx)
        used.add(partner_idx)
        pair_names = tuple(sorted((cand.rel_path, partner.rel_path)))
        pair_key = f"{pair_names[0]}|{pair_names[1]}"
        dates = [dt for dt in (cand.captured_at, partner.captured_at) if dt is not None]
        pair_captured_at = min(dates) if dates else None
        pair_score = (scores[cand.rel_path] + scores[partner.rel_path]) / 2.0
        slides.append(
            Slide(
                key=pair_key,
                kind="pair",
                names=pair_names,
                captured_at=pair_captured_at,
                score=pair_score,
                cluster=-1,
            )
        )

    return slides


def _trim_with_reserve(
    items: Sequence[Candidate] | Sequence[Slide],
    *,
    limit: int,
    is_reserved,
    sort_key,
) -> tuple[list, int]:
    reserved = [item for item in items if is_reserved(item)]
    non_reserved = [item for item in items if not is_reserved(item)]
    non_reserved.sort(key=sort_key)
    allowed_non_reserved = max(0, limit - len(reserved))
    kept = reserved + non_reserved[:allowed_non_reserved]
    return kept, len(items) - len(kept)


def select(candidates: Sequence[Candidate], *, today: date) -> Selection:
    """Run the full admission, deduplication, pairing, ceiling, reserve, and cap pipeline."""
    rejected: dict[str, int] = {
        "too_small": 0,
        "aspect": 0,
        "screenshot": 0,
        "duplicate": 0,
        "day_ceiling": 0,
        "month_share": 0,
        "cap": 0,
    }

    admitted: list[Candidate] = []
    for cand in candidates:
        reason = admission_reject(cand)
        if reason is not None:
            rejected[reason] += 1
        else:
            admitted.append(cand)

    if not admitted:
        return Selection(slides=(), rejected=rejected, days_covered=0, days_total=0)

    admitted_dates = {cand.captured_at.date() for cand in admitted if cand.captured_at is not None}
    days_total = len(admitted_dates)
    cand_by_name = {cand.rel_path: cand for cand in admitted}

    # Step 1: one sharpest representative per burst cluster.
    cluster_ids = cluster(admitted)
    clusters_grouped: dict[int, list[Candidate]] = {}
    for cand in admitted:
        clusters_grouped.setdefault(cluster_ids[cand.rel_path], []).append(cand)

    unique_survivors: list[Candidate] = []
    for group in clusters_grouped.values():
        best = min(group, key=lambda cand: (-cand.sharpness, cand.rel_path))
        unique_survivors.append(best)
        rejected["duplicate"] += len(group) - 1

    # Reserve the newest unique moments before ceilings so fresh photos always appear.
    dated_survivors = [cand for cand in unique_survivors if cand.captured_at is not None]
    dated_survivors.sort(key=lambda cand: cand.rel_path)
    dated_survivors.sort(key=lambda cand: cand.captured_at, reverse=True)  # type: ignore[arg-type,return-value]
    reserved_names = {cand.rel_path for cand in dated_survivors[:RECENT_RESERVE]}

    # Step 2: score survivors and apply day ceiling (reserved photos are exempt from drops).
    weights = sharpness_weights({cand.rel_path: cand.sharpness for cand in unique_survivors})
    scores = {
        cand.rel_path: score(cand, today=today, sharpness_weight=weights[cand.rel_path])
        for cand in unique_survivors
    }

    by_day: dict[date | None, list[Candidate]] = {}
    for cand in unique_survivors:
        day_key = cand.captured_at.date() if cand.captured_at is not None else None
        by_day.setdefault(day_key, []).append(cand)

    day_survivors: list[Candidate] = []
    for group in by_day.values():
        kept, dropped = _trim_with_reserve(
            group,
            limit=DAY_CEILING,
            is_reserved=lambda cand: cand.rel_path in reserved_names,
            sort_key=lambda cand: (-scores[cand.rel_path], cand.rel_path),
        )
        day_survivors.extend(kept)
        rejected["day_ceiling"] += dropped

    # Step 3: greedy portrait pairing and single/backdrop classification.
    slides = _build_slides(day_survivors, scores, cluster_ids)

    # Step 4 & 5: month share ceiling (undated and reserved slides exempt).
    month_quota = int(SLIDE_CAP * MONTH_SHARE_CEILING)
    by_month: dict[tuple[int, int] | None, list[Slide]] = {}
    for slide in slides:
        month_key = (slide.captured_at.year, slide.captured_at.month) if slide.captured_at is not None else None
        by_month.setdefault(month_key, []).append(slide)

    def _slide_is_reserved(slide: Slide) -> bool:
        return any(name in reserved_names for name in slide.names)

    def _slide_sort_key(slide: Slide) -> tuple[float, str]:
        return (-slide.score, slide.key)

    month_survivors: list[Slide] = []
    for month_key, group in by_month.items():
        if month_key is None:
            month_survivors.extend(group)
            continue
        kept, dropped = _trim_with_reserve(
            group,
            limit=month_quota,
            is_reserved=_slide_is_reserved,
            sort_key=_slide_sort_key,
        )
        month_survivors.extend(kept)
        rejected["month_share"] += dropped

    # Step 6: overall deck cap (reserved slides stay guaranteed, consuming cap slots).
    if len(month_survivors) > SLIDE_CAP:
        capped_slides, dropped = _trim_with_reserve(
            month_survivors,
            limit=SLIDE_CAP,
            is_reserved=_slide_is_reserved,
            sort_key=_slide_sort_key,
        )
        rejected["cap"] += dropped
    else:
        capped_slides = month_survivors

    # Step 7: deterministic return order and day coverage accounting.
    final_slides = tuple(sorted(capped_slides, key=lambda slide: (slide.captured_at or datetime.min, slide.key)))
    covered_dates = {
        cand_by_name[name].captured_at.date()  # type: ignore[union-attr]
        for slide in final_slides
        for name in slide.names
        if cand_by_name[name].captured_at is not None
    }
    return Selection(
        slides=final_slides,
        rejected=rejected,
        days_covered=len(covered_dates),
        days_total=days_total,
    )


def day_order(selection: Selection, *, seed: date) -> list[str]:
    """Order slides into a deterministic daily playlist that spreads scores and separates burst clusters."""
    if not selection.slides:
        return []

    ordered = sorted(selection.slides, key=lambda slide: (-slide.score, slide.key))
    total = len(ordered)
    base_size, remainder = divmod(total, _DAY_ORDER_BUCKETS)

    buckets: list[list[Slide]] = []
    cursor = 0
    for bucket_idx in range(_DAY_ORDER_BUCKETS):
        bucket_size = base_size + (1 if bucket_idx < remainder else 0)
        bucket = ordered[cursor : cursor + bucket_size]
        cursor += bucket_size
        random.Random(f"{seed.isoformat()}:{bucket_idx}").shuffle(bucket)
        buckets.append(bucket)

    max_len = max(len(bucket) for bucket in buckets)
    interleaved: list[Slide] = []
    for round_idx in range(max_len):
        for bucket in buckets:
            if round_idx < len(bucket):
                interleaved.append(bucket[round_idx])

    result: list[Slide] = []
    queue = list(interleaved)
    counts = Counter(slide.cluster for slide in queue if slide.cluster >= 0)
    while queue:
        prev_cluster = result[-1].cluster if result else -1
        remaining = len(queue)
        # If a cluster holds > remaining // 2 slots, any other pick leaves an unsolvable tail.
        forced_cluster = next(
            (
                cluster_id
                for cluster_id, count in counts.items()
                if count > remaining // 2 and (prev_cluster < 0 or cluster_id != prev_cluster)
            ),
            None,
        )
        nxt = queue.pop(0)
        if forced_cluster is not None:
            if nxt.cluster != forced_cluster:
                swap_idx = next(pos for pos, other in enumerate(queue) if other.cluster == forced_cluster)
                queue[swap_idx], nxt = nxt, queue[swap_idx]
        elif prev_cluster >= 0 and nxt.cluster == prev_cluster:
            swap_idx = next(
                (pos for pos, other in enumerate(queue) if other.cluster < 0 or other.cluster != prev_cluster),
                None,
            )
            if swap_idx is not None:
                queue[swap_idx], nxt = nxt, queue[swap_idx]
        if nxt.cluster >= 0:
            counts[nxt.cluster] -= 1
            if counts[nxt.cluster] == 0:
                del counts[nxt.cluster]
        result.append(nxt)

    return [slide.key for slide in result]
