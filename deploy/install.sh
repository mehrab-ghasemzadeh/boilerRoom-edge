#!/usr/bin/env bash
#
# Install boilerRoom-edge as a systemd service, so it comes up with the Pi.
#
#   sudo ./deploy/install.sh
#
# Takes a fresh checkout to a device that starts on boot and restarts after a
# power cut. Four things stand between those, and this does all four:
#
#   * the unit's user and paths, rewritten to match this checkout
#   * the Python packages the hardware needs
#   * SPI and 1-Wire, without which the ADC, the display and the temperature
#     probes have no device nodes to open
#   * enabling the service and starting it
#
# Safe to re-run: everything here is idempotent.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="boilerroom-edge.service"
UNIT_SOURCE="${PROJECT_DIR}/deploy/${UNIT_NAME}"
UNIT_TARGET="/etc/systemd/system/${UNIT_NAME}"

# The account that owns the checkout, not root: the agent has no need for it.
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "${SERVICE_USER}")"
PYTHON_BIN="$(command -v python3)"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi

IS_PI=0
if grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    IS_PI=1
fi

echo "Project   : ${PROJECT_DIR}"
echo "User      : ${SERVICE_USER}:${SERVICE_GROUP}"
echo "Python    : ${PYTHON_BIN}"
echo "Hardware  : $([[ ${IS_PI} -eq 1 ]] && echo "Raspberry Pi" || echo "not a Pi — the agent will run its simulated sensors")"
echo

# ---------------------------------------------------------------------------
# Python packages
# ---------------------------------------------------------------------------
#
# Installed with apt rather than pip: the service runs the system Python, and
# on Bookworm and later pip refuses to touch that (PEP 668). See
# requirements_hardware.txt.

APT_UPDATED=0

apt_install() {
    local package="$1"
    if DEBIAN_FRONTEND=noninteractive apt-get install -y "${package}" >/dev/null 2>&1; then
        return 0
    fi
    # A fresh image has stale package lists; that is worth one retry.
    if [[ ${APT_UPDATED} -eq 0 ]]; then
        APT_UPDATED=1
        echo "    (refreshing package lists)"
        apt-get update >/dev/null 2>&1 || true
        DEBIAN_FRONTEND=noninteractive apt-get install -y "${package}" >/dev/null 2>&1 && return 0
    fi
    return 1
}

MISSING=()

require_module() {
    local module="$1" package="$2" needed="$3"

    if "${PYTHON_BIN}" -c "import ${module}" >/dev/null 2>&1; then
        printf '  %-12s present\n' "${module}"
        return 0
    fi

    if [[ "${needed}" -eq 0 ]]; then
        printf '  %-12s not installed (only needed on the Pi)\n' "${module}"
        return 0
    fi

    printf '  %-12s missing — installing %s\n' "${module}" "${package}"
    if command -v apt-get >/dev/null 2>&1 && apt_install "${package}"; then
        if "${PYTHON_BIN}" -c "import ${module}" >/dev/null 2>&1; then
            printf '  %-12s installed\n' "${module}"
            return 0
        fi
    fi

    MISSING+=("${module}  (apt install ${package})")
    printf '  %-12s STILL MISSING\n' "${module}"
}

echo "Python packages:"
require_module websockets python3-websockets 1
require_module RPi.GPIO python3-rpi.gpio "${IS_PI}"
require_module spidev python3-spidev "${IS_PI}"

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo
    echo "Cannot continue — ${PYTHON_BIN} cannot import:" >&2
    printf '  %s\n' "${MISSING[@]}" >&2
    echo >&2
    echo "Install them and run this again. Enabling the service without them" >&2
    echo "would give a device that restarts and fails until systemd gives up." >&2
    exit 1
fi
echo

# ---------------------------------------------------------------------------
# Buses
# ---------------------------------------------------------------------------
#
# SPI carries the gas ADC on CE0 and the ST7920 display on CE1; 1-Wire carries
# the temperature probes. Without the overlays there are no device nodes to
# open, and the agent comes up reporting every probe as unavailable.

REBOOT_NEEDED=0

