# kitchen-display

Countertop kitchen touch display for the Pinkney homelab: a Home Assistant Lovelace kiosk on a
Raspberry Pi 5, provisioned entirely from this repository.

Everything below is either a hardware fact (what was bought) or a measurement taken on the running
panel. Nothing here is a datasheet projection.

## What it does, as measured

- **Boots unattended.** Three unattended boots on the deployed panel: 2026-11-17 20:19, 2026-11-20
  02:31, 2026-11-22 02:31. After each, `systemctl --user is-active kitchen-display.service` is
  `active`, `NRestarts=0`, and the dashboard is on screen with the Home Assistant session intact.
- **Restarts itself at 02:30.** `kitchen-display-reboot.timer` (`OnCalendar=*-*-* 02:30:00`) runs a
  one-shot service with a one-minute grace; the panel is back at ~02:32. The timer is deliberately
  not persistent — catching up a missed run would reboot the panel with someone standing at it.
- **Publishes the solar chart every 15 minutes.** Cron job `89853d99b294` rewrites `solar.html` on
  the Pi; the page is served locally on `:8788` and embedded in the dashboard's `Solar` view, so the
  chart survives a Home Assistant outage.
- **Shows a photo frame after 5 minutes idle** (`swayidle` → `photo-switch show` via Chromium's
  loopback CDP port), advancing every 30 seconds. Touch wakes the screen and drops straight back to
  the dashboard. Photos are mirrored from Nextcloud to the Pi every 30 minutes by cron.
