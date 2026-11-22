"""Specs for provision/install.sh — the Pi provisioning script.

Behaviour is tested through --dry-run, which must plan the work without
touching the system, and must be deterministic so two runs can be diffed.
"""
import getpass
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

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
                   "kitchen-idle-policy.service", "swayidle-policy",
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


PHOTO_SYNC_SOURCES = (
    ("sync_photos.py", "sync-photos.py"),
    ("photo_select.py", "photo_select.py"),
    ("photo_format.py", "photo_format.py"),
    ("photo_cache.py", "photo_cache.py"),
)


def _stage_and_run_installer(repo_root, tmp_path, *, missing_module=None, temp_home=None):
    staged = tmp_path / "repo"
    if not staged.exists():
        (staged / "provision").mkdir(parents=True)
        shutil.copytree(repo_root / "kiosk", staged / "kiosk")
        shutil.copytree(repo_root / "tools", staged / "tools")
        shutil.copytree(repo_root / "provision" / "system", staged / "provision" / "system")

        model_file = tmp_path / "pi-model"
        model_file.write_text("Raspberry Pi 5 Model B\n")
        systemd_dir = tmp_path / "systemd"
        systemd_dir.mkdir()

        script_text = script_path(repo_root).read_text()
        patched = (
            script_text
            .replace('"${EUID}" -ne 0', "0 -ne 0")
            .replace("/proc/device-tree/model", str(model_file))
            .replace("/etc/systemd/system", str(systemd_dir))
        )
        staged_script = staged / SCRIPT
        staged_script.write_text(patched)
        staged_script.chmod(0o755)

    if missing_module is not None:
        (staged / "tools" / missing_module).unlink()

    if temp_home is None:
        temp_home = tmp_path / "home"
        temp_home.mkdir()

    venv_py = temp_home / ".local" / "venvs" / "photoswitch" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("#!/bin/sh\nexit 0\n")
    venv_py.chmod(0o755)

    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for cmd in ("dpkg", "apt-get", "raspi-config", "loginctl", "systemctl",
                "systemd-analyze"):
        shim = shims / cmd
        shim.write_text("#!/bin/sh\nexit 0\n")
        shim.chmod(0o755)
    crontab_shim = shims / "crontab"
    crontab_shim.write_text('#!/bin/sh\nfor a in "$@"; do [ "$a" = "-l" ] && exit 0; done\ncat >/dev/null\n')
    crontab_shim.chmod(0o755)

    user = getpass.getuser()
    env = dict(
        os.environ,
        HOME=str(temp_home),
        KIOSK_USER=user,
        KIOSK_HOME=str(temp_home),
        PATH=f"{shims}:{os.environ['PATH']}",
    )
    proc = subprocess.run(
        ["/bin/bash", str(staged / SCRIPT)],
        capture_output=True, text=True, timeout=60, cwd=str(staged), env=env,
    )
    return proc, temp_home


def test_photo_sync_modules_referenced_and_targeted_to_bin_dir(repo_root):
    body = script_path(repo_root).read_text()
    for src, _dest in PHOTO_SYNC_SOURCES:
        assert src in body, f"{src} missing from {SCRIPT}"
    assert 'SYNC_PHOTOS_DEST="${BIN_DIR}/sync-photos.py"' in body
    assert '"${BIN_DIR}/${mod}"' in body
    dry = run_dry(repo_root)
    assert dry.returncode == 0, dry.stderr
    for _src, dest in PHOTO_SYNC_SOURCES:
        assert dest in dry.stdout, f"{dest} missing from --dry-run output"


