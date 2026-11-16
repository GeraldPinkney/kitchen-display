# Kitchen display — component status and resume state

Living checkpoint for the K1–K12 hardening work. Update this file whenever a
component closes or a gate passes; do not keep status in chat only.

Last updated: 2026-11-16, after K5 closed (solar publish cron verified live).

## Hardware and endpoints

- Pi: `hermes@192.168.178.56`, display `HDMI-A-1` 1920x1200 @59.95 Hz, 1.5 scale
  (1280x800 logical), touch `HID 0712:000a`, compositor labwc + kanshi, WiFi only.
- Home Assistant: `http://192.168.178.84/kitchen-display/home?kiosk`.
- Solar page served on the panel: `http://192.168.178.56:8788/solar.html`
  (from `~/.local/share/kitchen-display/solar.html`).
- Repo: `/opt/data/kitchen-display` (branch `main`). Env: `/opt/data/.homeassistant.env`,
  `/opt/data/.luxpower.env`. There is **no** `/opt/data/.kitchen_display.env` — the
  publish tooling runs on its defaults.

## Component state

| # | Component | State |
|---|-----------|-------|
| K1 | Kiosk as a supervised service that actually paints | **DONE** (root cause fixed, session persists across restart) |
| K2 | Declarative startup, no sleep-chain race | **CODE DONE**, gate = first unattended reboot |
| K3 | Persistent journald | **BLOCKED** (needs root on the Pi) |
| K4 | Reboot acceptance (kiosk + backlight/swayidle) | **BLOCKED** (needs `sudo reboot`) |
| K5 | Scheduled solar page publish | **DONE** (cron `89853d99b294`, verified 17:24:54) |
| K6 | Nightly reboot timer | **BLOCKED** (needs root to write the unit) |
| K7 | Pi checkout + idempotent `provision/install.sh` | **BLOCKED** (needs root) |
| K8 | Fix dashboard push tooling | **DONE** (`websockets` 17.4 in the kitchen venv) |
| K9 | Device health cron (silent-on-success) | **NOT STARTED** (reuse the probe below) |
| K10 | Network policy | **DONE** (WiFi-only is the design, documented in `install.md`) |
| K11 | Optional kiosk chrome (`?kiosk` header/sidebar) | **OPEN** (no strong preference expressed) |
| K12 | Smart plug | **DROPPED** (owner deferred, do not resurface unprompted) |

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
  → 45 passed, 1 skipped.
- Authenticated panel: page title must be `Kitchen – Home Assistant` (the login page
  is plain `Home Assistant`). Read it with CDP on a throwaway instance.
- Session probe: `/opt/data/cache/scratch/ha_session_check.py` (copied to `/tmp` on
  the Pi) reports `hassTokens found in Local Storage`. It also checks cookies, but
  cookies are not where HA keeps its session — do not trust a cookie-only verdict.
- Kiosk state: `systemctl --user is-active kitchen-display.service` → `active`,
  `is-enabled` → `enabled`, `pgrep -c chromium` → 10.
- Solar publish: `ssh hermes@192.168.178.56 'stat -c "%y %s" ~/.local/share/kitchen-display/solar.html'`
  should advance every 15 minutes.

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

## Blocked on root on the Pi

`hermes` has no sudo: `sudo -n` → "a password is required", polkit has no rule.

    sudo apt install -y wvkbd     # on-screen keyboard; squeekboard is a dead end
    sudo reboot                   # proves K2's autostart path and K4 (swayidle/backlight)

K3 (journald), K6 (nightly reboot timer) and K7 (provisioning) also need root. Either
the owner runs those, or adds a scoped drop-in:

    echo 'hermes ALL=(root) NOPASSWD: /usr/bin/apt-get, /usr/bin/apt, /usr/bin/systemctl' | sudo tee /etc/sudoers.d/020-hermes-kitchen

## Next actions, in order

1. Owner: install `wvkbd`, then reboot. On return, confirm the panel reaches
   `Kitchen – Home Assistant` unattended and that the keyboard appears on touch.
2. Close K2's gate with that reboot; if the kiosk does not come up, the rollback is
   `~/.config/labwc/autostart.bak.*` on the Pi (and `.bak.*` unit copies alongside).
3. K9: host cron, silent-on-success, using `ha_session_check.py` plus a `:8788`
   health check; failure line at exit 0.
4. K11 only if the owner asks.

## Backups and scratch

- Launcher: `/opt/data/cache/scratch/launch_kiosk.sh.bak.*`, same on the Pi.
- Pi autostart + unit: `~/.config/labwc/autostart.bak.*`, `~/.config/systemd/user/kitchen-display.service.bak.*`.
- Diagnostics: `/opt/data/cache/scratch/` (`pngstat.py`, `ha_session_check.py`,
  `netlog3.json`, screenshots).
