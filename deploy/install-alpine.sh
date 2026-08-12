#!/bin/sh
#
# Install boilerRoom-edge as an OpenRC service on Alpine Linux, so it comes up
# with the Pi.
#
#   doas ./deploy/install-alpine.sh      (or sudo, or run it as root)
#
# The Raspberry Pi OS installer next door uses systemd, apt and raspi-config,
# none of which exist here. This does the same four jobs the Alpine way:
#
#   * a virtualenv with the Python packages, built with apk's toolchain
#   * SPI and 1-Wire turned on, and their modules loaded at boot
#   * the OpenRC service written, enabled and started
#   * a warning if this is a diskless install, where none of it survives
#
# POSIX sh on purpose: Alpine's /bin/sh is busybox ash, and bash is not
# installed by default. Safe to re-run.

set -eu

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
SERVICE_NAME=boilerroom-edge
INIT_TARGET="/etc/init.d/${SERVICE_NAME}"
CONF_TARGET="/etc/conf.d/${SERVICE_NAME}"
VENV_DIR="${PROJECT_DIR}/.venv-pi"

# Alpine on a Pi is commonly root-only; the agent needs GPIO, SPI and the
# checkout, and Alpine ships none of the udev rules that make those reachable
# from an unprivileged account. Override by editing /etc/conf.d after install.
SERVICE_USER="${SERVICE_USER:-root}"

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root: doas $0" >&2
    exit 1
fi

if [ ! -f /etc/alpine-release ]; then
    echo "This is the Alpine installer and this is not Alpine." >&2
    echo "On Raspberry Pi OS use deploy/install.sh instead." >&2
    exit 1
fi

IS_PI=0
if grep -qi "raspberry pi" /proc/device-tree/model 2>/dev/null; then
    IS_PI=1
fi

echo "Project   : ${PROJECT_DIR}"
echo "Alpine    : $(cat /etc/alpine-release)"
echo "User      : ${SERVICE_USER}"
if [ "${IS_PI}" -eq 1 ]; then
    echo "Hardware  : $(tr -d '\0' < /proc/device-tree/model)"
else
    echo "Hardware  : not a Pi — the agent will run its simulated sensors"
fi
echo

# ---------------------------------------------------------------------------
# Is any of this going to survive a reboot?
# ---------------------------------------------------------------------------
#
# Alpine's diskless mode runs the whole root filesystem from RAM and writes
# nothing back unless you say so. This agent is built around outliving power
# cuts — cached schedule, cached config, credentials, a month of readings — and
# in RAM every one of those is gone on the next boot, while the database grows
# until it eats the memory. Worth stopping over.

DISKLESS=0
if command -v lbu >/dev/null 2>&1 && grep -qE '^\S+ / tmpfs ' /proc/mounts; then
    DISKLESS=1
fi

if [ "${DISKLESS}" -eq 1 ]; then
    echo "############################################################"
    echo "This is a DISKLESS Alpine install: / is a RAM disk."
    echo
    echo "This agent keeps its schedule, config, credentials and a month"
    echo "of readings on disk, precisely so a boiler room that loses"
    echo "power comes back doing the right thing. On a RAM disk all of"
    echo "that is lost at every reboot, and the reading database grows"
    echo "until it exhausts memory."
    echo
    echo "Either run a sys install (setup-disk), or mount persistent"
    echo "storage and put this checkout on it. Then re-run this script."
    echo
    echo "Continuing anyway is fine for a bench test and wrong for a"
    echo "boiler room."
    echo "############################################################"
    echo
    printf "Type 'bench' to continue anyway: "
    read -r answer
    if [ "${answer}" != "bench" ]; then
        exit 1
    fi
    echo
fi

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------
#
# In a virtualenv rather than the system Python: Alpine has no packages for
# RPi.GPIO or spidev, so both are built here from source, and recent Alpine
# refuses pip against the system interpreter anyway.

echo "Packages:"
apk add --no-cache python3 py3-pip py3-virtualenv >/dev/null
echo "  python3, pip"

if [ "${IS_PI}" -eq 1 ]; then
    # RPi.GPIO and spidev are C extensions with no Alpine packages and no musl
    # wheels, so they are compiled here.
    apk add --no-cache build-base python3-dev linux-headers >/dev/null
    echo "  build toolchain (for RPi.GPIO and spidev)"
fi

if [ ! -x "${VENV_DIR}/bin/python" ]; then
    python3 -m venv "${VENV_DIR}"
    echo "  created ${VENV_DIR}"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
"${VENV_DIR}/bin/pip" install --quiet -r "${PROJECT_DIR}/requirements.txt"
echo "  websockets, tzdata"

if [ "${IS_PI}" -eq 1 ]; then
    "${VENV_DIR}/bin/pip" install --quiet -r "${PROJECT_DIR}/requirements_hardware.txt"
    echo "  RPi.GPIO, spidev"
fi

MISSING=""
check_module() {
    "${VENV_DIR}/bin/python" -c "import $1" >/dev/null 2>&1 || MISSING="${MISSING} $1"
}
check_module websockets
if [ "${IS_PI}" -eq 1 ]; then
    check_module RPi.GPIO
    check_module spidev
