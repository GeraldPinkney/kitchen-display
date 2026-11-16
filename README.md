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
- **Browser Kiosk**: `chromium` (native Wayland via `--ozone-platform=wayland --kiosk`) launched by `kiosk/launch_kiosk.sh`.
- **Service Supervision**: `kiosk/kitchen-display.service` runs as a **systemd user unit** (`Restart=always`, `RestartSec=5`) bound to `graphical-session.target` and started from `~/.config/labwc/autostart`.
- **Screen Blanking**: `swayidle` + `wlopm` turns `HDMI-A-1` off after inactivity and wakes on touch input.
- **Nightly Maintenance**: A systemd timer (`kitchen-display-reboot.timer`) reboots the Pi daily at `03:00`.
- **Configuration & Secrets**:
  - `~/.config/kitchen-display/env` (`chmod 600`) stores private environment variables and secrets (never committed to git).
  - `~/.config/kitchen-display/url` overrides the target URL (defaults to `http://192.168.178.84/kitchen-display/home?kiosk`).
- **Home Assistant Dashboard**: Storage-mode Lovelace dashboard at `url_path: kitchen-display` (`ha/kitchen_dashboard.yaml`), using only built-in Home Assistant cards (no HACS dependencies) with `home` and `energy` views.

## Repository Layout

- `ha/kitchen_dashboard.yaml` — Canonical Lovelace dashboard configuration (`Kitchen` with `home` and `energy` views).
- `tools/dashboard_check.py` — Validates dashboard structure, built-in card types, and entity IDs against `tests/fixtures/ha_states.json` or live Home Assistant (`--live`).
- `tools/push_dashboard.py` — Creates/updates the `kitchen` storage dashboard over the Home Assistant WebSocket API (`--apply`).
- `kiosk/launch_kiosk.sh` — Resolves the target URL (`$KIOSK_URL_FILE` -> `$KIOSK_URL` -> default) and `exec`s `chromium` under Wayland.
- `kiosk/kitchen-display.service` — systemd user service unit for the kiosk browser.
- `kiosk/autostart` — `labwc` autostart script starting `kitchen-display.service` and `swayidle`/`wlopm`.
- `provision/install.sh` — Idempotent Raspberry Pi provisioning script (`--dry-run` supported).
- `docs/install.md` — Flash, boot, provision, and verification runbook.
- `tests/` — Pytest test suite and captured Home Assistant entity fixture.
