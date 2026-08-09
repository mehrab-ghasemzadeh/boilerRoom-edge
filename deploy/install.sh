#!/usr/bin/env bash
#
# Install boilerRoom-edge as a systemd service.
#
#   sudo ./deploy/install.sh
#
# Rewrites the unit's user and paths to match this checkout, so it works from
# wherever the repository actually lives.

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

if [[ ! -f "${PROJECT_DIR}/.env" ]]; then
    echo "Missing ${PROJECT_DIR}/.env — copy .env.example and fill in the device" >&2
    echo "credentials before starting the service." >&2
    exit 1
fi

echo "Project   : ${PROJECT_DIR}"
echo "User      : ${SERVICE_USER}:${SERVICE_GROUP}"
echo "Python    : ${PYTHON_BIN}"

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

systemctl daemon-reload
systemctl enable "${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"

echo
systemctl --no-pager --lines=0 status "${UNIT_NAME}" || true
echo
echo "Follow the agent's own log:  tail -f ${PROJECT_DIR}/data/boilerroom.log"
echo "Follow systemd's view:       journalctl -u ${UNIT_NAME} -f"
