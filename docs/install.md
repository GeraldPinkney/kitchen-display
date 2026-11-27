# Kitchen Display — Installation & Verification Runbook

## 1. Flash Raspberry Pi OS (Imager)

Use Raspberry Pi Imager with the following OS customisation settings:

- **Device**: Raspberry Pi 5
- **OS**: Raspberry Pi OS (64-bit) — Debian **Trixie** desktop (Wayland / `labwc`)
- **Hostname**: `kitchen-display`
- **Username**: `hermes`
- **SSH**: Enabled, public-key authentication only (paste the owner's SSH public key)
- **Locale / Timezone**: `Europe/London`

## 2. First Boot & Clone

Ensure the Raspberry Pi 5 joins the LAN over Wi-Fi (`802.11`), connect the Waveshare 10.1" HDMI + USB touch cable, and power on with the official 27W USB-C PSU. Ethernet stays down on purpose: `eth0` reports `NO-CARRIER` and the panel is never cabled (see **Network** at the end).

From a workstation on the LAN:

```bash
ssh hermes@kitchen-display.local
git clone <repo-url> ~/kitchen-display
cd ~/kitchen-display
```

## 3. Provision the Kiosk

Preview the provisioning plan first, then execute as root (`/etc/systemd/system` and the kiosk user's crontab are touched, so this cannot run unprivileged):

```bash
KIOSK_USER=hermes ./provision/install.sh --dry-run
sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh
```

This installs the packages, configures `labwc` autologin for `hermes`, seeds `~/.local/share/kitchen-display/solar.html` (if absent), installs and enables three user units — `kitchen-display.service` (scaled via `KIOSK_SCALE=1.5` for a 1280x800 logical viewport), `kitchen-display-http.service` (serving `~/.local/share/kitchen-display/` on `:8788`) and `kitchen-idle-policy.service` (which runs `swayidle` with the time-of-day screen-off timeout) — writes the 02:30 reboot units into `/etc/systemd/system` and enables the timer, then installs `photo.html`, `photo-switch`, `swayidle-policy` and the `photoswitch` venv. It deliberately does **not** install `sync-photos.py` or a photo cron: since the P3 flip the deck is written by `photo_app` on dockerpi. Re-provisioning a host that still needs the old pipeline takes `INSTALL_LEGACY_PHOTO_SYNC=1`, and the default run removes a leftover `sync-photos.py` cron line if one exists. If Home Assistant credentials, `KIOSK_SCALE`, or custom URL overrides are needed, edit `~/.config/kitchen-display/env` (kept at mode `0600`).

Acceptance for this step is a **second run that changes nothing**:

```bash
sudo KIOSK_USER=hermes KIOSK_HOME=/home/hermes bash provision/install.sh   # exits 0, no unit restarts
systemctl --user show -p MainPID --value kitchen-display.service           # same PID before/after
systemctl list-timers kitchen-display-reboot.timer                          # same NextElapse
git status --porcelain                                                     # empty
```

## 4. Push the Home Assistant Dashboard

From a machine with the repo and a Home Assistant admin token in the environment:

```bash
export HA_URL="http://192.168.178.84"
export HA_TOKEN="<long-lived-access-token>"

python3 tools/dashboard_check.py --live
python3 tools/push_dashboard.py --apply
```

## 5. Verification Commands

Run on `kitchen-display` over SSH:

1. **User unit status**:
   ```bash
   systemctl --user status kitchen-display.service kitchen-display-http.service
   ```
   Confirm `curl -I http://127.0.0.1:8788/solar.html` returns `200 OK`.
2. **Screen blank and wake (`wlopm` on the active output)**: `wlopm` takes the
   connector name, which changes with the port (`HDMI-A-2` on the current panel,
   `HDMI-A-1` previously) — `"*"` matches whatever is connected:
   ```bash
   WAYLAND_DISPLAY=wayland-0 wlopm --off "*"
   WAYLAND_DISPLAY=wayland-0 wlopm --on "*"
   ```
   Also verify touch input wakes the screen when blanked by `swayidle`, and that
   `systemctl --user status kitchen-idle-policy` is active with exactly one
   `swayidle` process (`pgrep -af swayidle`): its screen-off timeout must read
   `18000` between 06:00 and 22:00 and `1800` outside that window. `swayidle-policy
   --print` shows the command line it would arm right now.
3. **Nightly reboot timer**:
   ```bash
   systemctl list-timers kitchen-display-reboot.timer
   ```
4. **Dashboard reachability**:
   ```bash
   head -c 200 ~/.config/kitchen-display/url
   ```
   Confirm `http://192.168.178.84/kitchen-display/home?kiosk` loads on the display (including `Home`, `Energy` with East/West Forecast.Solar planes, and `Solar` panel webpage card at `:8788/solar.html`) and recovers automatically after `sudo reboot`.
5. **Unattended boot**: reboot and leave it alone. After ~2 minutes the kiosk must be back with no
   login and no keystroke (`systemctl --user is-active kitchen-display.service`,
   `NRestarts=0`). This is the gate that matters — the panel is only "installed" if it comes back on
   its own.
6. **Panel watchdog** (run from the machine that holds `kitchen_panel_watch.py`):
   ```bash
   python3 kitchen_panel_watch.py; echo "exit=$?"
   ```
   Silent with `exit=0` is healthy. One line on stdout means a panel fault; `exit=2` plus stderr
   means the check itself could not run (unreachable, no `grim`, unreadable state). The 02:30
   restart window is the one excused failure: silence there, and a panel that has not returned by
   the 02:45 run still exits 2.

## 6. Verify the Photo Frame

Run on `kitchen-display` over SSH. The deck is pushed by `photo_app` on dockerpi, so the
panel needs no Nextcloud credentials; trigger a publish from dockerpi if the frame is empty
(`~/photo-app/deploy/run-photo-app.sh`), then check the panel:

1. **User services active**:
   ```bash
   systemctl --user is-active kitchen-display.service kitchen-display-http.service
   ```
   Both must print `active`.
2. **CDP bound to loopback only**:
   ```bash
   ss -ltnp | grep 9222
   ```
   Confirm Chromium is listening on `127.0.0.1:9222` (never `0.0.0.0`).
3. **Trigger and capture the photo frame**:
   ```bash
   photo-switch show
   WAYLAND_DISPLAY=wayland-0 grim /tmp/photo-frame.png
   photo-switch hide
   ```
   Confirm `/tmp/photo-frame.png` shows the full-bleed photo slideshow (`:8788/photo.html`) and `photo-switch hide` returns to the Home Assistant dashboard.

## 7. Not yet automated (needs an interactive root session)

Known gaps, not runbook steps — the `sudo` drop-in in `provision/system/sudoers-020-hermes-kitchen`
deliberately allows only `systemctl`, `systemd-analyze`, `shutdown`, `apt-get` and `apt`, so these
need a human at a root prompt:

```bash
# K3: persistent journald, so post-reboot forensics survive a restart
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo sed -i 's/^#*Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

The on-panel re-run of `provision/install.sh` (§3) is pending the same kind of session. Do not widen
the sudoers drop-in to get there.

## Network

WiFi is the design, not a fallback: `eth0` reports `NO-CARRIER` and stays unwired. Do not "fix" it by cabling the panel — the kiosk, the `:8788` HTTP server and the dashboard all resolve over WiFi, and the publish path (`scp` from the host) uses the same address.
