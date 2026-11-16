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
    assert "systemctl --user start kitchen-display.service" in body
    assert "&" in body or "start" in body
