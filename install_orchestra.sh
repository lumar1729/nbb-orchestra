#!/usr/bin/env bash
set -euo pipefail

# Initial installer for the No Black Boxes orchestra.
#
# Assumes the main LastBlackBox repository is already installed at:
#   ~/NoBlackBoxes/LastBlackBox
#
# Usage:
#   sudo ./install_orchestra.sh <server-ip-or-hostname> [-d|--default <wav-file>]

usage() {
    echo "Usage: sudo $0 <server-ip-or-hostname> [-d|--default <wav-file>]"
    echo "Examples:"
    echo "  sudo $0 192.168.1.115"
    echo "  sudo $0 192.168.1.115 -d Choir.wav"
}

if [[ $# -lt 1 ]]; then usage; exit 1; fi
SERVER_HOST="$1"
shift
DEFAULT_WAV=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--default)
            [[ $# -ge 2 ]] || { echo "ERROR: $1 requires a WAV filename."; exit 1; }
            DEFAULT_WAV="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "ERROR: Unknown argument: $1"; usage; exit 1 ;;
    esac
done

if [[ -n "$DEFAULT_WAV" && "${DEFAULT_WAV,,}" != *.wav ]]; then
    DEFAULT_WAV="${DEFAULT_WAV}.wav"
fi

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
GENERATION_DIR="$LBB_ROOT/boxes/audio/signal-processing/python/generation"
WAV_DIR="$GENERATION_DIR/wav"
DEFAULT_WAV_FILE="$GENERATION_DIR/default_wav.txt"

echo "========================================"
echo " No Black Boxes Orchestra Installer"
echo "========================================"
echo
echo "User:   $ORCHESTRA_USER"
echo "Home:   $ORCHESTRA_HOME"
echo "Server: $SERVER_HOST"
if [[ -n "$DEFAULT_WAV" ]]; then
    echo "Default WAV: $DEFAULT_WAV"
else
    echo "Default WAV: first WAV alphabetically"
fi
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
echo " Configure default WAV"
echo "========================================"
mapfile -t WAV_FILES < <(
    find "$WAV_DIR" -maxdepth 1 -type f -iname '*.wav' -printf '%f\n' | sort -f
)
if [[ ${#WAV_FILES[@]} -eq 0 ]]; then
    echo "ERROR: No WAV files were found in: $WAV_DIR"
    exit 1
fi

if [[ -n "$DEFAULT_WAV" ]]; then
    SELECTED_WAV=""
    for wav in "${WAV_FILES[@]}"; do
        if [[ "${wav,,}" == "${DEFAULT_WAV,,}" ]]; then
            SELECTED_WAV="$wav"; break
        fi
    done
    if [[ -z "$SELECTED_WAV" ]]; then
        echo "ERROR: Requested default WAV was not found: $DEFAULT_WAV"
        echo "Available WAV files:"
        printf '  %s\n' "${WAV_FILES[@]}"
        exit 1
    fi
else
    SELECTED_WAV="${WAV_FILES[0]}"
fi

printf '%s\n' "$SELECTED_WAV" > "$DEFAULT_WAV_FILE"
chown "$ORCHESTRA_USER:$ORCHESTRA_USER" "$DEFAULT_WAV_FILE"
echo "Default WAV: $SELECTED_WAV"
echo "Saved to: $DEFAULT_WAV_FILE"
echo

echo "========================================"
echo " Orchestra installation complete"
echo "========================================"
echo
echo "Installed for: $ORCHESTRA_USER"
echo "LastBlackBox:  $LBB_ROOT"
echo "Server:        $SERVER_HOST"
echo "Default WAV:   $SELECTED_WAV"
echo
echo "Chrony status:"
echo "  chronyc tracking"
echo "  chronyc sources -v"
