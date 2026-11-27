#!/usr/bin/env bash
# Idempotent provisioning script for the kitchen-display Raspberry Pi 5 kiosk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KIOSK_USER="${KIOSK_USER:-kiosk}"
KIOSK_HOME="${KIOSK_HOME:-/home/${KIOSK_USER}}"
CONFIG_DIR="${KIOSK_HOME}/.config/kitchen-display"
DATA_DIR="${KIOSK_HOME}/.local/share/kitchen-display"
SOLAR_PLACEHOLDER="${DATA_DIR}/solar.html"
BIN_DIR="${KIOSK_HOME}/.local/bin"
SYNC_PHOTOS_DEST="${BIN_DIR}/sync-photos.py"
PHOTO_SWITCH_DEST="${BIN_DIR}/photo-switch"
SWAYIDLE_POLICY_DEST="${BIN_DIR}/swayidle-policy"
PHOTOS_DIR="${DATA_DIR}/photos"
PHOTO_HTML_DEST="${DATA_DIR}/photo.html"
STATE_DIR="${KIOSK_HOME}/.local/state/kitchen-display"
VENVS_DIR="${KIOSK_HOME}/.local/venvs"
PHOTOSWITCH_VENV="${VENVS_DIR}/photoswitch"
CRON_LINE='*/30 * * * * $HOME/.local/bin/sync-photos.py >> $HOME/.local/state/kitchen-display/photo-sync.log 2>&1'
# P3 (#33): the legacy photo producer is retired. photo_app publishes the deck from
# dockerpi and this host only reads it, so a rebuild must never re-arm the 30-minute
# sync cron. INSTALL_LEGACY_PHOTO_SYNC=1 provisions the old pipeline on purpose.
LEGACY_PHOTO_SYNC="${INSTALL_LEGACY_PHOTO_SYNC:-0}"
ENV_FILE="${CONFIG_DIR}/env"
URL_FILE="${CONFIG_DIR}/url"
LAUNCHER_DEST="${CONFIG_DIR}/launch_kiosk.sh"
USER_UNIT_DIR="${KIOSK_HOME}/.config/systemd/user"
USER_UNIT_DEST="${USER_UNIT_DIR}/kitchen-display.service"
HTTP_UNIT_DEST="${USER_UNIT_DIR}/kitchen-display-http.service"
IDLE_UNIT_DEST="${USER_UNIT_DIR}/kitchen-idle-policy.service"
LABWC_DIR="${KIOSK_HOME}/.config/labwc"
AUTOSTART_DEST="${LABWC_DIR}/autostart"
DEFAULT_URL="http://192.168.178.84/kitchen-display/home?kiosk"

PACKAGES=(
    labwc
    chromium
    swayidle
    wlopm
    kanshi
    python3
    fonts-liberation
)

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

if [[ "${LEGACY_PHOTO_SYNC}" == "1" ]]; then
    PLAN_9="Install photo-frame scripts (${SYNC_PHOTOS_DEST}, ${BIN_DIR}/photo_select.py, ${BIN_DIR}/photo_format.py, ${BIN_DIR}/photo_cache.py, ${PHOTO_SWITCH_DEST}), idle policy (${SWAYIDLE_POLICY_DEST}), page (${PHOTO_HTML_DEST}), dirs (${PHOTOS_DIR}, ${STATE_DIR}), and venv (${PHOTOSWITCH_VENV} with websockets)"
    PLAN_10="Add user cron entry for ${KIOSK_USER}: ${CRON_LINE}"
else
    PLAN_9="Install photo page (${PHOTO_HTML_DEST}), photo-switch (${PHOTO_SWITCH_DEST}), idle policy (${SWAYIDLE_POLICY_DEST}), dirs (${PHOTOS_DIR}, ${STATE_DIR}), and venv (${PHOTOSWITCH_VENV} with websockets); legacy sync tools are retired because photo_app publishes the deck from dockerpi (INSTALL_LEGACY_PHOTO_SYNC=1 restores sync-photos.py, photo_select.py, photo_format.py, photo_cache.py)"
    PLAN_10="Leave the photo sync cron disabled for ${KIOSK_USER} and remove any sync-photos.py entry: the photo_app timer on dockerpi is the producer"
