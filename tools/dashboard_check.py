#!/usr/bin/env python3
"""Validate a Home Assistant Lovelace dashboard configuration."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "ha" / "kitchen_dashboard.yaml"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "ha_states.json"

ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
VIEW_PATH_RE = re.compile(r"^[a-z0-9_-]+$")


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load a YAML dashboard configuration file."""
    with pathlib.Path(path).open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


def collect_entity_ids(node: Any) -> set[str]:
    """Recursively collect entity IDs from `entity` and `entities` keys."""
    found: set[str] = set()

    if isinstance(node, dict):
        entity_val = node.get("entity")
        if isinstance(entity_val, str) and ENTITY_ID_RE.match(entity_val):
            found.add(entity_val)

        entities_val = node.get("entities")
        if isinstance(entities_val, str) and ENTITY_ID_RE.match(entities_val):
            found.add(entities_val)
        elif isinstance(entities_val, list):
            for item in entities_val:
                if isinstance(item, str) and ENTITY_ID_RE.match(item):
                    found.add(item)
                else:
                    found.update(collect_entity_ids(item))

        for key, value in node.items():
            if key in ("entity", "entities"):
                continue
            if isinstance(value, (dict, list)):
                found.update(collect_entity_ids(value))

    elif isinstance(node, list):
        for item in node:
            found.update(collect_entity_ids(item))

    return found


def _validate_cards(cards: list[Any], errors: list[str]) -> None:
    for card in cards:
        if not isinstance(card, dict):
            errors.append(f"card must be a mapping, got {card!r}")
            continue
        card_type = card.get("type")
        if not isinstance(card_type, str) or not card_type.strip():
            errors.append(f"card is missing required 'type': {card!r}")
        elif card_type.startswith("custom:"):
            errors.append(f"custom HACS card type is not allowed: {card_type}")

        nested_cards = card.get("cards")
        if isinstance(nested_cards, list):
            _validate_cards(nested_cards, errors)

        nested_single = card.get("card")
        if isinstance(nested_single, dict):
            _validate_cards([nested_single], errors)


def validate(config: dict[str, Any], entity_ids: set[str] | list[str]) -> list[str]:
    """Validate dashboard structure, card types, and entity references."""
    errors: list[str] = []
    if not isinstance(config, dict):
        return ["dashboard config must be a mapping"]

    views = config.get("views")
    if not isinstance(views, list) or len(views) == 0:
        errors.append("dashboard has no views")
        return errors

    seen_paths: set[str] = set()
    for idx, view in enumerate(views):
        if not isinstance(view, dict):
            errors.append(f"view #{idx} is not a mapping")
            continue

        title = view.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"view #{idx} is missing title")

        path = view.get("path")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"view #{idx} is missing path")
        else:
            if not VIEW_PATH_RE.match(path):
                errors.append(f"view path '{path}' must match ^[a-z0-9_-]+$")
            if path in seen_paths:
                errors.append(f"duplicate view path '{path}'")
            seen_paths.add(path)

        if "cards" not in view or not isinstance(view.get("cards"), list):
            errors.append(f"view '{path or idx}' is missing cards list")
        else:
            _validate_cards(view["cards"], errors)

    known = set(entity_ids)
    for entity_id in sorted(collect_entity_ids(config)):
        if entity_id not in known:
            errors.append(f"unknown entity '{entity_id}'")

    return errors


def fetch_live_entity_ids(ha_url: str, ha_token: str) -> set[str]:
    """Fetch current entity IDs from Home Assistant REST API."""
    url = ha_url.rstrip("/") + "/api/states"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {ha_token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, list):
        return {
            item["entity_id"]
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
        }
    if isinstance(payload, dict):
        return set(payload.keys())
    return set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Kitchen Lovelace dashboard YAML.")
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG,
        help="Path to dashboard YAML file.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Validate against live HA_URL / HA_TOKEN instead of fixture.",
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.live:
        ha_url = os.environ.get("HA_URL", "").strip()
        ha_token = os.environ.get("HA_TOKEN", "").strip()
        if not ha_url or not ha_token:
            print("HA_URL and HA_TOKEN must be set in environment for --live", file=sys.stderr)
            return 1
        entity_ids = fetch_live_entity_ids(ha_url, ha_token)
    else:
        entity_ids = set(json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8")))

    errors = validate(config, entity_ids)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
