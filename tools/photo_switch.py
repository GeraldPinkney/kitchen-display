#!/usr/bin/env python3
"""Switch the Chromium kiosk tab between the HA dashboard and the photo frame."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_DASHBOARD_URL = "http://192.168.178.84/kitchen-display/home?kiosk"
DEFAULT_URL_FILE = pathlib.Path.home() / ".config" / "kitchen-display" / "url"
DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_PHOTO_PAGE_URL = "http://127.0.0.1:8788/photo.html"
HTTP_TIMEOUT = 2.0
CDP_TIMEOUT = 3.0


class PhotoSwitchError(Exception):
    """Raised when CDP discovery or navigation fails."""


def _ensure_venv() -> None:
    try:
        import websockets  # noqa: F401
        return
    except ImportError:
        venv_python = pathlib.Path.home() / ".local" / "venvs" / "photoswitch" / "bin" / "python"
        if venv_python.is_file() and pathlib.Path(sys.executable).resolve() != venv_python.resolve():
            os.execv(str(venv_python), [str(venv_python), *sys.argv])


def _read_url_file() -> str:
    """Return the dashboard URL the kiosk was launched with, or "" if unreadable.

    Mirrors kiosk/launch_kiosk.sh: ~/.config/kitchen-display/url is the kiosk's own
    source of truth, so `hide` returns to the page the panel actually started on.
    """
    path = pathlib.Path(os.environ.get("KIOSK_URL_FILE") or DEFAULT_URL_FILE)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve_dashboard_url(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    override = os.environ.get("KIOSK_DASHBOARD_URL", "").strip()
    if override:
        return override
    file_url = _read_url_file()
    if file_url:
        return file_url
    env_url = os.environ.get("KIOSK_URL", "").strip()
    return env_url or DEFAULT_DASHBOARD_URL


def _is_photo_page_ready(photo_url: str, timeout: float | None = None) -> bool:
    eff_timeout = HTTP_TIMEOUT if timeout is None else timeout
    req = Request(photo_url, method="HEAD")
    try:
        with urlopen(req, timeout=eff_timeout) as resp:
            return resp.status == 200
    except (HTTPError, URLError, OSError):
        return False


def _find_page_ws_url(cdp_endpoint: str, timeout: float | None = None) -> str:
    eff_timeout = CDP_TIMEOUT if timeout is None else timeout
    list_url = f"{cdp_endpoint.rstrip('/')}/json/list"
    req = Request(list_url, method="GET")
    try:
        with urlopen(req, timeout=eff_timeout) as resp:
            if resp.status != 200:
                raise PhotoSwitchError(f"CDP {list_url} returned HTTP {resp.status}")
            payload = json.loads(resp.read().decode("utf-8"))
    except PhotoSwitchError:
        raise
    except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhotoSwitchError(f"failed to query CDP {list_url}: {exc}") from exc

    if not isinstance(payload, list):
        raise PhotoSwitchError("CDP /json/list did not return a JSON list")

    for target in payload:
        if isinstance(target, dict) and target.get("type") == "page":
            ws_url = target.get("webSocketDebuggerUrl")
            if isinstance(ws_url, str) and ws_url:
                return ws_url

    raise PhotoSwitchError("no page target found in CDP /json/list")


async def _cdp_navigate(ws_url: str, target_url: str, timeout: float | None = None) -> None:
    import websockets

    eff_timeout = CDP_TIMEOUT if timeout is None else timeout
    close_timeout = min(0.5, eff_timeout)

    async def _run() -> None:
        async with websockets.connect(ws_url, open_timeout=eff_timeout, close_timeout=close_timeout) as ws:
            cmd = {"id": 1, "method": "Page.navigate", "params": {"url": target_url}}
            await ws.send(json.dumps(cmd))
            while True:
                raw = await ws.recv()
                msg = json.loads(raw)
                if isinstance(msg, dict) and msg.get("id") == 1:
                    if "error" in msg and msg["error"] is not None:
                        raise PhotoSwitchError(f"CDP Page.navigate error: {msg['error']}")
                    return

    try:
        await asyncio.wait_for(_run(), timeout=eff_timeout)
    except PhotoSwitchError:
        raise
    except Exception as exc:
        raise PhotoSwitchError(f"CDP WebSocket failure: {exc}") from exc


def navigate_kiosk(target_url: str, cdp_endpoint: str, timeout: float | None = None) -> None:
    eff_timeout = CDP_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + eff_timeout
    ws_url = _find_page_ws_url(cdp_endpoint, timeout=eff_timeout)
    remaining = max(0.2, deadline - time.monotonic())
    asyncio.run(_cdp_navigate(ws_url, target_url, timeout=remaining))


def run_action(
    action: str,
    *,
    dashboard_url: str | None = None,
    cdp_endpoint: str | None = None,
    photo_page_url: str | None = None,
) -> int:
    dash = resolve_dashboard_url(dashboard_url)
    cdp = (cdp_endpoint or os.environ.get("CDP_ENDPOINT") or DEFAULT_CDP_ENDPOINT).strip()
    photo = (photo_page_url or os.environ.get("PHOTO_PAGE_URL") or DEFAULT_PHOTO_PAGE_URL).strip()

    if action == "show":
        if not _is_photo_page_ready(photo):
            return 0
        sep = "&" if "?" in photo else "?"
        target = f"{photo}{sep}{urlencode({'return': dash})}"
    elif action == "hide":
        target = dash
    else:
        raise PhotoSwitchError(f"unknown action: {action}")

    navigate_kiosk(target, cdp)
    return 0


def main(argv: list[str] | None = None) -> int:
    _ensure_venv()
    parser = argparse.ArgumentParser(description="Switch kiosk Chromium tab to or from photo frame.")
    parser.add_argument("action", choices=("show", "hide"), help="show photo frame or hide back to dashboard.")
    args = parser.parse_args(argv)

    try:
        return run_action(args.action)
    except Exception as exc:
        msg = str(exc).replace("\n", " ")
        print(f"photo-switch: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
