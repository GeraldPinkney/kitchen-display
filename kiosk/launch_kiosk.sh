#!/usr/bin/env bash
# Kitchen display kiosk launcher (Raspberry Pi OS Trixie Wayland/labwc).
set -euo pipefail

DEFAULT_URL="http://192.168.178.84/kitchen/home?kiosk"
URL_FILE="${KIOSK_URL_FILE:-${HOME}/.config/kitchen-display/url}"

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
    --noerrdialogs \
    --disable-infobars \
    --disable-session-crashed-bubble \
    --no-first-run \
    --disable-features=Translate,InterestCohort \
    --overscroll-history-navigation=0 \
    "$URL"
