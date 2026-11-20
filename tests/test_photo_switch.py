"""Offline specs for tools/photo_switch.py using local HTTP and WebSocket CDP fakes."""
from __future__ import annotations

import asyncio
import http.server
import json
import socket
import socketserver
import threading
import time
from urllib.parse import quote

import pytest
import websockets

from conftest import load_tool


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module")
def switch_mod():
    return load_tool("photo_switch")


@pytest.fixture
def fake_photo_http():
    state = {"status": 200, "head_calls": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_HEAD(self):  # noqa: N802
            state["head_calls"] += 1
            self.send_response(state["status"])
            self.end_headers()

    server = _ThreadedHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield state, f"http://{host}:{port}/photo.html"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def fake_cdp():
    state = {
        "commands": [],
        "targets": None,
        "hang_ws": False,
        "cdp_error": None,
        "json_calls": 0,
    }
    ready = threading.Event()
    ws_port_box = {}
    stop_box = {}

    async def ws_handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            state["commands"].append(msg)
            if state["hang_ws"]:
                await stop_box["event"].wait()
                return
            # Emit an unrelated CDP event before replying to verify id matching.
            await ws.send(json.dumps({"method": "Page.frameStartedLoading", "params": {"frameId": "f1"}}))
            if state["cdp_error"] is not None:
                await ws.send(json.dumps({"id": msg["id"], "error": state["cdp_error"]}))
            else:
                await ws.send(json.dumps({"id": msg["id"], "result": {"frameId": "f1"}}))

    def run_ws_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()
        stop_box["loop"] = loop
        stop_box["event"] = stop_event

        async def runner():
            async with websockets.serve(ws_handler, "127.0.0.1", 0) as srv:
                ws_port_box["port"] = srv.sockets[0].getsockname()[1]
                ready.set()
                await stop_event.wait()

        loop.run_until_complete(runner())
        loop.close()

    ws_thread = threading.Thread(target=run_ws_loop, daemon=True)
    ws_thread.start()
    assert ready.wait(timeout=5)
    ws_url = f"ws://127.0.0.1:{ws_port_box['port']}/devtools/page/1"
    state["ws_url"] = ws_url

    class CDPHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            return

        def do_GET(self):  # noqa: N802
            state["json_calls"] += 1
            if self.path != "/json/list":
                self.send_response(404)
                self.end_headers()
                return
            targets = state["targets"]
            if targets is None:
                targets = [
                    {"type": "service_worker", "webSocketDebuggerUrl": "ws://127.0.0.1:1/sw"},
                    {"type": "page", "webSocketDebuggerUrl": ws_url},
                ]
            body = json.dumps(targets).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    http_srv = _ThreadedHTTPServer(("127.0.0.1", 0), CDPHandler)
    http_thread = threading.Thread(target=http_srv.serve_forever, daemon=True)
    http_thread.start()
    try:
        host, port = http_srv.server_address
        yield state, f"http://{host}:{port}"
    finally:
        http_srv.shutdown()
        http_srv.server_close()
        stop_box["loop"].call_soon_threadsafe(stop_box["event"].set)
        ws_thread.join(timeout=2)


def test_show_navigates_when_photo_page_returns_200(switch_mod, fake_photo_http, fake_cdp, monkeypatch, capsys):
    photo_state, photo_url = fake_photo_http
    cdp_state, cdp_url = fake_cdp
    dash_url = "http://ha.local:8123/kitchen-display/home?kiosk"

    monkeypatch.setenv("PHOTO_PAGE_URL", photo_url)
    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)
    monkeypatch.setenv("KIOSK_DASHBOARD_URL", dash_url)

    rc = switch_mod.main(["show"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert photo_state["head_calls"] == 1
    assert len(cdp_state["commands"]) == 1
    cmd = cdp_state["commands"][0]
    assert cmd["id"] == 1
    assert cmd["method"] == "Page.navigate"
    assert cmd["params"]["url"] == f"{photo_url}?return={quote(dash_url, safe='')}"


def test_show_is_noop_when_photo_page_not_200(switch_mod, fake_photo_http, fake_cdp, monkeypatch, capsys):
    photo_state, photo_url = fake_photo_http
    cdp_state, cdp_url = fake_cdp
    photo_state["status"] = 404

    monkeypatch.setenv("PHOTO_PAGE_URL", photo_url)
    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)

    rc = switch_mod.main(["show"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert cdp_state["json_calls"] == 0
    assert cdp_state["commands"] == []


def test_hide_navigates_to_dashboard_url(switch_mod, fake_cdp, monkeypatch, capsys):
    cdp_state, cdp_url = fake_cdp
    dash_url = "http://ha.local:8123/kitchen-display/home?kiosk"

    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)
    monkeypatch.setenv("KIOSK_DASHBOARD_URL", dash_url)

    rc = switch_mod.main(["hide"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert len(cdp_state["commands"]) == 1
    assert cdp_state["commands"][0] == {
        "id": 1,
        "method": "Page.navigate",
        "params": {"url": dash_url},
    }


def test_hide_uses_kiosk_url_file_when_no_override(switch_mod, fake_cdp, monkeypatch, capsys, tmp_path):
    cdp_state, cdp_url = fake_cdp
    url_file = tmp_path / "url"
    url_file.write_text("http://ha.local:8123/kitchen-display/energy?kiosk\n", encoding="utf-8")

    monkeypatch.delenv("KIOSK_DASHBOARD_URL", raising=False)
    monkeypatch.setenv("KIOSK_URL_FILE", str(url_file))
    monkeypatch.setenv("KIOSK_URL", "http://stale.example/history")
    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)

    rc = switch_mod.main(["hide"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert cdp_state["commands"][0]["params"]["url"] == "http://ha.local:8123/kitchen-display/energy?kiosk"


def test_hide_falls_back_to_default_when_url_file_absent(switch_mod, monkeypatch):
    missing = "/nonexistent/kitchen-display/url"
    monkeypatch.delenv("KIOSK_DASHBOARD_URL", raising=False)
    monkeypatch.delenv("KIOSK_URL", raising=False)
    monkeypatch.setenv("KIOSK_URL_FILE", missing)
    assert switch_mod.resolve_dashboard_url() == switch_mod.DEFAULT_DASHBOARD_URL
    assert switch_mod.resolve_dashboard_url("http://explicit.local/x") == "http://explicit.local/x"


def test_needs_venv_interpreter_uses_prefix_not_resolved_executable(switch_mod, tmp_path):
    """A venv's bin/python symlinks the system interpreter, so sys.prefix decides."""
    fake_venv = tmp_path / "photoswitch"
    (fake_venv / "bin").mkdir(parents=True)
    fake_python = fake_venv / "bin" / "python"
    fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
    fake_python.chmod(0o755)

    assert switch_mod._needs_venv_interpreter("/usr", fake_python) is True
    assert switch_mod._needs_venv_interpreter(str(fake_venv), fake_python) is False
    assert switch_mod._needs_venv_interpreter("/usr", tmp_path / "missing" / "bin" / "python") is False


def test_cdp_port_closed_exits_1_with_prefixed_line(switch_mod, monkeypatch, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    monkeypatch.setenv("CDP_ENDPOINT", f"http://127.0.0.1:{closed_port}")
    rc = switch_mod.main(["hide"])
    captured = capsys.readouterr()
    assert rc == 1
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("photo-switch: ")


def test_no_page_target_exits_1_with_prefixed_line(switch_mod, fake_cdp, monkeypatch, capsys):
    cdp_state, cdp_url = fake_cdp
    cdp_state["targets"] = [{"type": "background_page", "webSocketDebuggerUrl": cdp_state["ws_url"]}]
    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)

    rc = switch_mod.main(["hide"])
    captured = capsys.readouterr()
    assert rc == 1
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("photo-switch: ")


def test_ws_timeout_is_bounded_and_exits_1(switch_mod, fake_cdp, monkeypatch, capsys):
    cdp_state, cdp_url = fake_cdp
    cdp_state["hang_ws"] = True
    monkeypatch.setenv("CDP_ENDPOINT", cdp_url)
    monkeypatch.setattr(switch_mod, "CDP_TIMEOUT", 0.5)

    start = time.monotonic()
    rc = switch_mod.main(["hide"])
    elapsed = time.monotonic() - start
    captured = capsys.readouterr()
    assert rc == 1
    assert elapsed < 3.0
    lines = captured.err.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("photo-switch: ")
