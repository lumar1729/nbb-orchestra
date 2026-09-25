#!/usr/bin/env bash
set -euo pipefail

# Install/update the two orchestra Python files on an existing
# No Black Boxes installation.
#
# Usage:
#   ./install_orchestra_code.sh
#
# Code is downloaded from:
#   https://github.com/lumar1729/nbb-orchestra
#
# The script deliberately uses $HOME rather than a hard-coded username.

if [[ $# -ne 0 ]]; then
    echo "Usage: $0"
    exit 1
fi

RAW_BASE_URL="https://raw.githubusercontent.com/lumar1729/nbb-orchestra/main"

PI_CLIENT_DEST="$HOME/pi_message_client.py"
LBB_ROOT="$HOME/NoBlackBoxes/LastBlackBox"
GENERATION_DIR="$LBB_ROOT/boxes/audio/signal-processing/python/generation"
PLAY_WAV_DEST="$GENERATION_DIR/play_wav.py"

PI_CLIENT_SOURCE="$RAW_BASE_URL/pi_message_client.py"
PLAY_WAV_SOURCE="$RAW_BASE_URL/play_wav.py"

echo "========================================"
echo " No Black Boxes Orchestra Code Setup"
echo "========================================"
echo

if [[ ! -d "$LBB_ROOT" ]]; then
    echo "ERROR: No Black Boxes installation not found:"
    echo "  $LBB_ROOT"
    echo
    echo "This script assumes the No Black Boxes repository is already"
    echo "installed on this Pi."
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is not installed."
    echo "Install it with:"
    echo "  sudo apt install curl"
    exit 1
fi

mkdir -p "$GENERATION_DIR"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

download_file() {
    local url="$1"
    local destination="$2"
    local description="$3"

    echo "Downloading $description..."
    if ! curl --fail --location --silent --show-error \
        --retry 3 --retry-delay 2 \
        "$url" -o "$destination"; then
        echo "ERROR: Failed to download:"
        echo "  $url"
        exit 1
    fi

    if [[ ! -s "$destination" ]]; then
        echo "ERROR: Downloaded file is empty:"
        echo "  $url"
        exit 1
    fi
}

download_file "$PI_CLIENT_SOURCE" "$TMP_DIR/pi_message_client.py" "pi_message_client.py"
download_file "$PLAY_WAV_SOURCE" "$TMP_DIR/play_wav.py" "play_wav.py"

if ! grep -qE '^#!|import |from ' "$TMP_DIR/pi_message_client.py"; then
    echo "WARNING: pi_message_client.py does not look like a Python source file."
fi

if ! grep -qE '^#!|import |from ' "$TMP_DIR/play_wav.py"; then
    echo "WARNING: play_wav.py does not look like a Python source file."
fi

backup_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        cp "$file" "$file.backup"
        echo "Backed up:"
        echo "  $file.backup"
    fi
}

backup_file "$PI_CLIENT_DEST"
backup_file "$PLAY_WAV_DEST"

install -m 755 "$TMP_DIR/pi_message_client.py" "$PI_CLIENT_DEST"
install -m 755 "$TMP_DIR/play_wav.py" "$PLAY_WAV_DEST"

echo
echo "Installed:"
echo "  $PI_CLIENT_DEST"
echo "  $PLAY_WAV_DEST"
echo
echo "Orchestra code setup complete."
