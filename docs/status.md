# Kitchen display — component status and resume state

Living checkpoint for the K1–K12 hardening work. Update this file whenever a
component closes or a gate passes; do not keep status in chat only.

Last updated: 2026-11-18, after the panel's first unattended reboot closed K2 and the
board was brought in sync with this file.

## Board of record

Work is tracked as GitHub issues in `GeraldPinkney/kitchen-display`, on Project #2
"Kitchen Display": https://github.com/users/GeraldPinkney/projects/2

Every K-gate below maps to a card. Do not add tasks here without a card (see the
work-tracking rule in `AGENTS.md`).

| Card | Covers |
|------|--------|
| #4 Provision Pi (closed) | kiosk unit, launcher, K1, K2 boot gate |
| #6 HA Lovelace dashboard (in progress) | dashboard content, K11 kiosk chrome |
| #8 Boot hardening (todo, needs root) | K3 journald, K4 reboot acceptance, K6 nightly schedule, K7 provisioning, boot-clock skew |
| #11 K9 panel health cron (todo) | K9 |
| #12 Solar view renders "Configuration error" (todo) | dashboard embed of the solar page — invalid `type: webpage` card, **owner decision needed (options A-D on the card)** |
| #5 Physical build (todo, owner confirm) | mount/wiring, closes on owner's word |
| #10 Polish, docs, v1.0 | README, docs, tag |

Closed: #1 hardware, #2 repo prep, #3 solar page, #7 display toggle, #9 power draw
(not planned — the smart plug was dropped as K12).

## Hardware and endpoints

- Pi: `hermes@192.168.178.56` (Pi 5), display `HDMI-A-1` 1920x1200 @59.95 Hz, 1.5 scale
  (1280x800 logical), touch `HID 0712:000a`, compositor labwc + kanshi, WiFi only.
- Home Assistant: `http://192.168.178.84/kitchen-display/home?kiosk`.
- Solar page served on the panel: `http://192.168.178.56:8788/solar.html`
  (from `~/.local/share/kitchen-display/solar.html`).
- Repo: `/opt/data/kitchen-display` (branch `main`). Env: `/opt/data/.homeassistant.env`,
  `/opt/data/.luxpower.env`. There is **no** `/opt/data/.kitchen_display.env` — the
  publish tooling runs on its defaults.

## Component state

| # | Component | State | Card |
|---|-----------|-------|------|
| K1 | Kiosk as a supervised service that actually paints | **DONE** (root cause fixed, session persists across restart) | #4 |
| K2 | Declarative startup, no sleep-chain race | **DONE** — first unattended reboot passed 2026-11-17 20:19 | #4 |
| K3 | Persistent journald | **BLOCKED** (needs root on the Pi) | #8 |
| K4 | Reboot acceptance (kiosk + backlight/swayidle) | **BLOCKED** (needs `sudo reboot` + timer) | #8 |
| K5 | Scheduled solar page publish | **DONE** (cron every 15m, verified 2026-11-18 08:20:35) | #3 |
| K6 | Nightly screen/reboot schedule timer | **BLOCKED** (needs root to write the unit) | #8 |
| K7 | Pi checkout + idempotent `provision/install.sh` | **BLOCKED** (needs root) | #8 |
| K8 | Fix dashboard push tooling | **DONE** (`websockets` 17.4 in the kitchen venv) | #6 |
| K9 | Device health cron (silent-on-success) | **NOT STARTED** (design agreed, spec on the card) | #11 |
| K10 | Network policy | **DONE** (WiFi-only is the design, documented in `install.md`) | #4 |
| K11 | Optional kiosk chrome (`?kiosk` header/sidebar) | **OPEN** — `?kiosk` is inert today, chrome still visible | #6 |
| K12 | Smart plug | **DROPPED** (owner deferred, do not resurface unprompted) | #9 |

## K2 gate: the evidence that closed it

The panel was powered off on 2026-11-17 and came back at **20:19** that evening. Twelve
hours later, with no human touching it:

- `systemctl --user is-active` → `active`, `is-enabled` → `enabled`, `NRestarts=0`.
- chromium MainPID 1481, start time equal to the boot, 10 chromium processes.
- remote `grim -o HDMI-A-1` capture (183 KB) shows the painted
  `Kitchen - Home Assistant` dashboard: sidebar, power-history chart, battery gauge 21%,
  inverter card, East/West forecast cards. Not white, not a login page.

Caveat when reading systemd timestamps: `who -b` and `ActiveEnterTimestamp` both say
`2026-11-16 20:11` — the clock was ~24h slow at boot and `systemd-timesyncd` stepped it
forward. `/proc/uptime` (43403 s at 08:22 on the 18th) is the trustworthy figure. See
the boot-clock item on #8.

## What was wrong in K1, and why the fix holds

The panel was plain white. Chain of evidence:

1. No login keyring existed (`~/.local/share/keyrings/` empty) and autologin never
   unlocked one, so `gcr-prompter` sat on screen with a modal unlock dialog.
2. Chromium blocked inside `COOKIE_PERSISTENT_STORE_KEY_LOAD_STARTED` waiting on
   libsecret. Netlog showed the TCP connect to HA succeeding and the socket closing
   ~0.15 s later, with `URL_REQUEST_START_JOB` still pending at cancel — the
   navigation request was never sent.
