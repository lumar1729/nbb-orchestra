#!/usr/bin/env bash
set -euo pipefail

# Initial installer for the No Black Boxes orchestra.
#
# Assumes the main LastBlackBox repository is already installed at:
#   ~/NoBlackBoxes/LastBlackBox
#
# Usage:
#   sudo ./install_orchestra.sh <server-ip-or-hostname>
#
# Example:
#   sudo ./install_orchestra.sh 192.168.1.115

if [[ $# -ne 1 ]]; then
    echo "Usage: sudo $0 <server-ip-or-hostname>"
    echo
    echo "Example:"
    echo "  sudo $0 192.168.1.115"
    exit 1
fi

SERVER_HOST="$1"
RAW_BASE_URL="https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main"

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This installer must be run with sudo."
    exit 1
fi

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
    ORCHESTRA_USER="$SUDO_USER"
    ORCHESTRA_HOME="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
else
    ORCHESTRA_USER="$(id -un)"
    ORCHESTRA_HOME="$HOME"
fi

if [[ -z "$ORCHESTRA_HOME" || ! -d "$ORCHESTRA_HOME" ]]; then
    echo "ERROR: Could not determine the orchestra user's home directory."
    exit 1
fi

LBB_ROOT="$ORCHESTRA_HOME/NoBlackBoxes/LastBlackBox"

echo "========================================"
echo " No Black Boxes Orchestra Installer"
echo "========================================"
echo
echo "User:   $ORCHESTRA_USER"
echo "Home:   $ORCHESTRA_HOME"
echo "Server: $SERVER_HOST"
echo

if [[ ! -d "$LBB_ROOT" ]]; then
    echo "ERROR: Existing LastBlackBox installation not found:"
    echo "  $LBB_ROOT"
    echo
    echo "Clone/install the main No Black Boxes repository first."
    exit 1
fi

PACKAGES=()
command -v curl >/dev/null 2>&1 || PACKAGES+=("curl")
command -v chronyc >/dev/null 2>&1 || PACKAGES+=("chrony")
command -v python3 >/dev/null 2>&1 || PACKAGES+=("python3")

if [[ ${#PACKAGES[@]} -gt 0 ]]; then
    echo "Installing missing prerequisites:"
    printf '  %s\n' "${PACKAGES[@]}"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"
    echo
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

download_script() {
    local filename="$1"
    local destination="$TMP_DIR/$filename"

    echo "Downloading $filename..."
    if ! curl --fail --location --silent --show-error \
        --retry 3 --retry-delay 2 \
        "$RAW_BASE_URL/$filename" -o "$destination"; then
        echo "ERROR: Failed to download $filename."
        exit 1
    fi

    if [[ ! -s "$destination" ]]; then
        echo "ERROR: Downloaded $filename is empty."
        exit 1
    fi

    chmod 755 "$destination"
}

echo "Downloading deployment scripts..."
download_script "install_orchestra_code.sh"
download_script "setup_chrony.sh"
download_script "update_wavs.sh"

# The installer itself is running as root, so mktemp creates a private
# root-owned directory. Steps 1 and 3 run as the normal Pi user, so give
# that user access to the downloaded deployment scripts.
chown -R "$ORCHESTRA_USER:$ORCHESTRA_USER" "$TMP_DIR"

echo

echo "========================================"
echo " Step 1/3 - Install orchestra code"
echo "========================================"
sudo -u "$ORCHESTRA_USER" \
    env HOME="$ORCHESTRA_HOME" \
    "$TMP_DIR/install_orchestra_code.sh"
echo

echo "========================================"
echo " Step 2/3 - Configure Chrony"
echo "========================================"
env HOME="$ORCHESTRA_HOME" \
    "$TMP_DIR/setup_chrony.sh" "$SERVER_HOST"
echo

echo "========================================"
echo " Step 3/3 - Download WAV library"
echo "========================================"
sudo -u "$ORCHESTRA_USER" \
    env HOME="$ORCHESTRA_HOME" \
    "$TMP_DIR/update_wavs.sh" "$SERVER_HOST"
echo

echo "========================================"
echo " Orchestra installation complete"
echo "========================================"
echo
echo "Installed for: $ORCHESTRA_USER"
echo "LastBlackBox:  $LBB_ROOT"
echo "Server:        $SERVER_HOST"
echo
echo "Chrony status:"
echo "  chronyc tracking"
echo "  chronyc sources -v"
