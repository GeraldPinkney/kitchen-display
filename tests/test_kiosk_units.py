"""Specs for the kiosk systemd user unit and the labwc autostart entry."""
import pytest


def unit_path(repo_root):
    path = repo_root / "kiosk" / "kitchen-display.service"
    if not path.is_file():
        pytest.fail("missing kiosk/kitchen-display.service")
    return path


def directives(repo_root):
    """Return {key: [values]} for the unit file (last duplicate wins per key)."""
    out = {}
    for line in unit_path(repo_root).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(key, []).append(value)
    return out


def test_restarts_after_crash(repo_root):
    d = directives(repo_root)
    assert d.get("Restart") == ["always"]
    assert "RestartSec" in d


def test_runs_the_launcher_not_chromium_directly(repo_root):
    d = directives(repo_root)
    exec_start = d["ExecStart"][0]
    assert "launch_kiosk.sh" in exec_start
    assert not exec_start.strip().startswith("chromium")


def test_reads_url_and_settings_from_a_private_env_file(repo_root):
    d = directives(repo_root)
    assert any("kitchen-display/env" in v for v in d.get("EnvironmentFile", [])), d.get("EnvironmentFile")


def test_starts_with_the_wayland_session(repo_root):
    d = directives(repo_root)
    assert d.get("WantedBy") == ["graphical-session.target"]


def test_command_is_exec_not_shell_wrapped(repo_root):
    d = directives(repo_root)
    assert d.get("Type") == ["simple"]
    assert not any("sh -c" in v for v in d.get("ExecStart", []))


def test_labwc_autostart_starts_the_user_unit(repo_root):
    path = repo_root / "kiosk" / "autostart"
    if not path.is_file():
        pytest.fail("missing kiosk/autostart")
    body = path.read_text()
    start_lines = [l for l in body.splitlines() if "systemctl --user start kitchen-display.service" in l]
    assert start_lines, "autostart does not start the user unit"
    # labwc's autostart must not block: every entry has to be backgrounded.
    assert all(l.rstrip().endswith("&") for l in start_lines), start_lines


def test_labwc_autostart_keeps_kanshi_and_never_blocks(repo_root):
    path = repo_root / "kiosk" / "autostart"
    body = path.read_text()
    active_lines = [
        l.strip() for l in body.splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]
    assert any("kanshi" in l for l in active_lines), active_lines
    assert any("kitchen-display-http.service" in l for l in active_lines), active_lines
    assert all(l.endswith("&") for l in active_lines), active_lines


def http_unit_directives(repo_root):
    path = repo_root / "kiosk" / "kitchen-display-http.service"
    if not path.is_file():
        pytest.fail("missing kiosk/kitchen-display-http.service")
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out.setdefault(key, []).append(value)
    return out


def test_http_service_serves_kiosk_data_dir_on_port_8788(repo_root):
    d = http_unit_directives(repo_root)
    assert d.get("Type") == ["simple"]
    assert d.get("Restart") == ["always"]
    assert "RestartSec" in d
    assert d.get("WantedBy") == ["default.target"]
    exec_start = d.get("ExecStart", [""])[0]
    assert "http.server" in exec_start and "8788" in exec_start, exec_start
    assert "0.0.0.0" in exec_start, exec_start
    assert "%h/.local/share/kitchen-display" in exec_start, exec_start
