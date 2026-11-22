#!/usr/bin/env python3
"""Arm swayidle with a time-of-day screen-off timeout, and re-arm at the window edge.

`swayidle` reads its timeouts once at start and has no reload, so the panel cannot
follow the house from a single static invocation. This script owns the idle timers
instead: it runs swayidle with the timeout for the current window, waits for the
next edge (06:00 or 22:00), and replaces the process — which restarts the idle
clock.

Re-arming at 22:00 therefore starts a fresh 30 minutes rather than measuring what
was already idle, so a panel untouched since the afternoon goes dark at 22:30
instead of 22:00. That overshoot is accepted (#32): sway exposes no idle-time
query on this panel, and the alternative is a Wayland idle-notify client.

The 300 s photo-frame timeout is unconditional: photos may cycle overnight if the
board is woken, and that is allowed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import shlex
import subprocess
import sys
import time
from typing import Callable, Protocol, Sequence

DAY_START = (6, 0, 0)
DAY_END = (22, 0, 0)
DAY_OFF_SECONDS = 5 * 3600
NIGHT_OFF_SECONDS = 30 * 60
PHOTO_FRAME_SECONDS = 300
PHOTO_FRAME_ACTION = "timeout 5 photo-switch show"
SCREEN_OFF_ACTION = "wlopm --off '*'"
RESUME_ACTION = "photo-switch hide; wlopm --on '*'"
# How often the loop re-checks the clock while waiting for an edge. The child is
# supervised on the same tick, so a swayidle crash is noticed within a minute
# instead of the next edge.
POLL_SECONDS = 60


class Child(Protocol):
    """The slice of subprocess.Popen this loop uses (a fake satisfies it in tests)."""

    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _clock_tuple(now: dt.datetime) -> tuple[int, int, int]:
    """(hour, minute, second) — tz-agnostic, so an aware `--now` cannot raise."""
    return (now.hour, now.minute, now.second)


def in_day_window(now: dt.datetime | None = None) -> bool:
    """True inside 06:00-22:00 local (start inclusive, end exclusive)."""
    now = now or dt.datetime.now()
    return DAY_START <= _clock_tuple(now) < DAY_END


def off_timeout(now: dt.datetime | None = None) -> int:
    """Seconds of idle before the panel is blanked, for the window `now` falls in."""
    return DAY_OFF_SECONDS if in_day_window(now) else NIGHT_OFF_SECONDS


def next_boundary(now: dt.datetime | None = None) -> dt.datetime:
    """The next 06:00 or 22:00 strictly after `now`."""
    now = now or dt.datetime.now()
    for edge in (DAY_START, DAY_END):
        candidate = now.replace(hour=edge[0], minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    return (now + dt.timedelta(days=1)).replace(
        hour=DAY_START[0], minute=0, second=0, microsecond=0
    )


def swayidle_argv(now: dt.datetime | None = None) -> list[str]:
    """The exact swayidle command line for the window `now` falls in."""
    return [
        "swayidle",
        "-w",
        "timeout",
        str(PHOTO_FRAME_SECONDS),
        PHOTO_FRAME_ACTION,
        "timeout",
        str(off_timeout(now)),
        SCREEN_OFF_ACTION,
        "resume",
        RESUME_ACTION,
    ]


def format_argv(argv: Sequence[str]) -> str:
    return shlex.join(argv)


def _log(message: str) -> None:
    print(f"swayidle-policy: {message}", file=sys.stderr, flush=True)


def run(
    spawn: Callable[[list[str]], Child] = subprocess.Popen,
    clock: Callable[[], dt.datetime] = dt.datetime.now,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = _log,
) -> int:
    """Arm, supervise until the next edge, replace, repeat.

    Returns only on failure, so systemd's Restart=always brings the policy back;
    a clean exit would mean the panel can stay lit indefinitely.
    """
    while True:
        armed_at = clock()
        argv = swayidle_argv(armed_at)
        deadline = next_boundary(armed_at)
        log(
            f"arming: screen off after {off_timeout(armed_at)}s; "
            f"re-arm at {deadline:%Y-%m-%d %H:%M}"
        )
        try:
            proc = spawn(argv)
        except OSError as exc:
            log(f"cannot start swayidle: {exc}")
            return 2

        try:
            while True:
                remaining = (deadline - clock()).total_seconds()
                if remaining <= 0:
                    log("window edge reached; replacing swayidle to pick up the new timeout")
                    break
                sleep(min(remaining, POLL_SECONDS))
                if proc.poll() is not None:
                    log(f"swayidle exited ({proc.poll()}); asking systemd to restart the policy")
                    return 1
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arm swayidle with a time-of-day screen-off timeout."
    )
    parser.add_argument(
        "--print",
        dest="print_command",
        action="store_true",
        help="print the swayidle command line for --now (or the wall clock) and exit",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="arm swayidle and re-arm at each window edge (what the systemd unit runs)",
    )
    parser.add_argument(
        "--now",
        metavar="ISO",
        help="evaluate this local ISO time instead of the wall clock (inspection only)",
    )
    args = parser.parse_args(argv)
    now = dt.datetime.fromisoformat(args.now) if args.now else dt.datetime.now()

    if args.print_command:
        print(format_argv(swayidle_argv(now)))
        return 0

    try:
        return run()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
