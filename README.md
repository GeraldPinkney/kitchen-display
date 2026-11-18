# kitchen-display

Countertop kitchen touch display for the Pinkney homelab, running a Home Assistant Lovelace kiosk on a Raspberry Pi 5.

## Hardware (BOM)

| Component | Model / Spec | Notes |
| --- | --- | --- |
| Compute | Raspberry Pi 5 | Runs Raspberry Pi OS Trixie 64-bit desktop |
| Display | Waveshare 10.1" IPS capacitive touch (1920x1200) | HDMI video + USB touch |
| Power | Official Raspberry Pi 27W USB-C PSU | Dedicated Pi 5 power supply |
| Stand | Waveshare included desktop stand | Countertop placement (no wall mount) |
| Network | LAN-only (Ethernet / local Wi-Fi) | Connects directly to Home Assistant at `http://192.168.178.84` |

## Software Stack & Architecture

- **OS & Compositor**: Raspberry Pi OS **Trixie 64-bit** with **Wayland / `labwc`** desktop autologin. No X11 utilities (`xset`, `xdotool`, `unclutter`).
- **Browser Kiosk**: `chromium` (native Wayland via `--ozone-platform=wayland --kiosk --force-device-scale-factor=1.5 --touch-events=enabled`) launched by `kiosk/launch_kiosk.sh`, giving a 1280x800 logical viewport on the 1920x1200 panel (`KIOSK_SCALE=1.5` default).
- **Service Supervision**: `kiosk/kitchen-display.service` (browser kiosk) and `kiosk/kitchen-display-http.service` (local static HTTP server serving `~/.local/share/kitchen-display/solar.html` on `:8788`) run as **systemd user units** (`Restart=always`, `RestartSec=5`) started from `~/.config/labwc/autostart` alongside `kanshi`.
- **Screen Blanking**: `swayidle` + `wlopm` turns `HDMI-A-1` off after inactivity and wakes on touch input.
- **Photo Frame Screensaver**: After 5 min idle, `swayidle` calls `photo-switch show` via Chromium's loopback CDP port (`127.0.0.1:9222`) to load `photo.html` on `:8788` displaying resized photos mirrored locally to `~/.local/share/kitchen-display/photos/` (synced every 30 min via cron); touch wakes and runs `photo-switch hide` back to Home Assistant, and 60 min idle blanks `HDMI-A-1`.
- **Nightly Maintenance**: A systemd timer (`kitchen-display-reboot.timer`) reboots the Pi daily at `03:00`.
- **Configuration & Secrets**:
  - `~/.config/kitchen-display/env` (`chmod 600`) stores private environment variables (`KIOSK_URL`, `KIOSK_SCALE`, credentials — never committed to git).
  - `~/.config/kitchen-display/url` overrides the target URL (defaults to `http://192.168.178.84/kitchen-display/home?kiosk`).
  - Target kiosk user on the Pi is `hermes` (`KIOSK_USER=hermes` for `provision/install.sh`).
- **Home Assistant Dashboard**: Storage-mode Lovelace dashboard at `url_path: kitchen-display` (`ha/kitchen_dashboard.yaml`), using only built-in Home Assistant cards (no HACS dependencies) with `Home`, `Energy` (calibrated East `_2` and West `_3` Forecast.Solar planes), and `Solar` (panel `webpage` card pointing at `http://192.168.178.56:8788/solar.html`) views.

## Repository Layout

- `ha/kitchen_dashboard.yaml` — Canonical Lovelace dashboard configuration (`Kitchen` with `Home`, `Energy`, and `Solar` views).
- `tools/dashboard_check.py` — Validates dashboard structure, built-in card types, and entity IDs against `tests/fixtures/ha_states.json` or live Home Assistant (`--live`).
- `tools/push_dashboard.py` — Creates/updates the `kitchen` storage dashboard over the Home Assistant WebSocket API (`--apply`).
- `kiosk/launch_kiosk.sh` — Resolves the target URL (`$KIOSK_URL_FILE` -> `$KIOSK_URL` -> default) and `KIOSK_SCALE` (default `1.5`), then `exec`s `chromium` under Wayland.
- `kiosk/kitchen-display.service` — systemd user service unit for the kiosk browser.
- `kiosk/kitchen-display-http.service` — systemd user service unit serving `~/.local/share/kitchen-display` on `:8788`.
- `kiosk/autostart` — `labwc` autostart script starting `kanshi`, `kitchen-display-http.service`, `kitchen-display.service`, and `swayidle`/`wlopm`.
- `provision/install.sh` — Idempotent Raspberry Pi provisioning script (`--dry-run` supported).
- `docs/install.md` — Flash, boot, provision, and verification runbook.
- `tests/` — Pytest test suite and captured Home Assistant entity fixture.
