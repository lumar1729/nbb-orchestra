#!/usr/bin/env bash
set -euo pipefail

# Initial installer for the No Black Boxes orchestra.
#
# Assumes the main LastBlackBox repository is already installed at:
#   ~/NoBlackBoxes/LastBlackBox
#
# Usage:
#   sudo ./install_orchestra.sh <server-ip-or-hostname> <raw-github-base-url>
#
# Example:
#   sudo ./install_orchestra.sh \
#     192.168.1.115 \
#     https://raw.githubusercontent.com/USERNAME/NoBlackBoxes-orchestra/main
#
# Expected files at the GitHub raw base URL:
#   pi_message_client.py
#   play_wav.py
#   install_orchestra_code.sh
#   setup_chrony.sh
#   update_wavs.sh
#
# WAV files themselves are served separately by the orchestra server
# over HTTP (port 8000 by default).

if [[ $# -ne 2 ]]; then
    echo "Usage:"
    echo "  sudo $0 <server-ip-or-hostname> <raw-github-base-url>"
    echo
    echo "Example:"
    echo "  sudo $0 192.168.1.115 \\"
    echo "    https://raw.githubusercontent.com/USERNAME/NoBlackBoxes-orchestra/main"
    exit 1
fi

SERVER_HOST="$1"
RAW_BASE_URL="${2%/}"

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This installer must be run with sudo."
    exit 1
fi

# When a script is run through sudo, $HOME may be /root. Determine the
# home directory of the user who invoked sudo instead.
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
echo "User:          $ORCHESTRA_USER"
echo "Home:          $ORCHESTRA_HOME"
echo "Server:        $SERVER_HOST"
echo "GitHub source: $RAW_BASE_URL"
echo

if [[ ! -d "$LBB_ROOT" ]]; then
    echo "ERROR: Existing LastBlackBox installation not found:"
    echo "  $LBB_ROOT"
    echo
    echo "Clone/install the main No Black Boxes repository first."
    exit 1
fi

# ---------------------------------------------------------------------------
# Install basic prerequisites
# ---------------------------------------------------------------------------

PACKAGES=()

if ! command -v curl >/dev/null 2>&1; then
    PACKAGES+=("curl")
fi

if ! command -v chronyc >/dev/null 2>&1; then
    PACKAGES+=("chrony")
fi

if ! command -v python3 >/dev/null 2>&1; then
    PACKAGES+=("python3")
fi

if [[ ${#PACKAGES[@]} -gt 0 ]]; then
    echo "Installing missing prerequisites:"
    printf '  %s\n' "${PACKAGES[@]}"
    echo

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y "${PACKAGES[@]}"
    echo
else
    echo "Required system tools are already installed."
    echo
fi

# ---------------------------------------------------------------------------
# Download the three component setup scripts
# ---------------------------------------------------------------------------

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

INSTALL_CODE="$TMP_DIR/install_orchestra_code.sh"
SETUP_CHRONY="$TMP_DIR/setup_chrony.sh"
UPDATE_WAVS="$TMP_DIR/update_wavs.sh"

download_script() {
    local filename="$1"
    local destination="$2"
    local url="$RAW_BASE_URL/$filename"

    echo "Downloading $filename..."

    if ! curl \
        --fail \
        --location \
        --silent \
        --show-error \
        --retry 3 \
        --retry-delay 2 \
        "$url" \
        -o "$destination"; then

        echo
        echo "ERROR: Failed to download:"
        echo "  $url"
        exit 1
    fi

    if [[ ! -s "$destination" ]]; then
        echo "ERROR: Downloaded file is empty:"
        echo "  $url"
        exit 1
    fi

    chmod 755 "$destination"
}

echo "Downloading deployment scripts..."
echo

download_script "install_orchestra_code.sh" "$INSTALL_CODE"
download_script "setup_chrony.sh" "$SETUP_CHRONY"
download_script "update_wavs.sh" "$UPDATE_WAVS"

echo
echo "Deployment scripts downloaded successfully."
echo

# ---------------------------------------------------------------------------
# Step 1: Install Python code
# ---------------------------------------------------------------------------

echo "========================================"
echo " Step 1/3 - Install orchestra code"
echo "========================================"
echo

# Run this as the normal user. This ensures $HOME inside
# install_orchestra_code.sh points at the correct account.
sudo -u "$ORCHESTRA_USER" \
    env HOME="$ORCHESTRA_HOME" \
    "$INSTALL_CODE" "$RAW_BASE_URL"

echo

# ---------------------------------------------------------------------------
# Step 2: Configure Chrony
# ---------------------------------------------------------------------------

echo "========================================"
echo " Step 2/3 - Configure Chrony"
echo "========================================"
echo

# setup_chrony.sh needs root privileges but must still see the actual
# user's home so that it can locate ~/pi_message_client.py.
env HOME="$ORCHESTRA_HOME" \
    "$SETUP_CHRONY" "$SERVER_HOST"

echo

# ---------------------------------------------------------------------------
# Step 3: Download WAV library
# ---------------------------------------------------------------------------

echo "========================================"
echo " Step 3/3 - Download WAV library"
echo "========================================"
echo

# WAV files should belong to the normal user rather than root.
sudo -u "$ORCHESTRA_USER" \
    env HOME="$ORCHESTRA_HOME" \
    "$UPDATE_WAVS" "$SERVER_HOST"

echo

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo "========================================"
echo " Orchestra installation complete"
echo "========================================"
echo
echo "Installed for:"
echo "  $ORCHESTRA_USER"
echo
echo "LastBlackBox:"
echo "  $LBB_ROOT"
echo
echo "Orchestra server:"
echo "  $SERVER_HOST"
echo
echo "Chrony status can be checked with:"
echo "  chronyc tracking"
echo "  chronyc sources -v"
echo
echo "The orchestra Pi is ready."
