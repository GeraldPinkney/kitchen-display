"""Specs for provision/install.sh — the Pi provisioning script.

Behaviour is tested through --dry-run, which must plan the work without
touching the system, and must be deterministic so two runs can be diffed.
"""
import os
import pathlib
import shutil
import subprocess

import pytest

SCRIPT = "provision/install.sh"


def script_path(repo_root):
    p = repo_root / SCRIPT
    if not p.is_file():
        pytest.fail(f"missing {SCRIPT}")
    return p


def run_dry(repo_root):
    return subprocess.run(
        ["/bin/bash", str(script_path(repo_root)), "--dry-run"],
        capture_output=True, text=True, timeout=60, cwd=str(repo_root),
    )


def test_script_parses(repo_root):
    proc = subprocess.run(["/bin/bash", "-n", str(script_path(repo_root))],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_dry_run_succeeds_and_plans_every_component(repo_root):
    proc = run_dry(repo_root)
    assert proc.returncode == 0, proc.stderr
    out = (proc.stdout + proc.stderr).lower()
    for needle in ("apt", "chromium", "swayidle", "wlopm", "kanshi",
                   "kitchen-display.service", "kitchen-display-http.service",
                   ".local/share/kitchen-display", "autostart", "reboot",
                   "sync-photos.py", "photo-switch", "photo.html",
                   "photoswitch", "websockets", "*/30"):
        assert needle in out, f"dry-run output does not mention {needle}:\n{proc.stdout}"


def test_install_has_no_sudo_and_ships_photo_frame_cron_idempotently(repo_root):
    body = script_path(repo_root).read_text()
    assert "sudo " not in body
    assert '*/30 * * * * $HOME/.local/bin/sync-photos.py >> $HOME/.local/state/kitchen-display/photo-sync.log 2>&1' in body
    assert ".local/state/kitchen-display" in body
    assert ".local/share/kitchen-display" in body
    assert ".local/venvs" in body


def test_dry_run_honours_kiosk_user_override(repo_root):
    env = dict(os.environ, KIOSK_USER="hermes")
    proc = subprocess.run(
        ["/bin/bash", str(script_path(repo_root)), "--dry-run"],
        capture_output=True, text=True, timeout=60, cwd=str(repo_root), env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "/home/hermes/.config/kitchen-display" in proc.stdout
    assert "/home/hermes/.local/share/kitchen-display" in proc.stdout


def test_install_passes_kiosk_user_as_sudo_user_to_raspi_config(repo_root):
    body = script_path(repo_root).read_text()
    assert 'SUDO_USER="${KIOSK_USER}" raspi-config nonint do_boot_behaviour B4' in body


def test_dry_run_is_deterministic(repo_root):
    first, second = run_dry(repo_root), run_dry(repo_root)
    assert first.stdout == second.stdout


def test_dry_run_writes_nothing_outside_the_repo(repo_root, tmp_path):
    before = set(pathlib.Path("/etc").iterdir())
    proc = run_dry(repo_root)
    assert proc.returncode == 0, proc.stderr
    assert set(pathlib.Path("/etc").iterdir()) == before


def test_refuses_to_run_for_real_without_root(repo_root):
    proc = subprocess.run(["/bin/bash", str(script_path(repo_root))],
                          capture_output=True, text=True, timeout=30, cwd=str(repo_root))
    assert proc.returncode != 0
    assert "root" in (proc.stderr + proc.stdout).lower()


def test_has_no_embedded_secrets(repo_root):
    body = script_path(repo_root).read_text()
    for marker in ("eyJ", "HA_TOKEN=", "Bearer "):
        assert marker not in body, f"possible secret material in install.sh: {marker}"


def test_shellcheck_clean_when_available(repo_root):
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    proc = subprocess.run(["shellcheck", "-S", "warning", str(script_path(repo_root))],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
