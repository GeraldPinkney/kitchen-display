# AGENTS.md — kitchen-display

Repo contract for external coding agents (`agy`, `codex`, `claude-code`).

## Worktree & Git Discipline
- Work inside a dedicated `git worktree` (never commit, push, or run destructive git commands).
- Leave all edits uncommitted in the worktree; Hermes reviews, tests, and commits with a `Delegated-by: agy/<conversation_id>` trailer.
- Never modify `tests/fixtures/*` unless a brief explicitly designates a fixture capture task.
- Never delete, skip, or comment out existing test assertions.

## Test Gate
Run the full offline pytest suite via the dedicated test virtualenv:

```bash
/opt/data/venvs/kitchen-tests/bin/python -m pytest -v
```

Every test must pass (`0 failed, 0 errors`).

## Architecture & Conventions
- Target hardware: Raspberry Pi 5 + Waveshare 10.1" 1920x1200 capacitive touch panel on Debian 13 (Trixie), Wayland (`labwc`) + Chromium kiosk (`KIOSK_SCALE=1.5` -> 1280x800 logical viewport).
- Home Assistant runs over HTTP on `http://192.168.178.84` (`url_path: kitchen-display`).
- Built-in Lovelace cards only (`custom:*` cards are rejected by `tools/dashboard_check.py`).
- Forecast.Solar in HA has three planes: legacy single-plane (`sensor.energy_production_today`) plus real East (`_2`) and West (`_3`) planes. Surface East (`_2`) and West (`_3`) explicitly.
- Local static solar chart page is served on the Pi at `http://192.168.178.56:8788/solar.html` by user unit `kitchen-display-http.service` (`python3 -m http.server 8788 --bind 0.0.0.0 --directory %h/.local/share/kitchen-display`).
