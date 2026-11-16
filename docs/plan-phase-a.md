# Phase A — software build (no hardware)

Status: **in progress** · owner: Gerald · branch: `kitchen/phase-a`

## Scope

Everything that can be built and verified before the Pi 5 is provisioned:
dashboard configuration, the kiosk launcher and unit, and the provisioning
script. Hardware steps (Phase B/C) are out of scope.

## Stack (decided 2026-11-16)

- Raspberry Pi OS **Trixie 64-bit, desktop** — Wayland/labwc, `chromium`
  (binary name), no `xset`/`xdotool`/`unclutter`.
- Kiosk = labwc session (autologin) + systemd **user** unit for auto-restart.
- Screen blanking = `swayidle` + `wlopm` on `HDMI-A-1`; touch resumes.
- Dashboard = HA storage dashboard at `url_path: kitchen-display` (HA requires a hyphen), `?kiosk` suffix.

## Deliverables

| File | Purpose |
| --- | --- |
| `ha/kitchen_dashboard.yaml` | canonical dashboard config, pushed to HA |
| `tools/dashboard_check.py` | validate config: known entities, no HACS cards |
| `tools/push_dashboard.py` | create/refresh the dashboard over the HA websocket |
| `kiosk/launch_kiosk.sh` | URL resolution + Chromium Wayland kiosk launch |
| `kiosk/kitchen-display.service` | systemd user unit, `Restart=always` |
| `kiosk/autostart` | labwc autostart entry that starts the unit |
| `provision/install.sh` | idempotent Pi installer with `--dry-run` |
| `docs/install.md` | runbook: flash → boot → provision → verify |
| `README.md` | updated BOM/stack (currently describes the abandoned Pi 4/X11 plan) |

## Method

SDLC with TDD: specs written first and observed failing
(`/opt/data/venvs/kitchen-tests/bin/python -m pytest tests/ -q`), then
implementation, then refactor. Backups of the replaced files are kept as
`*.bak.20261116`. Revert path is the branch: `git checkout main -- <file>`.

## Acceptance gates

1. `pytest tests/ -q` green.
2. `tools/dashboard_check.py --live` reports zero errors against HA.
3. Dashboard pushed and reachable at `http://192.168.178.84/kitchen-display/home?kiosk`.
4. `install.sh --dry-run` deterministic and non-destructive.
5. No secret material in any tracked file.

## Not in scope

Smart plug / power measurement (blocked on hardware), 03:00 reboot timer
verification (needs the Pi), Solar iframe view (needs a decision on hosting
`luxpower_dashboard.html`).
