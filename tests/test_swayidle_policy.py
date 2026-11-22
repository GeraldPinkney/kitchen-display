"""Specs for tools/swayidle_policy.py — the time-of-day screen-off policy (#32).

The loop is driven with an injected clock and a fake swayidle, so a re-arm that
happens twice a day is proven without waiting for 22:00.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_tool

BOOT_DAY = dt.datetime(2026, 11, 22, 14, 0, 0)


@pytest.fixture(scope="module")
def policy():
    return load_tool("swayidle_policy")


class FakeProc:
    """Stand-in for subprocess.Popen: only what the loop touches."""

    def __init__(self, argv, exit_now=None):
        self.argv = argv
        self.returncode = exit_now
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakeClock:
    def __init__(self, start: dt.datetime):
        self.now = start

    def __call__(self) -> dt.datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now = self.now + dt.timedelta(seconds=seconds)


@pytest.mark.parametrize(
    "stamp, expected",
    [
        (dt.datetime(2026, 11, 22, 5, 59, 59), 30 * 60),
        (dt.datetime(2026, 11, 22, 6, 0, 0), 5 * 3600),
        (dt.datetime(2026, 11, 22, 12, 0, 0), 5 * 3600),
        (dt.datetime(2026, 11, 22, 21, 59, 59), 5 * 3600),
        (dt.datetime(2026, 11, 22, 22, 0, 0), 30 * 60),
        (dt.datetime(2026, 11, 22, 2, 30, 0), 30 * 60),
    ],
)
def test_off_timeout_follows_the_day_window(policy, stamp, expected):
    assert policy.off_timeout(stamp) == expected


@pytest.mark.parametrize(
    "stamp, expected",
    [
        (dt.datetime(2026, 11, 22, 5, 59, 59), dt.datetime(2026, 11, 22, 6, 0, 0)),
        (dt.datetime(2026, 11, 22, 6, 0, 0), dt.datetime(2026, 11, 22, 22, 0, 0)),
        (dt.datetime(2026, 11, 22, 14, 0, 0), dt.datetime(2026, 11, 22, 22, 0, 0)),
        (dt.datetime(2026, 11, 22, 22, 0, 0), dt.datetime(2026, 11, 23, 6, 0, 0)),
        (dt.datetime(2026, 11, 22, 23, 30, 0), dt.datetime(2026, 11, 23, 6, 0, 0)),
    ],
)
def test_next_boundary_is_strictly_after_now(policy, stamp, expected):
    assert policy.next_boundary(stamp) == expected


def test_swayidle_argv_keeps_the_photo_frame_and_swaps_only_the_off_timeout(policy):
    day = policy.swayidle_argv(BOOT_DAY)
    night = policy.swayidle_argv(dt.datetime(2026, 11, 22, 23, 0, 0))

    assert day[:2] == ["swayidle", "-w"]
    assert day[2] == "timeout" and day[3] == str(policy.PHOTO_FRAME_SECONDS) == "300"
    assert day[4] == policy.PHOTO_FRAME_ACTION == "timeout 5 photo-switch show"
    assert day[5] == "timeout" and day[6] == "18000"
    assert day[7] == "wlopm --off '*'"
    assert day[8] == "resume"
    assert day[9] == "photo-switch hide; wlopm --on '*'"

    assert night[6] == "1800"
    # Everything except the screen-off timeout is identical across the window.
    assert day[:6] == night[:6] and day[7:] == night[7:]


def test_print_emits_a_shell_quoted_command_line(policy, capsys):
    stamp = dt.datetime(2026, 11, 22, 23, 0, 0)
    assert policy.main(["--print", "--now", stamp.isoformat()]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == policy.format_argv(policy.swayidle_argv(stamp))
    assert "timeout 1800" in printed
    assert "'wlopm --off '\"'\"'*'\"'\"''" in printed, printed


def test_loop_rearms_across_the_evening_edge_and_replaces_swayidle(policy):
    clock = FakeClock(dt.datetime(2026, 11, 22, 21, 50, 0))
    spawned: list[FakeProc] = []
    logs: list[str] = []

    def spawn(argv):
        proc = FakeProc(argv)
        spawned.append(proc)
        if len(spawned) == 2:
            # Two arms are enough to prove the edge is honoured.
            raise KeyboardInterrupt
        return proc

    with pytest.raises(KeyboardInterrupt):
        policy.run(spawn=spawn, clock=clock, sleep=clock.sleep, log=logs.append)

    assert len(spawned) == 2, spawned
    assert spawned[0].argv[6] == "18000", "daytime arm must carry the 5 h timeout"
    assert spawned[0].terminated, "the day-time swayidle must be stopped at the edge"
    assert spawned[1].argv[6] == "1800", "night arm must carry the 30 min timeout"
    assert clock.now >= dt.datetime(2026, 11, 22, 22, 0, 0)
    assert any("re-arm at 2026-11-22 22:00" in line for line in logs), logs
    assert any("window edge reached" in line for line in logs), logs


def test_loop_returns_for_systemd_when_swayidle_dies(policy):
    clock = FakeClock(BOOT_DAY)
    logs: list[str] = []

    def spawn(argv):
        return FakeProc(argv, exit_now=1)

    rc = policy.run(spawn=spawn, clock=clock, sleep=clock.sleep, log=logs.append)
    assert rc == 1, "a dead swayidle must not be silent -- systemd restarts the policy"
    assert any("swayidle exited" in line for line in logs), logs


def test_loop_reports_a_missing_swayidle_binary(policy):
    clock = FakeClock(BOOT_DAY)
    logs: list[str] = []

    def spawn(argv):
        raise FileNotFoundError("swayidle")

    rc = policy.run(spawn=spawn, clock=clock, sleep=clock.sleep, log=logs.append)
    assert rc == 2
    assert any("cannot start swayidle" in line for line in logs), logs


def test_kill_is_used_when_swayidle_ignores_sigterm(policy):
    clock = FakeClock(dt.datetime(2026, 11, 22, 21, 59, 0))
    procs: list[FakeProc] = []

    class Stubborn(FakeProc):
        def terminate(self):
            self.terminated = True  # i.e. it ignored SIGTERM: still running

        def wait(self, timeout=None):
            if timeout is not None:  # SIGTERM ignored: the bounded wait expires
                import subprocess

                raise subprocess.TimeoutExpired(cmd="swayidle", timeout=timeout)
            return self.returncode  # after kill() the loop waits without bound

    def spawn(argv):
        proc = Stubborn(argv)
        procs.append(proc)
        if len(procs) == 2:
            raise KeyboardInterrupt
        return proc

    with pytest.raises(KeyboardInterrupt):
        policy.run(spawn=spawn, clock=clock, sleep=clock.sleep, log=lambda _m: None)

    assert procs[0].killed, "a swayidle that ignores SIGTERM must be killed at the edge"
