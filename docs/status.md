# Kitchen display — component status and resume state

Living checkpoint for the K1–K12 hardening work. Update this file whenever a
component closes or a gate passes; do not keep status in chat only.

Last updated: 2026-11-20, after the photo frame landed on `main` (PR #22), the 02:30
reboot was observed for the first time (boot 02:31), and the installer stopped
writing its own reboot schedule (PR #23).

## Board of record

Work is tracked as GitHub issues in `GeraldPinkney/kitchen-display`, on Project #2
"Kitchen Display": https://github.com/users/GeraldPinkney/projects/2

Every K-gate below maps to a card. Do not add tasks here without a card (see the
work-tracking rule in `AGENTS.md`).

| Card | Covers |
|------|--------|
| #4 Provision Pi (closed) | kiosk unit, launcher, K1, K2 boot gate, K10 |
| #6 HA Lovelace dashboard (open) | dashboard content, K8 push tooling, K11 kiosk chrome |
| #8 Boot hardening (open, needs root for the rest) | K3 journald, K6 nightly schedule, K7 provisioning, boot-clock skew |
| #11 K9 panel health cron (open) | K9 |
| #5 Physical build (open, owner confirm) | mount/wiring, closes on owner's word |
| #10 Polish, docs, v1.0 | README, docs, tag |
| #17 Photo frame selection/day order (shipped in P1) | photo frame content rules |

Closed: #1 hardware, #2 repo prep, #3 solar page, #7 display toggle, #9 power draw
(not planned — the smart plug was dropped as K12), #12 solar embed, #13, #14 (owner
verified blank/wake after 60 min idle), #16 provisioning split, #20 battery tile.

## Hardware and endpoints

- Pi: `hermes@192.168.178.56` (Pi 5), display **`HDMI-A-2`** (verified with `wlopm`;
  the old notes said `HDMI-A-1`, which is why `wlopm --off HDMI-A-1` never blanked
  anything) 1920x1200 @59.95 Hz, 1.5 scale (1280x800 logical), touch `HID 0712:000a`,
  compositor labwc + kanshi, WiFi only.
- Home Assistant: `http://192.168.178.84/kitchen-display/home?kiosk`.
- Solar page served on the panel: `http://192.168.178.56:8788/solar.html`
  (from `~/.local/share/kitchen-display/solar.html`).
- Photo frame: `http://127.0.0.1:8788/photo.html`, shown by `photo-switch show` after
  5 min idle, hidden on touch.
- Repo: `/opt/data/kitchen-display` (branch `main`), and a read-only checkout on the
  Pi at `~/kitchen-display` (added 2026-11-20 for the K7 rebuild, kept for
  `git pull` + `provision/install.sh` re-runs).
- Env: `/opt/data/.homeassistant.env`, `/opt/data/.luxpower.env`. There is **no**
  `/opt/data/.kitchen_display.env` — the publish tooling runs on its defaults.

## Component state

| # | Component | State | Card |
|---|-----------|-------|------|
| K1 | Kiosk as a supervised service that actually paints | **DONE** (root cause fixed, session persists across restart) | #4 |
| K2 | Declarative startup, no sleep-chain race | **DONE** — unattended reboots passed 2026-11-17 20:19 and 2026-11-20 02:31 | #4 |
| K3 | Persistent journald | **BLOCKED** — `/var/log/journal` exists but empty, `journalctl --user -b` → "No journal files were found"; needs an interactive root session (the sudo drop-in has no `journalctl`) | #8 |
| K4 | Reboot acceptance (kiosk + backlight/swayidle) | **DONE** — see the K4/K6 gate below; blank/wake owner-verified and closed as #14 | #8 |
| K5 | Scheduled solar page publish | **DONE** (cron every 15m; page 200, mtime 2026-11-20 08:52) | #3 |
| K6 | Nightly reboot schedule | **DONE** — timer enabled+active, next elapse 2026-11-21 02:30, fired 2026-11-20 (boot 02:31) | #8 |
| K7 | Pi checkout + idempotent `provision/install.sh` | **CODE DONE** (PR #23), live re-run pending root — see the K7 gate | #8 |
| K8 | Fix dashboard push tooling | **DONE** (`websockets` 17.4 in the kitchen venv) | #6 |
| K9 | Device health cron (silent-on-success) | **IN PROGRESS** — spec written, implementation delegated, not yet deployed | #11 |
| K10 | Network policy | **DONE** (WiFi-only is the design, documented in `install.md`) | #4 |
| K11 | Optional kiosk chrome (`?kiosk` header/sidebar) | **OPEN** — `?kiosk` is inert today, chrome still visible | #6 |
| K12 | Smart plug | **DROPPED** (owner deferred, do not resurface unprompted) | #9 |

## K2 gate: the evidence that closed it

The panel was powered off on 2026-11-17 and came back at **20:19** that evening. Twelve
hours later, with no human touching it:

- `systemctl --user is-active` → `active`, `is-enabled` → `enabled`, `NRestarts=0`.
- chromium MainPID 1481, start time equal to the boot, 10 chromium processes.
- remote `grim` capture (183 KB) shows the painted `Kitchen - Home Assistant`
  dashboard: sidebar, power-history chart, battery gauge, inverter card, forecast
  cards. Not white, not a login page.

Caveat when reading systemd timestamps: `who -b` and `ActiveEnterTimestamp` both said
`2026-11-16 20:11` for the 17th's boot — the clock was ~24h slow at boot and
`systemd-timesyncd` stepped it forward. `/proc/uptime` is the trustworthy figure for
that boot. See the boot-clock item on #8.

## K4/K6 gate: the 02:30 schedule is real, not configured-but-never-fired

On 2026-11-20 the panel rebooted on its own, one minute after the timer's elapse:

    $ ssh hermes@192.168.178.56 'who -b; systemctl show kitchen-display-reboot.timer -p NextElapseUSecRealtime --value'
            system boot  2026-11-20 02:31
    Sat 2026-11-21 02:30:00 GMT

`systemctl is-enabled`/`is-active` → `enabled`/`active`; the unit is
`provision/system/kitchen-display-reboot.{service,timer}` (`OnCalendar=*-*-* 02:30:00`,
`ExecStart=/usr/sbin/shutdown -r +1`, `Persistent=false`).

After that boot the kiosk came back by itself: `is-active` → `active`, MainPID 1463,
`NRestarts=0`, solar page 200, CDP page title `Kitchen Display Photos`.

Post-reboot *forensics* are still lost: journald is not persistent (K3), so
`LastTriggerUSec` is blank after a reboot and `journalctl -b` has nothing. The
argument for the timer firing is the boot timestamp, not a journal entry.

## K7 gate: a rebuild must produce the deployed panel

Status: the installer is fixed and merged; the live re-run is outstanding because it
needs root in one shell (see "Blocked on root").

What was wrong: `install.sh` wrote its own `kitchen-display-reboot` units at **03:00**
with `ExecStart=/usr/bin/systemctl reboot`, while
`provision/system/install-root.sh` installs the **02:30** units with
`shutdown -r +1` and `systemd-analyze verify`. Both were guarded by "if the file does
not exist", so a rebuild from `main` produced a *different* schedule than the panel
runs — and skipped the verification.

Now: `install.sh` copies `provision/system/kitchen-display-reboot.{service,timer}`,
verifies them with `systemd-analyze verify`, then `daemon-reload` + `enable --now`. It
keeps no schedule of its own. `README.md` said 03:00; it now says 02:30.

Evidence for the code half:

    $ /opt/data/venvs/kitchen-tests/bin/python -m pytest -q
    168 passed, 2 skipped in 36.63s

including `test_staged_install_deploys_the_0230_reboot_schedule`, which runs the real
installer in a staged tree and asserts the deployed timer is byte-identical to
`provision/system/kitchen-display-reboot.timer`.

Evidence for the live half (to run once root is available):

    cd ~/kitchen-display && git pull --ff-only
    sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh
    sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh   # second run: no changes
    systemctl is-enabled kitchen-display-reboot.timer; systemctl show kitchen-display-reboot.timer -p NextElapseUSecRealtime --value
    systemctl --user is-active kitchen-display.service; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8788/solar.html

Idempotency means: the second run exits 0, the timer is still 02:30, the kiosk is still
`active` with the same MainPID, and `git status` on the checkout is clean.

## K9 gate: panel health cron (#11)

Design (fixed on the card): silent on success; **one** body line at exit 0 when the
panel is unhealthy; non-zero reserved for the check itself being unable to run.

Checks: kiosk unit `active` **and** `enabled` (a disabled unit loses the panel at
02:30); `NRestarts=0`; an unexplained unit re-start inside the same `boot_id`;
`hassTokens` present in the chromium leveldb (a logged-out panel looks painted but
serves nothing); `solar.html` 200 and newer than 45 min; a live socket to
192.168.178.84:80 — skipped while the photo frame is on screen, because it drops that
socket by design; and a `grim` frame hash that is byte-identical across
`FROZEN_RUNS=3` consecutive runs — skipped while the screen is blanked, because a
black frame is not a frozen one.

Non-goals: the watchdog never restarts the kiosk, and never uses `pkill -f`/`pgrep -f`
over SSH (both mask the crash-loop evidence and the latter kills the session).

## Verified evidence (re-runnable)

- Suite: `cd /opt/data/kitchen-display && /opt/data/venvs/kitchen-tests/bin/python -m pytest -o cache_dir=/opt/data/cache/scratch/pytest-cache -q`
  → 168 passed, 2 skipped (2026-11-20, after PR #23).
- Live dashboard check: `tools/dashboard_check.py --live` → rc=0.
- Solar view embed (#12), measured over CDP at 1280x800: one iframe, src
  `http://192.168.178.56:8788/solar.html`, `errors: []`. The card is a shadow-DOM
  child, so a plain `querySelectorAll('iframe')` finds nothing — the probe walks
  shadow roots.
- The page itself is fluid: `curl -s http://192.168.178.56:8788/solar.html | grep -c 100dvh` ≥ 1.
- Authenticated panel: page title must be `Kitchen – Home Assistant` (the login page is
  plain `Home Assistant`). Read it with CDP on a throwaway instance.
- Session probe: `/opt/data/cache/scratch/ha_session_check.py` (copied to `/tmp` on the
  Pi) reports `hassTokens found in Local Storage`. Cookies are not where HA keeps the
  session — do not trust a cookie-only verdict.
- Kiosk state: `systemctl --user is-active kitchen-display.service` → `active`,
  `is-enabled` → `enabled`, `pgrep -c chromium` → 10.
- Panel paint, non-invasive:
  `ssh hermes@192.168.178.56 'export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0; grim /tmp/panel.png'`
  then look at the PNG (~183 KB with content; a fixed-size white capture is a hang).
  `WAYLAND_DISPLAY` must be set or `wlopm`/`grim` abort with
  "WAYLAND_DISPLAY is not set".
- Display/blank state: `wlopm` → `HDMI-A-2 on`.
- Solar publish: `ssh hermes@192.168.178.56 'stat -c "%y %s" ~/.local/share/kitchen-display/solar.html'`
  should advance every 15 minutes.
- Re-home a panel that has been tapped away from the start URL:
  `systemctl --user restart kitchen-display.service` (no reboot needed).

## Pitfalls already paid for

- Two writers, one unit: `install.sh` and `install-root.sh` both wrote the
  `kitchen-display-reboot` units, each guarded by "if missing". Copy the files from
  `provision/system/`, never redefine them.
- `pkill -f "<pattern>"` over SSH matches the remote shell's own command line and kills
  the session (exit 255). Use `nohup`, explicit `rm -rf` of profile dirs, or `pkill -x`.
- `wlopm --off HDMI-A-1` never blanked anything: the panel is `HDMI-A-2`. The swayidle
  block uses `'*'`.
- `graphical-session.target` on this image is `RefuseManualStart=yes` and never
  activates, even when pulled as a dependency. The "own target `BindsTo`
  graphical-session.target" pattern leaves the kiosk stopped — a test now forbids
  reintroducing a session target file. labwc's autostart runs after the compositor, so
  starting the units by name from it is already correctly ordered.
- Colour-count heuristics on screenshots mislead (dashboard and login page are both
  mostly white). Use `document.title` via CDP, or file size: dashboard ≈ 131 KB, login
  page ≈ 59 KB.
- `squeekboard` starts, loads layouts, maps a 1920x400 `osk` layer surface, and
  composites nothing — identical pixels visible vs hidden. Do not spend more time on
  it; `wvkbd` is the path and is **not installed**.
- A failed publish is a *detected problem*: one body line at exit 0 (delivered), silent
  on success. Non-zero is reserved for the check being unable to run.
- No journal files exist for the current boot. Reboot diagnosis is blind until K3
  lands.

## Blocked on root

`hermes` has a scoped `NOPASSWD` drop-in (`/etc/sudoers.d/020-hermes-kitchen`:
`systemctl`, `systemd-analyze`, `shutdown`, `apt-get`, `apt`) plus a password-protected
`(ALL : ALL) ALL`. Anything else — writing a journald drop-in, running
`provision/install.sh` as root, `apt install wvkbd` — needs a password, i.e. a shell
the owner drives:

    cd ~/kitchen-display && git pull --ff-only
    sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh   # K7 live half
    sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal
    sudo sed -i 's/^#*Storage=.*/Storage=persistent/' /etc/systemd/journald.conf && sudo systemctl restart systemd-journald
    sudo apt install -y wvkbd

Do not widen the drop-in to `bash`/`journalctl`; it exists so the agent can read state
and manage units, not to become root.

## Next actions, in order

1. K9 (#11): deploy the health cron — implementation, shim + allowlist entry, cron job,
   then the forced-failure test on the card.
2. Owner: the root block above, starting with the K7 re-run (it unblocks the "rebuild
   reproduces the panel" claim).
3. K3 (#8): journald persistence, then re-read the reboot evidence.
4. K11 (#6): decide whether the visible sidebar/header matters; close the card either
   way.
5. #5: owner confirms the physical mount, then it closes.
6. #10: README + BOM + tag `v1.0` once the above are closed.

## Backups and scratch

- Launcher: `/opt/data/cache/scratch/launch_kiosk.sh.bak.*`, same on the Pi.
- Pi autostart + unit: `~/.config/labwc/autostart.bak.*`, `~/.config/systemd/user/kitchen-display.service.bak.*`.
- Docs: `/opt/data/cache/scratch/status.md.bak.*`, `AGENTS.md.bak.*`,
  `install.sh.bak.*`.
- Diagnostics: `/opt/data/cache/scratch/` (`pngstat.py`, `ha_session_check.py`,
  `netlog3.json`, screenshots).
