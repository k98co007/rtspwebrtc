#!/usr/bin/env bash
set -euo pipefail

# Build live555 in MSYS2 MinGW64 environment.
# Run this from MSYS2 MinGW64 shell, or execute via the PowerShell wrapper.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# live555 should be inside third_party/live555/src
LIVE_ROOT="$SCRIPT_DIR/live555"

if [ ! -d "$LIVE_ROOT" ]; then
  echo "live555 source not found at: $LIVE_ROOT"
  echo "Place the live555 source tree under cpp/third_party/live555"
  exit 1
fi

cd "$LIVE_ROOT"

# genMakefiles usually lives at the live555 root
if [ ! -f ./genMakefiles ]; then
  echo "genMakefiles not found at: $LIVE_ROOT"
  echo "Ensure you extracted the live555 distribution correctly (genMakefiles should be present)"
  exit 1
fi
chmod +x ./genMakefiles 2>/dev/null || true

echo "Generating Makefiles for mingw... (running genMakefiles from $LIVE_ROOT)"
./genMakefiles mingw || { echo "genMakefiles failed"; exit 1; }

echo "Starting build (this can take several minutes)..."
# mingw32-make is the usual target in MSYS2 MinGW64
if command -v mingw32-make >/dev/null 2>&1; then
  mingw32-make -j"$(nproc)"
else
  make -j"$(nproc)"
fi

echo "Build finished. Libraries are under: $LIVE_ROOT (check lib files and subdirectories)"
