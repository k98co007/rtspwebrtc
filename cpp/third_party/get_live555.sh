#!/usr/bin/env bash
set -euo pipefail

# Download and build live555 for WSL / Linux environments.
# Usage: bash get_live555.sh

DEST_DIR="$(cd "$(dirname "$0")" && pwd)/live555"
TMP_DIR="/tmp/rtspwebrtc_live555"
mkdir -p "$DEST_DIR"
rm -rf "$TMP_DIR"
mkdir -p "$TMP_DIR"
cd "$TMP_DIR"

URLS=(
  "https://download.live555.com/live555-latest.tar.gz"
  "https://live555.s3.amazonaws.com/live555-latest.tar.gz"
  "https://github.com/rg3/live555/archive/refs/heads/master.tar.gz"
  # Additional common mirrors / archives (may or may not exist)
  "https://gitlab.com/live555/live555/-/archive/master/live555-master.tar.gz"
  "https://github.com/live555/live555/archive/refs/heads/master.tar.gz"
)

# Allow users to provide extra candidate URLs via environment variable
if [ -n "${LIVE555_URLS-}" ]; then
  IFS=',' read -ra EXTRA_URLS <<< "$LIVE555_URLS"
  for u in "${EXTRA_URLS[@]}"; do
    URLS+=("$u")
  done
fi

DOWNLOAD_OK=0
for u in "${URLS[@]}"; do
  echo "Trying: $u"
  if command -v curl >/dev/null 2>&1; then
    if curl -fL "$u" -o live555.tar.gz; then
      DOWNLOAD_OK=1
      break
    fi
  elif command -v wget >/dev/null 2>&1; then
    if wget -O live555.tar.gz "$u"; then
      DOWNLOAD_OK=1
      break
    fi
  fi
done

if [ "$DOWNLOAD_OK" -ne 1 ]; then
  echo "\nFailed to download live555 automatically. Trying git clone fallbacks...\n"
  # Try cloning a few known forks/repos as a last resort
  GIT_URLS=(
    "https://github.com/rg3/live555.git"
    "https://github.com/live555/live555.git"
  )
  for g in "${GIT_URLS[@]}"; do
    echo "Attempting git clone: $g"
    if command -v git >/dev/null 2>&1; then
      if git clone --depth=1 "$g" live555-src; then
        mkdir -p "$DEST_DIR/src"
        mv live555-src/* "$DEST_DIR/src/"
        rm -rf live555-src
        DOWNLOAD_OK=1
        break
      else
        rm -rf live555-src
      fi
    fi
  done

  if [ "$DOWNLOAD_OK" -ne 1 ]; then
    echo "\nFailed to obtain live555 automatically.\n"
    echo "Please download live555-latest.tar.gz from https://www.live555.com/liveMedia/ and place/extract it at: $DEST_DIR"
    exit 1
  fi
fi

# Extract into third_party/live555/src
mkdir -p "$DEST_DIR/src"
# Many tarballs have a top-level folder; strip it
tar -xzf live555.tar.gz -C "$DEST_DIR/src" --strip-components=1

cd "$DEST_DIR/src"

# Generate makefiles for linux and build
if [ -x ./genMakefiles ]; then
  echo "Generating makefiles (linux)"
  ./genMakefiles linux
else
  echo "genMakefiles not found; follow live555 docs to generate platform-specific makefiles."
fi

if command -v make >/dev/null 2>&1; then
  echo "Building live555 (this may take a while)"
  make -j"$(nproc)"
  echo "Build finished. Built files are under: $DEST_DIR/src"
else
  echo "Make not found. Install build-essential (or use WSL) and re-run this script."
  exit 1
fi

rm -rf "$TMP_DIR"
echo "live555 download/build completed."