fi

print_plan() {
    cat <<EOF
[plan] 1. Install apt packages: ${PACKAGES[*]}
[plan] 2. Configure desktop session autologin for user ${KIOSK_USER} (labwc Wayland session)
[plan] 3. Create settings/env directory ${CONFIG_DIR} and mode-600 env file ${ENV_FILE}
[plan] 4. Initialize default URL file ${URL_FILE} (${DEFAULT_URL}) and local static data directory ${DATA_DIR}
[plan] 5. Install launcher script to ${LAUNCHER_DEST}
[plan] 6. Install systemd user units kitchen-display.service (${USER_UNIT_DEST}), kitchen-display-http.service (${HTTP_UNIT_DEST}) and kitchen-idle-policy.service (${IDLE_UNIT_DEST}) and enable linger
[plan] 7. Install labwc autostart entry to ${AUTOSTART_DEST}
[plan] 8. Install the nightly 02:30 reboot units (kitchen-display-reboot.service, kitchen-display-reboot.timer) from provision/system/ and enable the timer
[plan] 9. ${PLAN_9}
[plan] 10. ${PLAN_10}
EOF
}

if [[ "$DRY_RUN" -eq 1 ]]; then
    print_plan
    exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
    echo "Error: provision/install.sh must be run as root (try sudo)." >&2
    exit 1
fi

if [[ ! -r /proc/device-tree/model ]] || ! grep -q "Raspberry Pi" /proc/device-tree/model; then
    echo "Error: this installer must be run on a Raspberry Pi." >&2
    exit 1
fi

if ! id -u "${KIOSK_USER}" >/dev/null 2>&1; then
    echo "Error: target user '${KIOSK_USER}' does not exist." >&2
    exit 1
fi

KIOSK_GROUP="$(id -gn "${KIOSK_USER}")"

# 1. Install required apt packages if any are missing.
MISSING_PKGS=()
for pkg in "${PACKAGES[@]}"; do
    if ! dpkg -s "$pkg" >/dev/null 2>&1; then
        MISSING_PKGS+=("$pkg")
    fi
done
if [[ "${#MISSING_PKGS[@]}" -gt 0 ]]; then
    apt-get update -q
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${MISSING_PKGS[@]}"
fi

# 2. Configure desktop autologin (B4) via raspi-config if available.
if command -v raspi-config >/dev/null 2>&1; then
    SUDO_USER="${KIOSK_USER}" raspi-config nonint do_boot_behaviour B4
fi

