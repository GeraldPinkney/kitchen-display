"""Specs for kiosk/launch_kiosk.sh — URL resolution and Wayland flags.

The script is exercised for real: a shim named `chromium` earlier on PATH
records its argv, so the assertions are about what actually gets executed.
"""
import os
import pathlib
import subprocess

import pytest

SCRIPT = "kiosk/launch_kiosk.sh"
DEFAULT_URL = "http://192.168.178.84/kitchen-display/home?kiosk"


def run_launcher(repo_root, tmp_path, extra_env=None, url_file=None, default_url_file=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    argfile = tmp_path / "chromium.args"
    shim = bin_dir / "chromium"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argfile}"\n')
    shim.chmod(0o755)

    script = repo_root / SCRIPT
    if not script.is_file():
        pytest.fail(f"missing {SCRIPT}")

    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    if not default_url_file:
        env["KIOSK_URL_FILE"] = str(url_file if url_file is not None else tmp_path / "absent-url")
    env.update(extra_env or {})
    proc = subprocess.run(
        ["/bin/bash", str(script)], env=env, cwd=str(tmp_path),
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return argfile.read_text().splitlines()


def test_default_url_is_the_ha_kitchen_dashboard(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path)
    assert args[-1] == DEFAULT_URL
    assert "--kiosk" in args
    assert "--ozone-platform=wayland" in args


def test_kiosk_url_env_overrides_default(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path, extra_env={"KIOSK_URL": "http://example.test/page"})
    assert args[-1] == "http://example.test/page"


def test_url_file_wins_over_env(repo_root, tmp_path):
    url_file = tmp_path / "url"
    url_file.write_text("http://example.test/from-file\n")
    args = run_launcher(
        repo_root, tmp_path,
        extra_env={"KIOSK_URL": "http://example.test/from-env"}, url_file=url_file,
    )
    assert args[-1] == "http://example.test/from-file"


def test_empty_url_file_falls_back_to_default(repo_root, tmp_path):
    url_file = tmp_path / "url"
    url_file.write_text("   \n")
    args = run_launcher(repo_root, tmp_path, url_file=url_file)
    assert args[-1] == DEFAULT_URL


def test_default_url_file_is_read_from_the_config_dir(repo_root, tmp_path):
    # With no KIOSK_URL_FILE override the launcher must read
    # $HOME/.config/kitchen-display/url, which is what install.sh writes.
    config_dir = tmp_path / ".config" / "kitchen-display"
    config_dir.mkdir(parents=True)
    (config_dir / "url").write_text("http://example.test/installed\n")
    args = run_launcher(repo_root, tmp_path, default_url_file=True)
    assert args[-1] == "http://example.test/installed"


def test_launcher_suppresses_browser_chrome(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path)
    for flag in ("--noerrdialogs", "--disable-infobars", "--no-first-run",
                 "--disable-session-crashed-bubble"):
        assert flag in args, flag


def test_launcher_uses_basic_password_store(repo_root, tmp_path):
    # No login keyring exists for the autologin kiosk user, so chromium's
    # cookie-store key load blocks on libsecret forever and the panel stays
    # white. --password-store=basic bypasses the keyring entirely.
    args = run_launcher(repo_root, tmp_path)
    assert "--password-store=basic" in args, args


def test_default_scale_factor_is_1_5_for_10_inch_panel(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path)
    assert "--force-device-scale-factor=1.5" in args, args
    assert "--touch-events=enabled" in args, args


def test_kiosk_scale_env_overrides_default_scale_factor(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path, extra_env={"KIOSK_SCALE": "1.25"})
    assert "--force-device-scale-factor=1.25" in args, args


def test_launcher_enables_loopback_cdp_flags_before_url(repo_root, tmp_path):
    args = run_launcher(repo_root, tmp_path)
    assert args.count("--remote-debugging-port=9222") == 1, args
    assert args.count("--remote-allow-origins=http://127.0.0.1") == 1, args
    url_idx = len(args) - 1
    assert args.index("--remote-debugging-port=9222") < url_idx
    assert args.index("--remote-allow-origins=http://127.0.0.1") < url_idx
