#!/usr/bin/env bash
# Kitchen display kiosk launcher.
# Reads the target URL from KIOSK_URL_FILE (default: HA dashboard).
# Restarted automatically by systemd; watchdog reboots at 03:00 via cron.
set -euo pipefail

URL_FILE="${KIOSK_URL_FILE:-/opt/kitchen-display/kiosk/current_url.txt}"
HA_FALLBACK="http://192.168.178.84:80/lovelace/kitchen"

URL="$HA_FALLBACK"
if [[ -r "$URL_FILE" && -s "$URL_FILE" ]]; then
    URL="$(tr -d '[:space:]' < "$URL_FILE")"
fi

# Screen off 23:00-06:30 is handled by HA automation on a smart plug / DPMS;
# this script just launches the browser fullscreen.

exec chromium \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --disable-features=Translate,InterestCohort \
    --check-for-update-interval=86400 \
    --kiosk \
    --start-fullscreen \
    --autoplay-policy=no-user-gesture-required \
    --disable-pinch \
    --overscroll-history-navigation=0 \
    "$URL"
