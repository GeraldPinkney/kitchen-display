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

Ensure the Raspberry Pi 5 joins the LAN over Wi-Fi (`802.11`), connect the Waveshare 10.1" HDMI + USB touch cable, and power on with the official 27W USB-C PSU.

From a workstation on the LAN:

```bash
ssh hermes@kitchen-display.local
git clone <repo-url> ~/kitchen-display
cd ~/kitchen-display
```

## 3. Provision the Kiosk

Preview the provisioning plan first, then execute as root:

```bash
KIOSK_USER=hermes ./provision/install.sh --dry-run
sudo KIOSK_USER=hermes ./provision/install.sh
```

This configures `labwc` autologin for `hermes`, seeds `~/.local/share/kitchen-display/solar.html` (if absent), and enables both `kitchen-display.service` (scaled via `KIOSK_SCALE=1.5` for a 1280x800 logical viewport) and `kitchen-display-http.service` (serving `~/.local/share/kitchen-display/` on `:8788`). If Home Assistant credentials, `KIOSK_SCALE`, or custom URL overrides are needed, edit `~/.config/kitchen-display/env` (kept at mode `0600`).

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
2. **Screen blank and wake (`wlopm` on `HDMI-A-1`)**:
   ```bash
   WAYLAND_DISPLAY=wayland-0 wlopm --off HDMI-A-1
   WAYLAND_DISPLAY=wayland-0 wlopm --on HDMI-A-1
   ```
   Also verify touch input wakes the screen when blanked by `swayidle`.
3. **Nightly reboot timer**:
   ```bash
   systemctl list-timers kitchen-display-reboot.timer
   ```
4. **Dashboard reachability**:
   ```bash
   head -c 200 ~/.config/kitchen-display/url
   ```
   Confirm `http://192.168.178.84/kitchen-display/home?kiosk` loads on the display (including `Home`, `Energy` with East/West Forecast.Solar planes, and `Solar` panel webpage card at `:8788/solar.html`) and recovers automatically after `sudo reboot`.

## Network

WiFi is the design, not a fallback: `eth0` reports `NO-CARRIER` and stays unwired. Do not "fix" it by cabling the panel — the kiosk, the `:8788` HTTP server and the dashboard all resolve over WiFi, and the publish path (`scp` from the host) uses the same address.
