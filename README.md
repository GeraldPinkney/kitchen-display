# kitchen-display

Wall-mounted kitchen touch display for the Pinkney homelab, showing Home Assistant
dashboards and LuxPower optimizer performance.

## Hardware (planned BOM)

| Item | Choice | Notes |
| --- | --- | --- |
| Compute | Raspberry Pi 4 4GB | Chromium kiosk is comfortable; ~3–8 W |
| Display | 10.1" IPS capacitive touch monitor (USB-powered, e.g. Waveshare 10.1") | IPS for kitchen viewing angles; monitor powers the Pi |
| Power | One USB-C/USB-A 12V+ adapter per monitor spec | Add smart-plug energy monitoring for measured consumption |
| Mount | VESA / under-cabinet bracket | Ventilation — do not seal the Pi behind glass |
| Network | Wi-Fi (or Ethernet over USB) | LAN-only, no cloud |

## Use cases

1. Solar & optimizer performance: PV actuals vs forecast, battery SoC, optimizer
   planned actions (from `luxpower-optimizer` HA sensors).
2. Home at-a-glance: energy flow, weather, calendar, bin day.
3. Quick controls: kitchen lights/switches, timer shortcuts.
4. Alert surface: red banner on backup/CrowdSec/optimizer watchdog alerts
   (silent-on-success elsewhere, visible here).

## Requirements

- Kiosk auto-recovery: systemd restarts the browser, nightly 03:00 reboot,
  watchdog failure alerts to HA.
- Dark schedule: screen off ~23:00–06:30 (and idle timeout), wake on touch.
- LAN-only; all data from HA (http://192.168.178.84:80) and Nextcloud-hosted
  optimizer page.
- No cloud dependencies; long-lived HA auth token in an env file.

## Software approach

Chromium in kiosk mode via systemd, with a display toggle between:

- `ha.html` target: HA Lovelace "Kitchen" dashboard (`config/lovelace_kitchen.yaml`)
- `perf.html` target: optimizer performance page (variant of
  `generate_solar_charts.py` output)

Toggle via kiosk URL param and an HA entity (`input_select.kitchen_display`),
switched from the dashboard itself or via HA service call from the browser side.

## Layout

- `kiosk/` — systemd units, launcher script, README for flashing/setup
- `ha/` — Lovelace YAML, automations (night off, wake, reboot)
- `perf-page/` — optimizer chart page source, if it diverges from the
  Nextcloud-hosted `luxpower_dashboard.html`
- `docs/` — BOM, install walkthrough, photos

## Conventions

- Same discipline as `luxpower-optimizer`: backup before edit, test after,
  silent-on-success/alert-on-failure for any cron/automation scripts.
- Credentials live in env files (e.g. `.kiosk.env` for the HA token), never
  committed.