3. Fix: `--password-store=basic` in `kiosk/launch_kiosk.sh` (commit `0bf27e8`), plus
   regression test `test_launcher_uses_basic_password_store`.

Second, independent fault: the kiosk profile had never held an authenticated HA
session. Owner logged in once on the touchscreen with a physical keyboard; the
session now lives in `~/.config/chromium/Default/Local Storage/leveldb/`.

## Verified evidence (re-runnable)

- Suite: `cd /opt/data/kitchen-display && /opt/data/venvs/kitchen-tests/bin/python -m pytest -o cache_dir=/opt/data/cache/scratch/pytest-cache -q`
  → 45 passed, 1 skipped (2026-11-18).
- Authenticated panel: page title must be `Kitchen – Home Assistant` (the login page
  is plain `Home Assistant`). Read it with CDP on a throwaway instance.
- Session probe: `/opt/data/cache/scratch/ha_session_check.py` (copied to `/tmp` on
  the Pi) reports `hassTokens found in Local Storage`. It also checks cookies, but
  cookies are not where HA keeps its session — do not trust a cookie-only verdict.
- Kiosk state: `systemctl --user is-active kitchen-display.service` → `active`,
  `is-enabled` → `enabled`, `pgrep -c chromium` → 10.
- Panel paint, non-invasive:
  `ssh hermes@192.168.178.56 'export XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0; grim -o HDMI-A-1 /tmp/panel.png'`
  then look at the PNG. ~183 KB with content; a fixed-size white capture is a hang.
- Solar publish: `ssh hermes@192.168.178.56 'stat -c "%y %s" ~/.local/share/kitchen-display/solar.html'`
  should advance every 15 minutes (last seen 2026-11-18 08:20:35, 14,544 bytes).
- Re-home a panel that has been tapped away from the start URL:
  `systemctl --user restart kitchen-display.service` (no reboot needed).

## Pitfalls already paid for

- `pkill -f "<pattern>"` over SSH matches the remote shell's own command line and
  kills the session (exit 255). Use `nohup`, explicit `rm -rf` of profile dirs, or
  `pkill -x`.
- `graphical-session.target` on this image is `RefuseManualStart=yes` and never
  activates, even when pulled as a dependency. The "own target `BindsTo`
  graphical-session.target" pattern leaves the kiosk stopped — a test now forbids
  reintroducing a session target file. labwc's autostart runs after the compositor,
  so starting the units by name from it is already correctly ordered.
- Colour-count heuristics on screenshots mislead (dashboard and login page are both
  mostly white). Use `document.title` via CDP, or file size: dashboard ≈ 131 KB,
  login page ≈ 59 KB.
- `squeekboard` starts, loads layouts, maps a 1920x400 `osk` layer surface, and
  composites nothing — identical pixels visible vs hidden, with or without the kiosk
  running, and `GDK_GL=disable` changes nothing. Do not spend more time on it.
- A failed publish is a *detected problem*: one body line at exit 0 (delivered),
  silent on success. Non-zero is reserved for the check being unable to run.
- No journal files exist for the current boot (`journalctl --user -b` → "No journal
  files were found"). Reboot diagnosis is blind until #8 lands journald persistence.
- The clock is ~24h slow at boot until NTP steps it (see the K2 gate note). Anything
  timestamped in the first seconds of a boot is wrong by up to a day.

## Blocked on root on the Pi

`hermes` has no sudo: `sudo -n` → "a password is required", polkit has no rule.
All of this is one card now: #8.

    sudo apt install -y wvkbd     # on-screen keyboard; squeekboard is a dead end
    sudo reboot                   # K4 acceptance, alongside the nightly-schedule timer

K3 (journald), K6 (nightly timer), K7 (provisioning) and the boot-clock fix need root
too. Either the owner runs those, or adds a scoped drop-in:

    echo 'hermes ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /usr/bin/systemctl' | sudo tee /etc/sudoers.d/020-hermes-kitchen

## Next actions, in order

1. **#12 decision (owner):** which of A-D — iframe one-liner, iframe + fluid page (recommended),
   native HA rebuild, or keep the full-screen toggle only. Nothing else is blocked on it.
2. K9 (#11): build the health cron — unblocked, no root needed.
3. Owner: install `wvkbd` and grant the sudo drop-in (or run the #8 checklist by hand),
   starting with journald and the nightly timer.
4. K11 (#6): decide whether the visible sidebar/header matters; close the card either way.
5. #5: owner confirms the physical mount, then it closes.
6. #10: README + BOM + tag `v1.0` once the above are closed.

## Backups and scratch

- Launcher: `/opt/data/cache/scratch/launch_kiosk.sh.bak.*`, same on the Pi.
- Pi autostart + unit: `~/.config/labwc/autostart.bak.*`, `~/.config/systemd/user/kitchen-display.service.bak.*`.
- Docs: `/opt/data/cache/scratch/status.md.bak.*`, `AGENTS.md.bak.*`.
- Diagnostics: `/opt/data/cache/scratch/` (`pngstat.py`, `ha_session_check.py`,
  `netlog3.json`, screenshots, `panel-now.png` from the 2026-11-18 check).