if [[ ${IS_PI} -eq 1 ]] && command -v raspi-config >/dev/null 2>&1; then
    echo "Interfaces:"
    # raspi-config's non-interactive API takes 0 for "enable".
    raspi-config nonint do_spi 0 || true
    raspi-config nonint do_onewire 0 || true

    if [[ -e /dev/spidev0.0 && -e /dev/spidev0.1 ]]; then
        echo "  SPI       enabled (spidev0.0 and spidev0.1 present)"
    else
        echo "  SPI       enabled — needs a reboot for the device nodes to appear"
        REBOOT_NEEDED=1
    fi

    if [[ -d /sys/bus/w1/devices ]]; then
        echo "  1-Wire    enabled"
    else
        echo "  1-Wire    enabled — needs a reboot"
        REBOOT_NEEDED=1
    fi
    echo
fi

# ---------------------------------------------------------------------------
# The unit
# ---------------------------------------------------------------------------

# Only add hardware groups that exist on this host; SupplementaryGroups fails
# the unit outright if a named group is missing.
GROUPS_PRESENT=()
for group in gpio spi i2c; do
    if getent group "${group}" >/dev/null; then
        GROUPS_PRESENT+=("${group}")
    fi
done
echo "HW groups : ${GROUPS_PRESENT[*]:-none found}"

python3 - "$UNIT_SOURCE" "$UNIT_TARGET" "$PROJECT_DIR" "$SERVICE_USER" \
         "$SERVICE_GROUP" "$PYTHON_BIN" "${GROUPS_PRESENT[*]:-}" <<'PY'
import sys

source, target, project, user, group, python_bin, hw_groups = sys.argv[1:8]
unit = open(source, encoding="utf-8").read()

unit = unit.replace("User=pi", f"User={user}")
unit = unit.replace("Group=pi", f"Group={group}")
unit = unit.replace(
    "WorkingDirectory=/home/pi/boilerRoom-edge", f"WorkingDirectory={project}"
)
unit = unit.replace(
    "ExecStart=/usr/bin/python3 /home/pi/boilerRoom-edge/src/main.py",
    f"ExecStart={python_bin} {project}/src/main.py",
)

if hw_groups.strip():
    unit = unit.replace(
        "SupplementaryGroups=gpio spi i2c", f"SupplementaryGroups={hw_groups.strip()}"
    )
else:
    unit = unit.replace("SupplementaryGroups=gpio spi i2c\n", "")

open(target, "w", encoding="utf-8").write(unit)
print(f"Wrote {target}")
PY

# The agent writes the database, log and caches here.
install -d -o "${SERVICE_USER}" -g "${SERVICE_GROUP}" "${PROJECT_DIR}/data"

# The caches sit beside the checkout, and .env is written there too when
# credentials are typed at the panel. Without this the agent runs but cannot
# remember anything across a restart, which is a slow, confusing failure.
if ! runuser -u "${SERVICE_USER}" -- test -w "${PROJECT_DIR}"; then
    echo
    echo "WARNING: ${SERVICE_USER} cannot write ${PROJECT_DIR}." >&2
    echo "         Caches and credentials will not survive a restart." >&2
fi

systemctl daemon-reload
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

echo
systemctl --no-pager --lines=0 status "${UNIT_NAME}" || true

echo
echo "Enabled: the agent now starts on boot and restarts if it stops."
echo

if [[ ! -f "${PROJECT_DIR}/.env" ]] \
   || ! grep -qE '^\s*BOILERROOM_DEVICE_USERNAME\s*=\s*\S' "${PROJECT_DIR}/.env"; then
    echo "No device credentials in ${PROJECT_DIR}/.env — the device will ask for"
    echo "them on its panel, and write them there once the server accepts them."
    echo
fi

if [[ ${REBOOT_NEEDED} -eq 1 ]]; then
    echo "REBOOT REQUIRED for the SPI and 1-Wire device nodes to appear."
    echo "Until then the probes read as unavailable and the display stays dark."
    echo
fi

echo "Follow the agent's own log:  tail -f ${PROJECT_DIR}/data/boilerroom.log"
echo "Follow systemd's view:       journalctl -u ${UNIT_NAME} -f"
echo "Stop it starting on boot:    sudo systemctl disable --now ${UNIT_NAME}"
