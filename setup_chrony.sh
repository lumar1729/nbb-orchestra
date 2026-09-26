#!/usr/bin/env bash
set -euo pipefail

# Configure/reconfigure Chrony for the No Black Boxes orchestra.
#
# Usage:
#   sudo ./setup_chrony.sh <server-ip-or-hostname>
#
# Example:
#   sudo ./setup_chrony.sh 192.168.1.115
#
# This script deliberately delegates the actual chrony configuration to
# pi_message_client.py, which already contains the project's canonical
# --setup-chrony implementation.
#
# That implementation:
#   - preserves the existing chrony configuration;
#   - preserves other NTP sources;
#   - removes/replaces only its own managed configuration block;
#   - backs up the original configuration;
#   - restarts chrony;
#   - runs chronyc online + makestep;
#   - waits for a selected/synchronised source.

if [[ $# -ne 1 ]]; then
    echo "Usage: sudo $0 <server-ip-or-hostname>"
    echo
    echo "Example:"
    echo "  sudo $0 192.168.1.115"
    exit 1
fi

SERVER_HOST="$1"

# Under sudo, $HOME may point to /root. Use the invoking user's real home.
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    USER_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    USER_HOME="$HOME"
fi

if [[ -z "$USER_HOME" || ! -d "$USER_HOME" ]]; then
    echo "ERROR: Could not determine the invoking user's home directory."
    exit 1
fi

PI_CLIENT="$USER_HOME/pi_message_client.py"

echo "========================================"
echo " No Black Boxes Chrony Setup"
echo "========================================"
echo
echo "Time server: $SERVER_HOST"
echo

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This script must be run with sudo."
    echo "Example:"
    echo "  sudo $0 $SERVER_HOST"
    exit 1
fi

# The client is the source of truth for chrony setup. Do not duplicate
# its configuration logic here, otherwise the two implementations can
# eventually diverge.
if [[ ! -f "$PI_CLIENT" ]]; then
    echo "ERROR: pi_message_client.py was not found at:"
    echo "  $PI_CLIENT"
    echo
    echo "Run install_orchestra_code.sh first."
    exit 1
fi

# The client checks for chronyc/systemctl, but installing chrony here makes
# the standalone setup script useful on a fresh Pi as well.
if ! command -v chronyc >/dev/null 2>&1; then
    echo "Chrony is not installed. Installing..."
    apt-get update
    apt-get install -y chrony
    echo
fi

echo "Running the project's canonical Chrony setup..."
echo

# Use the same Python interpreter/environment that the existing Pi setup
# expects when available. The client itself only uses the Python standard
# library, so fall back to python3 if the LBB environment is unavailable.
LBB_PYTHON="$USER_HOME/NoBlackBoxes/LastBlackBox/_tmp/LBB/bin/python"

if [[ -x "$LBB_PYTHON" ]]; then
    PYTHON="$LBB_PYTHON"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "ERROR: Could not find a Python 3 interpreter."
    exit 1
fi

"$PYTHON" "$PI_CLIENT" "$SERVER_HOST" --setup-chrony

echo
echo "========================================"
echo " Chrony setup complete"
echo "========================================"
echo
echo "Configured time server: $SERVER_HOST"
echo
echo "You can verify it with:"
echo "  chronyc tracking"
echo "  chronyc sources -v"
