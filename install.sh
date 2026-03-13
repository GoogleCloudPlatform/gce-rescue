#!/bin/bash
# GCE Rescue installer
# Usage: curl -sSL https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/v2-beta/install.sh | bash

set -e

REPO="https://github.com/GoogleCloudPlatform/gce-rescue.git@v2-beta"
MIN_PYTHON="3.9"

echo "Installing GCE Rescue..."

# Find Python 3
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    if [ -n "$version" ]; then
      major=$(echo "$version" | cut -d. -f1)
      minor=$(echo "$version" | cut -d. -f2)
      if [ "$major" -ge 3 ] && [ "$minor" -ge 9 ]; then
        PYTHON="$cmd"
        break
      fi
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  echo "Error: Python >= ${MIN_PYTHON} is required but not found."
  echo ""
  echo "Install Python:"
  echo "  Debian/Ubuntu: sudo apt-get install -y python3 python3-pip"
  echo "  RHEL/CentOS:   sudo yum install -y python3 python3-pip"
  echo "  macOS:          brew install python3"
  exit 1
fi

echo "  Python: $($PYTHON --version)"

# Install
$PYTHON -m pip install --quiet --upgrade pip 2>/dev/null || true
$PYTHON -m pip install --quiet "git+${REPO}" 2>/dev/null

# Verify
if command -v gce-rescue >/dev/null 2>&1; then
  echo "  Version: $(gce-rescue --version 2>&1 | head -1)"
  echo ""
  echo "Done. Run: gce-rescue --help"
else
  # pip installed to user site, not in PATH
  INSTALL_PATH=$($PYTHON -m pip show gce-rescue 2>/dev/null | grep "^Location:" | cut -d' ' -f2)
  echo ""
  echo "Installed, but 'gce-rescue' is not in PATH."
  echo "Try: $PYTHON -m gce_rescue_v2.cli --help"
  echo "Or add pip's bin directory to PATH:"
  echo "  export PATH=\"\$($PYTHON -m site --user-base)/bin:\$PATH\""
  exit 0
fi
