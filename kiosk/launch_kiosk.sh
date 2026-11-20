#!/usr/bin/env bash
# Kitchen display kiosk launcher (Raspberry Pi OS Trixie Wayland/labwc).
set -euo pipefail

DEFAULT_URL="http://192.168.178.84/kitchen-display/home?kiosk"
URL_FILE="${KIOSK_URL_FILE:-${HOME}/.config/kitchen-display/url}"
KIOSK_SCALE="${KIOSK_SCALE:-1.5}"

FILE_URL=""
if [[ -r "$URL_FILE" ]]; then
    FILE_URL="$(tr -d '[:space:]' < "$URL_FILE")"
fi

if [[ -n "$FILE_URL" ]]; then
    URL="$FILE_URL"
elif [[ -n "${KIOSK_URL:-}" ]]; then
    URL="$KIOSK_URL"
else
    URL="$DEFAULT_URL"
fi

exec chromium \
    --kiosk \
    --ozone-platform=wayland \
    --force-device-scale-factor="${KIOSK_SCALE}" \
    --touch-events=enabled \
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --no-first-run \
    --password-store=basic \
    --disable-features=Translate,InterestCohort \
    --overscroll-history-navigation=0 \
    --remote-debugging-port=9222 \
    --remote-allow-origins=http://127.0.0.1 \
    "$URL"
