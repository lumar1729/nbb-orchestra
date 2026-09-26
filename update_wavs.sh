#!/usr/bin/env bash
set -euo pipefail

# Update the No Black Boxes orchestra WAV library from a server on the LAN.
#
# Usage:
#   ./update_wavs.sh <server-ip-or-hostname> [-d|--default <wav-file>]
#
# Examples:
#   ./update_wavs.sh 192.168.1.115
#   ./update_wavs.sh 192.168.1.115 -d Strings
#   ./update_wavs.sh 192.168.1.115 --default Strings.wav
#
# The server is expected to expose the WAV directory over HTTP on port 8000.
# Example server command:
#   python -m http.server 8000 --directory /path/to/wav
#
# The WAV files are installed at:
#   $HOME/NoBlackBoxes/LastBlackBox/boxes/audio/signal-processing/generation/wav
#
# The existing WAV directory is replaced only after the new library has
# downloaded successfully.

usage() {
    echo "Usage: $0 <server-ip-or-hostname> [-d|--default <wav-file>]"
    echo
    echo "Examples:"
    echo "  $0 192.168.1.115"
    echo "  $0 192.168.1.115 -d Strings"
    echo "  $0 192.168.1.115 --default Strings.wav"
}

if [[ $# -lt 1 ]]; then
    usage
    exit 1
fi

SERVER_HOST="$1"
shift

DEFAULT_WAV=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -d|--default)
            if [[ $# -lt 2 || -z "$2" ]]; then
                echo "ERROR: $1 requires a WAV filename."
                usage
                exit 1
            fi
            DEFAULT_WAV="$2"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

if [[ -n "$DEFAULT_WAV" && "${DEFAULT_WAV,,}" != *.wav ]]; then
    DEFAULT_WAV="${DEFAULT_WAV}.wav"
fi
SERVER_PORT="${ORCHESTRA_WAV_PORT:-8001}"
SERVER_URL="http://${SERVER_HOST}:${SERVER_PORT}"

LBB_ROOT="$HOME/NoBlackBoxes/LastBlackBox"
GENERATION_DIR="$LBB_ROOT/boxes/audio/signal-processing/python/generation"
WAV_DIR="$GENERATION_DIR/wav"
DEFAULT_WAV_FILE="$GENERATION_DIR/default_wav.txt"

echo "========================================"
echo " No Black Boxes WAV Library Update"
echo "========================================"
echo
echo "WAV server: $SERVER_URL"
echo "Destination: $WAV_DIR"
if [[ -n "$DEFAULT_WAV" ]]; then
    echo "New default: $DEFAULT_WAV"
fi
echo

if [[ ! -d "$LBB_ROOT" ]]; then
    echo "ERROR: No Black Boxes installation not found:"
    echo "  $LBB_ROOT"
    exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR: curl is not installed."
    echo "Install it with:"
    echo "  sudo apt install curl"
    exit 1
fi

mkdir -p "$GENERATION_DIR"

TMP_ROOT="$(mktemp -d)"
TMP_WAV_DIR="$TMP_ROOT/wav"
BACKUP_WAV_DIR="$TMP_ROOT/wav.old"

cleanup() {
    rm -rf "$TMP_ROOT"
}
trap cleanup EXIT

mkdir -p "$TMP_WAV_DIR"

echo "Checking connection to WAV server..."

if ! curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 10 \
    "$SERVER_URL/" \
    -o /dev/null; then
    echo
    echo "ERROR: Could not connect to:"
    echo "  $SERVER_URL"
    echo
    echo "Make sure the server is running the WAV HTTP server and"
    echo "that the Pi and server are on the same network."
    exit 1
fi

echo "Server is reachable."
echo
echo "Downloading WAV library..."

# Fetch the directory listing produced by Python's http.server.
INDEX_FILE="$TMP_ROOT/index.html"

if ! curl --fail --silent --show-error \
    --connect-timeout 5 \
    --max-time 30 \
    "$SERVER_URL/" \
    -o "$INDEX_FILE"; then
    echo "ERROR: Failed to retrieve the server directory listing."
    exit 1
fi

# Extract links ending in .wav. Python's http.server HTML is simple enough
# for this purpose, and the filenames are URL-encoded before downloading.
mapfile -t WAV_URLS < <(
    python3 - "$INDEX_FILE" "$SERVER_URL" <<'PY'
import html
import re
import sys
from urllib.parse import urljoin

index_file = sys.argv[1]
base_url = sys.argv[2].rstrip("/") + "/"

with open(index_file, "r", encoding="utf-8", errors="replace") as f:
    text = f.read()

# Python http.server emits href="filename.wav" links.
for match in re.finditer(r'href=["\']([^"\']+\.wav)["\']', text, re.IGNORECASE):
    href = html.unescape(match.group(1))
    print(urljoin(base_url, href))
PY
)

if [[ ${#WAV_URLS[@]} -eq 0 ]]; then
    echo "ERROR: No .wav files were found on the server."
    echo
    echo "Expected the server directory to contain files such as:"
    echo "  example.wav"
    echo "  another_sound.wav"
    exit 1
fi

echo "Found ${#WAV_URLS[@]} WAV file(s)."
echo

COUNT=0

for url in "${WAV_URLS[@]}"; do
    filename="$(basename "${url%%\?*}")"
    destination="$TMP_WAV_DIR/$filename"

    COUNT=$((COUNT + 1))
    echo "[$COUNT/${#WAV_URLS[@]}] $filename"

    if ! curl --fail --location --silent --show-error \
        --retry 3 --retry-delay 2 \
        --connect-timeout 10 \
        --max-time 600 \
        "$url" \
        -o "$destination"; then
        echo
        echo "ERROR: Failed to download:"
        echo "  $url"
        echo
        echo "The existing WAV library has NOT been changed."
        exit 1
    fi

    if [[ ! -s "$destination" ]]; then
        echo
        echo "ERROR: Downloaded file is empty:"
        echo "  $filename"
        echo
        echo "The existing WAV library has NOT been changed."
        exit 1
    fi
done

echo
echo "All WAV files downloaded successfully."

# Count the files actually downloaded.
DOWNLOADED_COUNT="$(find "$TMP_WAV_DIR" -maxdepth 1 -type f -iname '*.wav' | wc -l)"

if [[ "$DOWNLOADED_COUNT" -eq 0 ]]; then
    echo "ERROR: Temporary WAV library is empty."
    exit 1
fi

echo "Downloaded $DOWNLOADED_COUNT WAV file(s)."

RESOLVED_DEFAULT=""
if [[ -n "$DEFAULT_WAV" ]]; then
    # Match case-insensitively but preserve the exact downloaded filename.
    while IFS= read -r -d '' candidate; do
        candidate_name="$(basename "$candidate")"
        if [[ "${candidate_name,,}" == "${DEFAULT_WAV,,}" ]]; then
            RESOLVED_DEFAULT="$candidate_name"
            break
        fi
    done < <(find "$TMP_WAV_DIR" -maxdepth 1 -type f -iname '*.wav' -print0)

    if [[ -z "$RESOLVED_DEFAULT" ]]; then
        echo
        echo "ERROR: Requested default WAV was not found in the downloaded library:"
        echo "  $DEFAULT_WAV"
        echo
        echo "The existing WAV library and default have NOT been changed."
        exit 1
    fi
fi

echo
echo "Replacing existing WAV library..."

# Atomic-ish replacement:
# 1. Move the old directory out of the way.
# 2. Move the complete new directory into place.
# 3. Remove the old directory.
#
# Because the new library is fully downloaded before this starts, a failed
# network transfer cannot leave the Pi with a partially updated library.

if [[ -d "$WAV_DIR" ]]; then
    mv "$WAV_DIR" "$BACKUP_WAV_DIR"
fi

if ! mv "$TMP_WAV_DIR" "$WAV_DIR"; then
    echo "ERROR: Could not install the new WAV library."

    # Restore the old library if possible.
    if [[ ! -d "$WAV_DIR" && -d "$BACKUP_WAV_DIR" ]]; then
        mv "$BACKUP_WAV_DIR" "$WAV_DIR"
        echo "The previous WAV library has been restored."
    fi

    exit 1
fi

rm -rf "$BACKUP_WAV_DIR"

if [[ -n "$RESOLVED_DEFAULT" ]]; then
    printf '%s\n' "$RESOLVED_DEFAULT" > "$DEFAULT_WAV_FILE"
fi

echo
echo "========================================"
echo " WAV update complete"
echo "========================================"
echo
echo "Installed $DOWNLOADED_COUNT WAV file(s) to:"
echo "  $WAV_DIR"
if [[ -n "$RESOLVED_DEFAULT" ]]; then
    echo
    echo "Default WAV: $RESOLVED_DEFAULT"
    echo "Saved to: $DEFAULT_WAV_FILE"
fi
echo
