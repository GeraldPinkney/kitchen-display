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
    # graphical-session.target cannot be started on this image (RefuseManualStart),
    # so the unit is asked for by name from labwc's autostart, which only runs once
    # the compositor is up.
    assert d.get("PartOf") == ["graphical-session.target"]
    assert d.get("WantedBy") == ["graphical-session.target"]


def test_command_is_exec_not_shell_wrapped(repo_root):
    d = directives(repo_root)
    assert d.get("Type") == ["simple"]
    assert not any("sh -c" in v for v in d.get("ExecStart", []))


def test_session_target_is_not_used_as_a_trigger(repo_root):
    """graphical-session.target is RefuseManualStart=yes and never activates here.

    Attempting the dependency-target pattern (own target BindsTo graphical-session.target)
    left the kiosk stopped on this image, so the contract is: no target file, start by name.
    """
    assert not (repo_root / "kiosk" / "kitchen-session.target").exists()


def test_labwc_autostart_starts_all_units_by_name(repo_root):
    """Autostart cannot use a session target here, so it asks systemd for the units."""
    path = repo_root / "kiosk" / "autostart"
    if not path.is_file():
        pytest.fail("missing kiosk/autostart")
    body = path.read_text()
    start_lines = [l for l in body.splitlines() if "systemctl --user start" in l]
    assert any("kitchen-display.service" in l for l in start_lines), body
    assert any("kitchen-display-http.service" in l for l in start_lines), body
    assert any("kitchen-idle-policy.service" in l for l in start_lines), body
    # labwc's autostart must not block: every unit start has to be backgrounded.
    assert all(l.rstrip().endswith("&") for l in start_lines), start_lines
    # No session-target indirection: graphical-session.target is RefuseManualStart.
    assert not any("session.target) " in l or "start graphical-session.target" in l for l in start_lines), start_lines


def test_labwc_autostart_publishes_wayland_env_before_starting_the_target(repo_root):
    body = (repo_root / "kiosk" / "autostart").read_text()
    lines = [l.strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("#")]
    imports = [i for i, l in enumerate(lines) if "import-environment" in l]
    assert imports, "autostart does not import the compositor environment"
    assert "WAYLAND_DISPLAY" in lines[imports[0]], lines[imports[0]]
    starts = [i for i, l in enumerate(lines) if "kitchen-display.service" in l]
    assert starts and imports[0] < starts[0], "env must be published before the kiosk starts"


def test_labwc_autostart_has_no_sleep_race(repo_root):
    body = (repo_root / "kiosk" / "autostart").read_text()
    offenders = [
        l.strip() for l in body.splitlines()
        if l.strip() and not l.strip().startswith("#") and "sleep" in l
    ]
    assert not offenders, f"autostart still sleeps instead of relying on systemd order: {offenders}"


def _autostart_logical_lines(body: str) -> list[str]:
    logical: list[str] = []
    buf = ""
    for raw in body.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1].strip() + " "
        else:
            logical.append((buf + s).strip())
            buf = ""
    if buf:
        logical.append(buf.strip())
    return logical


def test_labwc_autostart_keeps_kanshi_and_never_blocks(repo_root):
    path = repo_root / "kiosk" / "autostart"
    body = path.read_text()
    active_lines = _autostart_logical_lines(body)
    assert any("kanshi" in l for l in active_lines), active_lines
    assert any("kitchen-idle-policy.service" in l for l in active_lines), active_lines
    # Anything that can wait on another process (unit start, daemon) is backgrounded;
    # import-environment is a fast local call and stays synchronous by design.
    waiting = [l for l in active_lines if "systemctl --user start" in l or "kanshi)" in l]
    assert all(l.endswith("&") for l in waiting), waiting


def test_labwc_autostart_leaves_the_idle_timers_to_the_policy_unit(repo_root):
    """#32: an inline swayidle line would hold a stale timeout and win over the policy."""
    body = (repo_root / "kiosk" / "autostart").read_text()
    offenders = [l for l in _autostart_logical_lines(body) if l.startswith("swayidle")]
    assert not offenders, f"autostart must not run swayidle itself: {offenders}"

    unit = (repo_root / "kiosk" / "kitchen-idle-policy.service").read_text()
    assert "ExecStart=%h/.local/bin/swayidle-policy --run" in unit, unit
    assert "Restart=always" in unit, "a crashed policy must come back, not leave the panel lit"
    assert "Environment=PATH=%h/.local/bin:" in unit, "photo-switch and wlopm must be on PATH"
    assert "Environment=WAYLAND_DISPLAY=" in unit, "swayidle needs the compositor socket"


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
