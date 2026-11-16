# Kitchen Display — Installation & Verification Runbook

## 1. Flash Raspberry Pi OS (Imager)

Use Raspberry Pi Imager with the following OS customisation settings:

- **Device**: Raspberry Pi 5
- **OS**: Raspberry Pi OS (64-bit) — Debian **Trixie** desktop (Wayland / `labwc`)
- **Hostname**: `kitchen-display`
- **Username**: `kiosk`
- **SSH**: Enabled, public-key authentication only (paste the owner's SSH public key)
- **Locale / Timezone**: `Europe/London`

## 2. First Boot & Clone

Connect the Raspberry Pi 5 to the LAN over Ethernet, connect the Waveshare 10.1" HDMI + USB touch cable, and power on with the official 27W USB-C PSU.

From a workstation on the LAN:

```bash
ssh kiosk@kitchen-display.local
git clone <repo-url> ~/kitchen-display
cd ~/kitchen-display
```

## 3. Provision the Kiosk

Preview the provisioning plan first, then execute as root:

```bash
./provision/install.sh --dry-run
sudo KIOSK_USER=kiosk ./provision/install.sh
```

If Home Assistant credentials or custom URL overrides are needed, edit `~/.config/kitchen-display/env` (kept at mode `0600`).

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
   systemctl --user status kitchen-display.service
   ```
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
   Confirm `http://192.168.178.84/kitchen/home?kiosk` loads on the display and recovers automatically after `sudo reboot`.