def test_install_ships_all_four_modules_and_they_import_as_a_set(repo_root, tmp_path):
    proc, temp_home = _stage_and_run_installer(repo_root, tmp_path)
    assert proc.returncode == 0, proc.stderr

    bin_dir = temp_home / ".local" / "bin"
    before_stats = {}
    for src, dest in PHOTO_SYNC_SOURCES:
        installed = bin_dir / dest
        assert installed.is_file(), f"missing installed {dest} in {bin_dir}"
        assert installed.read_text() == (repo_root / "tools" / src).read_text()
        before_stats[dest] = (installed.stat().st_ino, installed.stat().st_mtime_ns)

    # Second run skips unchanged files via cmp -s.
    proc2, _ = _stage_and_run_installer(repo_root, tmp_path, temp_home=temp_home)
    assert proc2.returncode == 0, proc2.stderr
    for _src, dest in PHOTO_SYNC_SOURCES:
        installed = bin_dir / dest
        assert (installed.stat().st_ino, installed.stat().st_mtime_ns) == before_stats[dest]

    if importlib.util.find_spec("PIL") is None or importlib.util.find_spec("requests") is None:
        pytest.skip("Pillow and requests are required to import installed photo sync modules")

    import_check = (
        "import importlib.util, pathlib, sys; "
        f"bin_dir = pathlib.Path({str(bin_dir)!r}); "
        "sys.path.insert(0, str(bin_dir)); "
        "import photo_cache, photo_format, photo_select; "
        "spec = importlib.util.spec_from_file_location('sync_photos', bin_dir / 'sync-photos.py'); "
        "mod = importlib.util.module_from_spec(spec); "
        "sys.modules['sync_photos'] = mod; "
        "spec.loader.exec_module(mod); "
        "assert pathlib.Path(mod.photo_cache.__file__).resolve().parent == bin_dir.resolve(); "
        "assert pathlib.Path(mod.photo_format.__file__).resolve().parent == bin_dir.resolve(); "
        "assert pathlib.Path(mod.photo_select.__file__).resolve().parent == bin_dir.resolve()"
    )
    imp = subprocess.run(
        [sys.executable, "-c", import_check],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
    )
    assert imp.returncode == 0, imp.stderr

    cli = subprocess.run(
        [sys.executable, str(bin_dir / "sync-photos.py"), "--help"],
        capture_output=True, text=True, timeout=30, cwd=str(tmp_path),
    )
    assert cli.returncode == 0, cli.stderr


@pytest.mark.parametrize("missing_module", [src for src, _ in PHOTO_SYNC_SOURCES])
def test_missing_photo_sync_module_fails_loudly(repo_root, tmp_path, missing_module):
    proc, temp_home = _stage_and_run_installer(repo_root, tmp_path, missing_module=missing_module)
    assert proc.returncode != 0
    assert missing_module in (proc.stderr + proc.stdout)
    assert not (temp_home / ".local" / "bin" / "sync-photos.py").exists()


def test_installer_ships_the_canonical_reboot_units_not_its_own(repo_root):
    """One source of truth for the reboot schedule (#8, K7).

    install.sh used to write its own 03:00 units while the panel actually runs the
    02:30 units from provision/system/, so a rebuild from main silently changed the
    schedule. It must copy the canonical files and never redefine them.
    """
    body = script_path(repo_root).read_text()
    code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
    assert "provision/system/${unit}" in body
    assert "kitchen-display-reboot.service" in body
    assert "kitchen-display-reboot.timer" in body
    assert "OnCalendar" not in code, "install.sh must not define its own reboot schedule"
    assert "03:00" not in code
    assert "systemctl reboot" not in code, "the reboot action lives in the unit files"


def test_staged_install_deploys_the_0230_reboot_schedule(repo_root, tmp_path):
    """The unit a rebuild deploys is byte-identical to provision/system/ (02:30)."""
    proc, _ = _stage_and_run_installer(repo_root, tmp_path)
    assert proc.returncode == 0, proc.stderr
    installed = tmp_path / "systemd" / "kitchen-display-reboot.timer"
    canonical = repo_root / "provision" / "system" / "kitchen-display-reboot.timer"
    assert installed.is_file(), "installer did not deploy the reboot timer"
    assert installed.read_text() == canonical.read_text()
    assert "02:30" in installed.read_text()


def test_dry_run_names_the_0230_reboot_units(repo_root):
    proc = run_dry(repo_root)
    assert proc.returncode == 0, proc.stderr
    out = (proc.stdout + proc.stderr).lower()
    assert "02:30" in out
    assert "kitchen-display-reboot.timer" in out



def test_staged_installer_ships_the_idle_policy(repo_root, tmp_path):
    """#32: the unit, the script and an autostart that no longer runs swayidle."""
    proc, temp_home = _stage_and_run_installer(repo_root, tmp_path)
    assert proc.returncode == 0, proc.stderr

    script = temp_home / ".local" / "bin" / "swayidle-policy"
    assert script.is_file(), f"missing installed swayidle-policy in {script.parent}"
    assert script.read_text() == (repo_root / "tools" / "swayidle_policy.py").read_text()
    assert script.stat().st_mode & 0o111, "swayidle-policy must be executable"

    unit_dir = temp_home / ".config" / "systemd" / "user"
    unit = unit_dir / "kitchen-idle-policy.service"
    assert unit.is_file(), f"missing installed unit in {unit_dir}"
    assert unit.read_text() == (repo_root / "kiosk" / "kitchen-idle-policy.service").read_text()
    assert (unit_dir / "graphical-session.target.wants" / "kitchen-idle-policy.service").exists()

    autostart = (temp_home / ".config" / "labwc" / "autostart").read_text()
    assert "kitchen-idle-policy.service" in autostart, autostart
    assert "swayidle -w" not in autostart, "the installer must not seed a stale inline swayidle"
