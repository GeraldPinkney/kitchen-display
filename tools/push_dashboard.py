#!/usr/bin/env python3
"""Create or update the Kitchen Lovelace dashboard over the Home Assistant WebSocket API."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import pathlib
import sys
from typing import Any, Callable
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "ha" / "kitchen_dashboard.yaml"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "ha_states.json"

DASHBOARD_URL_PATH = "kitchen-display"
DASHBOARD_TITLE = "Kitchen"
DASHBOARD_ICON = "mdi:silverware-fork-knife"


class HAError(Exception):
    """Raised when Home Assistant returns a failed command result."""


def _check_result(response: dict[str, Any]) -> Any:
    if not isinstance(response, dict) or not response.get("success", False):
        error = response.get("error") if isinstance(response, dict) else None
        if isinstance(error, dict):
            msg = error.get("message") or error.get("code") or str(error)
        else:
            msg = str(error or response)
        raise HAError(msg)
    return response.get("result")


def ensure_dashboard(
    call: Callable[..., dict[str, Any]],
    dashboards: list[dict[str, Any]] | None,
    url_path: str,
    title: str,
    icon: str,
) -> dict[str, Any]:
    """Return existing dashboard matching url_path or create a new storage dashboard."""
    if dashboards is None:
        dashboards = _check_result(call("lovelace/dashboards/list")) or []

    for dash in dashboards:
        if isinstance(dash, dict) and dash.get("url_path") == url_path:
            return dash

    resp = call(
        "lovelace/dashboards/create",
        url_path=url_path,
        title=title,
        icon=icon,
        mode="storage",
        show_in_sidebar=True,
    )
    result = _check_result(resp)
    return result if isinstance(result, dict) else {"url_path": url_path, "title": title, "mode": "storage"}


def push_config(
    call: Callable[..., dict[str, Any]],
    url_path: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Save Lovelace dashboard configuration for url_path."""
    resp = call("lovelace/config/save", url_path=url_path, config=config)
    _check_result(resp)
    return resp


def _load_dashboard_check():
    check_path = pathlib.Path(__file__).resolve().parent / "dashboard_check.py"
    spec = importlib.util.spec_from_file_location("dashboard_check", check_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {check_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ws_url(ha_url: str) -> str:
    parsed = urlparse(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path}/api/websocket"


async def _apply_over_websocket(ha_url: str, ha_token: str, config: dict[str, Any]) -> None:
    import websockets

    ws_uri = _ws_url(ha_url)
    async with websockets.connect(ws_uri) as ws:
        greeting = json.loads(await ws.recv())
        if greeting.get("type") != "auth_required":
            raise HAError(f"unexpected websocket greeting: {greeting}")

        await ws.send(json.dumps({"type": "auth", "access_token": ha_token}))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("type") != "auth_ok":
            raise HAError(f"websocket authentication failed: {auth_resp}")

        msg_id = 0

        async def async_call(type_: str, **payload: Any) -> dict[str, Any]:
            nonlocal msg_id
            msg_id += 1
            frame = {"id": msg_id, "type": type_, **payload}
            await ws.send(json.dumps(frame))
            while True:
                raw = json.loads(await ws.recv())
                if raw.get("id") == msg_id:
                    return raw

        dashboards_resp = await async_call("lovelace/dashboards/list")
        dashboards = _check_result(dashboards_resp) or []

        queued: list[tuple[str, dict[str, Any]]] = []

        def sync_recorder(type_: str, **payload: Any) -> dict[str, Any]:
            queued.append((type_, payload))
            return {"success": True, "result": {"url_path": payload.get("url_path", DASHBOARD_URL_PATH)}}

        ensure_dashboard(sync_recorder, dashboards, DASHBOARD_URL_PATH, DASHBOARD_TITLE, DASHBOARD_ICON)
        push_config(sync_recorder, DASHBOARD_URL_PATH, config)

        for type_, payload in queued:
            resp = await async_call(type_, **payload)
            _check_result(resp)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push Kitchen Lovelace dashboard to Home Assistant.")
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG,
        help="Path to dashboard YAML file.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes to Home Assistant via WebSocket (default is dry-run).",
    )
    args = parser.parse_args(argv)

    dc = _load_dashboard_check()
    config = dc.load_config(args.config)
    entity_ids = set(json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8")))
    errors = dc.validate(config, entity_ids)
    if errors:
        for err in errors:
            print(err, file=sys.stderr)
        return 1

    if not args.apply:
        dry_calls: list[dict[str, Any]] = []

        def dry_call(type_: str, **payload: Any) -> dict[str, Any]:
            dry_calls.append({"type": type_, **payload})
            return {"success": True, "result": {"url_path": DASHBOARD_URL_PATH}}

        ensure_dashboard(dry_call, [], DASHBOARD_URL_PATH, DASHBOARD_TITLE, DASHBOARD_ICON)
        push_config(dry_call, DASHBOARD_URL_PATH, config)
        print(json.dumps(dry_calls, indent=2))
        return 0

    ha_url = os.environ.get("HA_URL", "").strip()
    ha_token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not ha_token:
        print("HA_URL and HA_TOKEN must be set in environment for --apply", file=sys.stderr)
        return 1

    asyncio.run(_apply_over_websocket(ha_url, ha_token, config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
