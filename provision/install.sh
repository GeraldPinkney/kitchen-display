#!/usr/bin/env bash
# Idempotent provisioning script for the kitchen-display Raspberry Pi 5 kiosk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

KIOSK_USER="${KIOSK_USER:-kiosk}"
KIOSK_HOME="/home/${KIOSK_USER}"
CONFIG_DIR="${KIOSK_HOME}/.config/kitchen-display"
ENV_FILE="${CONFIG_DIR}/env"
URL_FILE="${CONFIG_DIR}/url"
LAUNCHER_DEST="${CONFIG_DIR}/launch_kiosk.sh"
USER_UNIT_DIR="${KIOSK_HOME}/.config/systemd/user"
USER_UNIT_DEST="${USER_UNIT_DIR}/kitchen-display.service"
LABWC_DIR="${KIOSK_HOME}/.config/labwc"
AUTOSTART_DEST="${LABWC_DIR}/autostart"
REBOOT_SERVICE="/etc/systemd/system/kitchen-display-reboot.service"
REBOOT_TIMER="/etc/systemd/system/kitchen-display-reboot.timer"
DEFAULT_URL="http://192.168.178.84/kitchen/home?kiosk"

PACKAGES=(
    labwc
    chromium
    swayidle
    wlopm
    fonts-liberation
)

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=1
fi

print_plan() {
    cat <<EOF
[plan] 1. Install apt packages: ${PACKAGES[*]}
[plan] 2. Configure desktop session autologin for user ${KIOSK_USER} (labwc Wayland session)
[plan] 3. Create settings/env directory ${CONFIG_DIR} and mode-600 env file ${ENV_FILE}
[plan] 4. Initialize default URL file ${URL_FILE} (${DEFAULT_URL})
[plan] 5. Install launcher script to ${LAUNCHER_DEST}
[plan] 6. Install systemd user unit kitchen-display.service to ${USER_UNIT_DEST} and enable linger
[plan] 7. Install labwc autostart entry to ${AUTOSTART_DEST}
[plan] 8. Install and enable nightly 03:00 reboot timer (${REBOOT_TIMER})
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
    raspi-config nonint do_boot_behaviour B4
fi

# 3. Settings directory and private env file (mode 600, never overwritten if present).
install -d -m 0750 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${CONFIG_DIR}"
if [[ ! -f "${ENV_FILE}" ]]; then
    cat > "${ENV_FILE}" <<'EOF'
# Kitchen display private environment settings (chmod 600).
# Optionally set KIOSK_URL or Home Assistant credentials here.
# KIOSK_URL=http://192.168.178.84/kitchen/home?kiosk
EOF
fi
chown "${KIOSK_USER}:${KIOSK_GROUP}" "${ENV_FILE}"
chmod 0600 "${ENV_FILE}"

# 4. Default URL file (created only if absent).
if [[ ! -f "${URL_FILE}" ]]; then
    printf '%s\n' "${DEFAULT_URL}" > "${URL_FILE}"
    chown "${KIOSK_USER}:${KIOSK_GROUP}" "${URL_FILE}"
    chmod 0644 "${URL_FILE}"
fi

# 5. Install launcher script.
if [[ ! -f "${LAUNCHER_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/launch_kiosk.sh" "${LAUNCHER_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/launch_kiosk.sh" "${LAUNCHER_DEST}"
fi

# 6. Install systemd user unit and enable linger for the kiosk user.
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${USER_UNIT_DIR}"
if [[ ! -f "${USER_UNIT_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/kitchen-display.service" "${USER_UNIT_DEST}"; then
    install -m 0644 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/kitchen-display.service" "${USER_UNIT_DEST}"
fi
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${USER_UNIT_DIR}/graphical-session.target.wants"
ln -sfn "${USER_UNIT_DEST}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-display.service"
chown -h "${KIOSK_USER}:${KIOSK_GROUP}" "${USER_UNIT_DIR}/graphical-session.target.wants/kitchen-display.service"
loginctl enable-linger "${KIOSK_USER}"

# 7. Install labwc autostart entry.
install -d -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" "${LABWC_DIR}"
if [[ ! -f "${AUTOSTART_DEST}" ]] || ! cmp -s "${REPO_ROOT}/kiosk/autostart" "${AUTOSTART_DEST}"; then
    install -m 0755 -o "${KIOSK_USER}" -g "${KIOSK_GROUP}" \
        "${REPO_ROOT}/kiosk/autostart" "${AUTOSTART_DEST}"
fi

# 8. Install and enable nightly 03:00 reboot timer.
if [[ ! -f "${REBOOT_SERVICE}" ]]; then
    cat > "${REBOOT_SERVICE}" <<'EOF'
[Unit]
Description=Nightly reboot for kitchen-display kiosk

[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl reboot
EOF
fi

if [[ ! -f "${REBOOT_TIMER}" ]]; then
    cat > "${REBOOT_TIMER}" <<'EOF'
[Unit]
Description=Nightly 03:00 reboot timer for kitchen-display kiosk

[Timer]
OnCalendar=*-*-* 03:00:00
Persistent=false

[Install]
WantedBy=timers.target
EOF
fi

systemctl daemon-reload
systemctl enable --now kitchen-display-reboot.timer
