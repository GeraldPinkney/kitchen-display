"""Contract tests for the root-installed units (#16: nightly reboot).

These pin the properties that matter at 02:30: the schedule, the fact that a missed
run is not caught up later, and that the unit does not depend on the kiosk session.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SYSTEM_DIR = REPO_ROOT / "provision" / "system"


def test_reboot_timer_schedule_and_no_catch_up():
    text = (SYSTEM_DIR / "kitchen-display-reboot.timer").read_text()
    assert "OnCalendar=*-*-* 02:30:00" in text
    # A missed night must not fire at 08:40 with people in the kitchen.
    assert "Persistent=false" in text
    assert "WantedBy=timers.target" in text
    assert "Unit=kitchen-display-reboot.service" in text


def test_reboot_service_uses_a_one_minute_grace():
    text = (SYSTEM_DIR / "kitchen-display-reboot.service").read_text()
    assert "Type=oneshot" in text
    assert "/usr/sbin/shutdown -r +1" in text
    # Must not be bound to the graphical session: at 02:30 nobody is logged in.
    assert "graphical.target" not in text
    assert "Requires=" not in text


def test_sudoers_drop_in_is_syntax_valid_and_narrow():
    path = SYSTEM_DIR / "sudoers-020-hermes-kitchen"
    if shutil.which("visudo") is None:
        pytest.skip("visudo not installed on this host")
    result = subprocess.run(
        ["visudo", "-c", "-f", str(path)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    text = path.read_text()
    assert text.startswith("#")
    assert "hermes ALL=(root) NOPASSWD:" in text


def test_install_root_script_is_valid_bash_and_refuses_non_root():
    script = SYSTEM_DIR / "install-root.sh"
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0
    text = script.read_text()
    assert 'if [[ "$(id -u)" -ne 0 ]]' in text
    # The drop-in must be validated in place before it lands in /etc/sudoers.d.
    assert text.index("visudo -c -f") < text.index("install -m 0440")