- **Blanks the output according to the hour.** `kitchen-idle-policy.service` owns the idle timers:
  the panel goes dark 5 hours after the last touch between 06:00 and 22:00, and 30 minutes after it
  overnight. `swayidle` cannot reload its timeouts, so the policy re-arms it at 06:00 and 22:00. The
  re-arm restarts the idle clock, so a panel untouched since the afternoon goes dark at 22:30 rather
  than 22:00 — the accepted trade for not running a Wayland idle-time client (#32).
- **Watches itself every 15 minutes.** Cron job `981ed65dcda9` probes the panel over one SSH call
  and prints nothing when it is healthy, one line when it is not. A check that could not run exits 2
  so it never looks like a panel fault — with one deliberate exception: the panel's own 02:30
  restart window is silence (see `docs/status.md`, card #28).

## Hardware (BOM)

| Component | Model / Spec | Notes |
| --- | --- | --- |
| Compute | Raspberry Pi 5 | Runs Raspberry Pi OS Trixie 64-bit desktop |
| Display | Waveshare 10.1" IPS capacitive touch (1920x1200) | HDMI video + USB touch; lands as `HDMI-A-2`, 1920x1200 @59.95 Hz |
| Power | Official Raspberry Pi 27W USB-C PSU | Dedicated Pi 5 power supply |
| Stand | Waveshare included desktop stand | Countertop placement (no wall mount) |
| Network | LAN-only (Wi-Fi) | `eth0` reports `NO-CARRIER` by design; the panel is not cabled |

## Software stack

- **OS & compositor**: Raspberry Pi OS **Trixie 64-bit**, Wayland with `labwc` desktop autologin. No
  X11 utilities (`xset`, `xdotool`, `unclutter` are all absent).
- **Browser kiosk**: `chromium` under Wayland (`--ozone-platform=wayland --kiosk
  --force-device-scale-factor=1.5 --touch-events=enabled`), launched by `kiosk/launch_kiosk.sh`. The
  1.5 device scale factor turns the 1920x1200 panel into a **1280x800 logical viewport**, which is
  the size the dashboard is laid out for.
- **Service supervision**: `kiosk/kitchen-display.service` (browser) and
  `kiosk/kitchen-display-http.service` (static server for `~/.local/share/kitchen-display` on
  `:8788`) are **systemd user units** (`Restart=always`, `RestartSec=5`), started from
  `~/.config/labwc/autostart` alongside `kanshi`. Nothing depends on a sleep chain.
- **Screen blanking and the photo frame**: `swayidle` + `wlopm` (connector `"*"`, so a replugged
  panel needs no edit) — 300 s to the photo frame, 3600 s to blank, touch wakes.
- **Nightly maintenance**: `provision/system/kitchen-display-reboot.{timer,service}`, 02:30.
- **Configuration & secrets**: `~/.config/kitchen-display/env` (`chmod 600`) holds private variables
  (`KIOSK_URL`, `KIOSK_SCALE`, Nextcloud photo credentials — never committed);
  `~/.config/kitchen-display/url` overrides the target URL (default
  `http://192.168.178.84/kitchen-display/home?kiosk`). Kiosk user on the Pi is `hermes`.
- **Home Assistant**: `http://192.168.178.84` (HTTP, port 80), storage-mode Lovelace dashboard at
  `url_path: kitchen-display`, built-in cards only — `tools/dashboard_check.py` rejects `custom:*`.

## Repository layout

- `ha/kitchen_dashboard.yaml` — canonical dashboard config (`Home`, `Energy`, `Solar`, `Performance`).
- `tools/dashboard_check.py` — validates dashboard structure, card types and entity IDs against
  `tests/fixtures/ha_states.json`, or live HA with `--live`.
- `tools/push_dashboard.py` — creates/updates the `kitchen` storage dashboard over the HA WebSocket
  API (`--apply`).
- `tools/photo_select.py`, `photo_format.py`, `photo_cache.py`, `sync_photos.py` — photo pipeline:
  WebDAV pull, admission rules, 1920-long-side cache, manifest v2.
- `tools/photo_switch.py` — `show`/`hide` the photo frame over CDP.
- `kiosk/launch_kiosk.sh` — resolves URL (`$KIOSK_URL_FILE` → `$KIOSK_URL` → default) and
  `KIOSK_SCALE` (default `1.5`), then `exec`s chromium.
- `kiosk/kitchen-display.service`, `kiosk/kitchen-display-http.service`, `kiosk/autostart`,
  `kiosk/photo.html`.
- `provision/install.sh` — idempotent provisioning (`--dry-run` supported); `provision/system/` holds
  the reboot units and the sudoers drop-in.
- `docs/install.md` — flash, boot, provision and verification runbook.
- `docs/status.md` — the K-gate checkpoint: every gate, its card, and the evidence that closed it.
- `tests/` — offline pytest suite; 168 passed, 2 skipped on 2026-11-22 (the skips are `shellcheck`
  and `visudo`, absent on the test host).

## Provisioning a new panel

Full walkthrough in `docs/install.md`. The short form, on the Pi after cloning this repo:

```bash
KIOSK_USER=hermes ./provision/install.sh --dry-run          # see the plan
sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh
sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh   # must be a no-op
```

The second run is not cosmetic: idempotency is only claimed when the second invocation exits 0, the
02:30 timer is unchanged, the kiosk keeps its MainPID and `git status` is clean.

## Verifying a running panel

```bash
ssh hermes@192.168.178.56 'systemctl --user is-active kitchen-display.service kitchen-display-http.service'
curl -sI http://192.168.178.56:8788/solar.html | head -1          # 200
python3 /opt/data/scripts/kitchen_panel_watch.py                  # silent, exit 0 when healthy
```

`docs/install.md` §5-6 has the rest (blank/wake, reboot timer, dashboard URL, photo frame). The
watchdog is the authority on "is the panel actually working": it checks the unit, the browser
process, the Home Assistant session, the solar page age, the HA socket and a `grim` frame hash.

## Status, gaps and work tracking

`docs/status.md` is the living K-gate checkpoint — gates K1-K12, each mapped to its card, with the
command output that closed it. Known gaps today:

- **K3 journald persistence** — `/var/log/journal` is empty, so post-reboot forensics are lost
  (blocked on an interactive root session; the sudo drop-in deliberately has no `journalctl`).
- **K7 live re-run** — the idempotent installer is merged, the on-panel re-run is pending root.
- **K11 kiosk chrome** — `?kiosk` is inert; the Home Assistant sidebar/header are still visible
  (#26, owner decision).

Work tracking is mandatory: every task is a GitHub issue in `GeraldPinkney/kitchen-display`, added to
Project #2 "Kitchen Display", filed before work starts. Cards close on published evidence, not on
"looks fine". `v1.0` is not tagged yet — it waits on K3 and K7.