# 3. Settings directory and private env file (mode 600, never overwritten if present).
install -d -m 0750 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${CONFIG_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" <<'EOF'
# Kitchen display private environment settings (chmod 600).
# Optionally set KIOSK_URL, KIOSK_SCALE, or Home Assistant credentials here.
# KIOSK_URL=http://192.168.178.84/kitchen-display/home?kiosk
# KIOSK_SCALE=1.5
EOF
fi
chown "${KIOSK_USER}:${KIOSK_GROUP}" "${ENV_FILE}"
chmod 0600 "${ENV_FILE}"

# 4. Default URL file and local static data directory (created only if absent).
if [[ ! -f "${URL_FILE}" ]]; then
    printf '%s\n' "${DEFAULT_URL}" > "${URL_FILE}"
    chown "${KIOSK_USER}:${KIOSK_GROUP}" "${URL_FILE}"
    chmod 0644 "${URL_FILE}"
fi

install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${DATA_DIR}"
if [[ ! -f "${SOLAR_PLACEHOLDER}" ]]; then
    cat > "${SOLAR_PLACEHOLDER}" <<'EOF'
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Kitchen Display Solar</title>
  <style>
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: #111317;
      color: #e6edf3;
      font-family: system-ui, -apple-system, sans-serif;
    }
    main { text-align: center; padding: 2rem; }
    p { color: #8b949e; }
  </style>
</head>
<body>
  <main>
    <h1>Solar Forecast &amp; Production</h1>
    <p>Waiting for local solar.html report…</p>
  </main>
</body>
</html>
EOF
    chown "${KIOSK_USER}:${KIOSK_GROUP}" "${SOLAR_PLACEHOLDER}"
    chmod 0644 "${SOLAR_PLACEHOLDER}"
fi

# 5. Install launcher script.
if [[ ! -f "${LAUNCHER_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/launch_kiosk.sh" "${LAUNCHER_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/launch_kiosk.sh" "${LAUNCHER_DEST}"
fi

# 6. Install systemd user units and enable linger for the kiosk user.
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${USER_UNIT_DIR}"
if [[ ! -f "${USER_UNIT_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/kitchen-display.service" "${USER_UNIT_DEST}"; then
    install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/kitchen-display.service" "${USER_UNIT_DEST}"
fi
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${USER_UNIT_DIR}/graphical-session.target.wants"
ln -sfn "${USER_UNIT_DEST}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-display.service"
chown -h "${KIOSK_USER}:${KIOSK_GROUP}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-display.service"

if [[ ! -f "${HTTP_UNIT_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/kitchen-display-http.service" "${HTTP_UNIT_DEST}"; then
    install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/kitchen-display-http.service" "${HTTP_UNIT_DEST}"
fi
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${USER_UNIT_DIR}/default.target.wants"
ln -sfn "${HTTP_UNIT_DEST}" "${USER_UNIT_DIR}/default.target.wants/kitchen-display-http.service"
chown -h "${KIOSK_USER}:${KIOSK_GROUP}" "${USER_UNIT_DIR}/default.target.wants/kitchen-display-http.service"

# The idle policy owns the swayidle timers (5 h by day, 30 min by night; #32).
# The 300 s photo-frame timeout it arms is unchanged. Without this unit the panel
# has no screen-off timeout at all, so its absence is a failure, not a fallback.
if [[ -f "${REPO_ROOT}/kiosk/kitchen-idle-policy.service" ]]; then
    if [[ ! -f "${IDLE_UNIT_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/kitchen-idle-policy.service" "${IDLE_UNIT_DEST}"; then
        install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
            "${REPO_ROOT}/kiosk/kitchen-idle-policy.service" "${IDLE_UNIT_DEST}"
    fi
    ln -sfn "${IDLE_UNIT_DEST}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-idle-policy.service"
    chown -h "${KIOSK_USER}:${KIOSK_GROUP}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-idle-policy.service"
else
    echo "Error: missing kiosk/kitchen-idle-policy.service" >&2
    exit 1
fi
loginctl enable-linger "${KIOSK_USER}"

# 7. Install labwc autostart entry.
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${LABWC_DIR}"
if [[ ! -f "${AUTOSTART_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/autostart" "${AUTOSTART_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/autostart" "${AUTOSTART_DEST}"
fi

# 8. Nightly reboot units. The canonical files live in provision/system/ (installed
#    too by install-root.sh, which is the root/agent entry point). install.sh used to
#    write its own inline 03:00 units, which disagreed with the 02:30 ones actually
#    deployed, so a rebuild silently changed the schedule (#8, K7). Copy the files,
#    never redefine them.
REBOOT_UNIT_DIR="/etc/systemd/system"
for unit in kitchen-display-reboot.service kitchen-display-reboot.timer; do
    if [[ ! -f "${REPO_ROOT}/provision/system/${unit}" ]]; then
        echo "Error: missing provision/system/${unit}" >&2
        exit 1
    fi
    # A malformed unit would only show up at 02:30.
    if command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "${REPO_ROOT}/provision/system/${unit}"
    fi
    install -m 0644 "${REPO_ROOT}/provision/system/${unit}" "${REBOOT_UNIT_DIR}/${unit}"
done
systemctl daemon-reload
systemctl enable --now kitchen-display-reboot.timer

# 9. Install photo-frame scripts, page, data/state dirs, and photoswitch venv.
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${BIN_DIR}" "${PHOTOS_DIR}" "${STATE_DIR}" "${VENVS_DIR}"
if [[ "${LEGACY_PHOTO_SYNC}" == "1" ]]; then
    for mod in sync_photos.py photo_select.py photo_format.py photo_cache.py; do
        [[ -f "${REPO_ROOT}/tools/${mod}" ]] || { echo "Error: missing photo-sync module ${REPO_ROOT}/tools/${mod}" >&2; exit 1; }
    done
    if [[ ! -f "${SYNC_PHOTOS_DEST}" ]] || ! cmp -s "${REPO_ROOT}/tools/sync_photos.py" "${SYNC_PHOTOS_DEST}"; then
        install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
            "${REPO_ROOT}/tools/sync_photos.py" "${SYNC_PHOTOS_DEST}"
    fi
    for mod in photo_select.py photo_format.py photo_cache.py; do
        if [[ ! -f "${BIN_DIR}/${mod}" ]] || ! cmp -s "${REPO_ROOT}/tools/${mod}" "${BIN_DIR}/${mod}"; then
            install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
                "${REPO_ROOT}/tools/${mod}" "${BIN_DIR}/${mod}"
        fi
    done
fi
if [[ ! -f "${PHOTO_SWITCH_DEST}" ]] || ! cmp -s "${REPO_ROOT}/tools/photo_switch.py" "${PHOTO_SWITCH_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/tools/photo_switch.py" "${PHOTO_SWITCH_DEST}"
fi
if [[ ! -f "${REPO_ROOT}/tools/swayidle_policy.py" ]]; then
    echo "Error: missing ${REPO_ROOT}/tools/swayidle_policy.py" >&2
    exit 1
fi
if [[ ! -f "${SWAYIDLE_POLICY_DEST}" ]] || ! cmp -s "${REPO_ROOT}/tools/swayidle_policy.py" "${SWAYIDLE_POLICY_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/tools/swayidle_policy.py" "${SWAYIDLE_POLICY_DEST}"
fi
if [[ ! -f "${PHOTO_HTML_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/photo.html" "${PHOTO_HTML_DEST}"; then
    install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/photo.html" "${PHOTO_HTML_DEST}"
fi

if [[ ! -x "${PHOTOSWITCH_VENV}/bin/python" ]] || ! "${PHOTOSWITCH_VENV}/bin/python" -c "import websockets" >/dev/null 2>&1; then
    if [[ ! -x "${PHOTOSWITCH_VENV}/bin/python" ]]; then
        python3 -m venv "${PHOTOSWITCH_VENV}"
    fi
    "${PHOTOSWITCH_VENV}/bin/pip" install --quiet websockets
    chown -R "${KIOSK_USER}:${KIOSK_GROUP}" "${PHOTOSWITCH_VENV}"
fi

# 10. Photo sync cron: only for the legacy producer. The default path also strips a
#     leftover entry, so a rebuild on a cut-over Pi cannot re-arm sync-photos.py.
EXISTING_CRON="$(crontab -u "${KIOSK_USER}" -l 2>/dev/null || true)"
if [[ "${LEGACY_PHOTO_SYNC}" == "1" ]]; then
    if ! printf '%s\n' "${EXISTING_CRON}" | grep -Fq 'sync-photos.py'; then
        {
            if [[ -n "${EXISTING_CRON}" ]]; then
                printf '%s\n' "${EXISTING_CRON}"
            fi
            printf '%s\n' "${CRON_LINE}"
        } | crontab -u "${KIOSK_USER}" -
    fi
else
    if printf '%s\n' "${EXISTING_CRON}" | grep -Fq 'sync-photos.py'; then
        # grep -Fv exits 1 when every line matched; keep pipefail happy and still
        # install the (possibly empty) remainder.
        KEPT_CRON="$(printf '%s\n' "${EXISTING_CRON}" | grep -Fv 'sync-photos.py' || true)"
        printf '%s\n' "${KEPT_CRON}" | crontab -u "${KIOSK_USER}" -
        echo "Removed the legacy sync-photos.py cron entry for ${KIOSK_USER} (photo_app is the producer)."
    fi
fi