fi

if [ -n "${MISSING}" ]; then
    echo >&2
    echo "Cannot continue — the virtualenv cannot import:${MISSING}" >&2
    echo "Enabling the service without them would give a device that starts" >&2
    echo "and fails, over and over." >&2
    exit 1
fi
echo

# ---------------------------------------------------------------------------
# Buses
# ---------------------------------------------------------------------------
#
# SPI carries the gas ADC on CE0 and the ST7920 display on CE1; 1-Wire carries
# the temperature probes. No raspi-config here, so the overlays go into the
# boot config by hand and the modules into /etc/modules.

if [ "${IS_PI}" -eq 1 ]; then
    echo "Interfaces:"

    BOOT_CONFIG=""
    for candidate in /boot/usercfg.txt /boot/config.txt /boot/firmware/config.txt; do
        if [ -f "${candidate}" ]; then
            BOOT_CONFIG="${candidate}"
            break
        fi
    done

    if [ -z "${BOOT_CONFIG}" ]; then
        echo "  no boot config found — enable SPI and 1-Wire by hand:"
        echo "      dtparam=spi=on"
        echo "      dtoverlay=w1-gpio"
        REBOOT_NEEDED=1
    else
        REBOOT_NEEDED=0
        for line in "dtparam=spi=on" "dtoverlay=w1-gpio"; do
            if grep -qxF "${line}" "${BOOT_CONFIG}"; then
                echo "  ${line} already in ${BOOT_CONFIG}"
            else
                # /boot is usually mounted read-only on Alpine's Pi images.
                mount -o remount,rw /boot 2>/dev/null || true
                printf '%s\n' "${line}" >> "${BOOT_CONFIG}"
                echo "  ${line} added to ${BOOT_CONFIG}"
                REBOOT_NEEDED=1
            fi
        done
    fi

    for module in spi-bcm2835 w1-gpio w1-therm; do
        if [ -f /etc/modules ] && grep -qxF "${module}" /etc/modules; then
            :
        else
            printf '%s\n' "${module}" >> /etc/modules
        fi
        modprobe "${module}" 2>/dev/null || true
    done
    echo "  modules: spi-bcm2835, w1-gpio, w1-therm"

    [ -e /dev/spidev0.1 ] || REBOOT_NEEDED=1
    [ -d /sys/bus/w1/devices ] || REBOOT_NEEDED=1
    echo
else
    REBOOT_NEEDED=0
fi

# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------

install -m 0755 "${PROJECT_DIR}/deploy/${SERVICE_NAME}.openrc" "${INIT_TARGET}"
echo "Wrote ${INIT_TARGET}"

cat > "${CONF_TARGET}" <<CONF
# Written by deploy/install-alpine.sh. Edit and restart the service to change.
BOILERROOM_DIR="${PROJECT_DIR}"
BOILERROOM_PYTHON="${VENV_DIR}/bin/python"
BOILERROOM_USER="${SERVICE_USER}"
CONF
chmod 0644 "${CONF_TARGET}"
echo "Wrote ${CONF_TARGET}"

# The agent writes the database, log and caches here, and .env beside them when
# credentials are typed at the panel.
install -d -o "${SERVICE_USER}" "${PROJECT_DIR}/data"
if ! su "${SERVICE_USER}" -s /bin/sh -c "test -w '${PROJECT_DIR}'"; then
    echo
    echo "WARNING: ${SERVICE_USER} cannot write ${PROJECT_DIR}." >&2
    echo "         Caches and credentials will not survive a restart." >&2
fi

rc-update add "${SERVICE_NAME}" default >/dev/null 2>&1 || true
rc-service "${SERVICE_NAME}" restart || rc-service "${SERVICE_NAME}" start

echo
rc-service "${SERVICE_NAME}" status || true

echo
echo "Enabled: the agent now starts on boot and restarts if it stops."
echo

if [ ! -f "${PROJECT_DIR}/.env" ] \
   || ! grep -qE '^[[:space:]]*BOILERROOM_DEVICE_USERNAME[[:space:]]*=[[:space:]]*[^[:space:]]' "${PROJECT_DIR}/.env"; then
    echo "No device credentials in ${PROJECT_DIR}/.env — the device will ask for"
    echo "them on its panel, and write them there once the server accepts them."
    echo
fi

if [ "${REBOOT_NEEDED}" -eq 1 ]; then
    echo "REBOOT REQUIRED for the SPI and 1-Wire device nodes to appear."
    echo "Until then the probes read as unavailable and the display stays dark."
    echo
fi

if [ "${DISKLESS}" -eq 1 ]; then
    echo "DISKLESS: run 'lbu commit -d' to keep the service across a reboot —"
    echo "and note the data directory still will not persist."
    echo
fi

echo "Follow the agent's own log:  tail -f ${PROJECT_DIR}/data/boilerroom.log"
echo "Follow OpenRC's view:        tail -f /var/log/${SERVICE_NAME}.log"
echo "Service control:             rc-service ${SERVICE_NAME} {status,restart,stop}"
echo "Stop it starting on boot:    rc-update del ${SERVICE_NAME} default"
