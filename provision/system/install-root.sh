#!/usr/bin/env bash
# Install the parts of the kitchen display that need root (#16, and the unblock for
# #8): the nightly reboot timer and, unless --no-sudoers, a sudoers drop-in that lets
# the hermes account manage the panel's own units without a prompt.
#
# Run as root on the Pi:   sudo ./install-root.sh [--no-sudoers]
#
# Idempotent: re-running reinstalls the units, reloads systemd, and leaves the timer
# enabled. Nothing here touches the kiosk session or the photo mirror.
set -euo pipefail

UNIT_DIR="/etc/systemd/system"
SUDOERS_FILE="/etc/sudoers.d/020-hermes-kitchen"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SUDOERS=1

for arg in "$@"; do
    case "${arg}" in
        --no-sudoers) INSTALL_SUDOERS=0 ;;
        -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown argument: ${arg}" >&2; exit 2 ;;
    esac
done

if [[ "$(id -u)" -ne 0 ]]; then
    echo "install-root.sh: must run as root (try: sudo $0 $*)" >&2
    exit 1
fi

for unit in kitchen-display-reboot.service kitchen-display-reboot.timer; do
    if [[ ! -f "${SOURCE_DIR}/${unit}" ]]; then
        echo "install-root.sh: missing ${SOURCE_DIR}/${unit}" >&2
        exit 1
    fi
    # Verify before installing: a malformed unit would only show up at 02:30.
    systemd-analyze verify "${SOURCE_DIR}/${unit}" || {
        echo "install-root.sh: ${unit} failed systemd-analyze verify" >&2
        exit 1
    }
    install -m 0644 "${SOURCE_DIR}/${unit}" "${UNIT_DIR}/${unit}"
done

systemctl daemon-reload
systemctl enable --now kitchen-display-reboot.timer

if [[ "${INSTALL_SUDOERS}" -eq 1 ]]; then
    if [[ -f "${SOURCE_DIR}/sudoers-020-hermes-kitchen" ]]; then
        # Validate the source copy first: installing an unparsable file into
        # /etc/sudoers.d would break sudo for every account on the box.
        visudo -c -f "${SOURCE_DIR}/sudoers-020-hermes-kitchen" >/dev/null || {
            echo "install-root.sh: sudoers drop-in failed visudo -c" >&2
            exit 1
        }
        install -m 0440 -o root -g root "${SOURCE_DIR}/sudoers-020-hermes-kitchen" "${SUDOERS_FILE}"
        echo "installed ${SUDOERS_FILE} (systemctl/shutdown/apt without a password for hermes;"
        echo "  remove it to revoke — see the comment inside the file)"
    else
        echo "install-root.sh: sudoers file missing, skipping" >&2
    fi
fi

echo
echo "--- timer:"
systemctl list-timers kitchen-display-reboot.timer --all --no-pager
echo
echo "--- schedule:"
systemd-analyze calendar '*-*-* 02:30:00' --iterations=1
echo
echo "--- enabled at boot:"
systemctl is-enabled kitchen-display-reboot.timer
