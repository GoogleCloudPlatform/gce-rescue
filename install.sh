#!/bin/bash
# GCE Rescue installer (Linux / macOS / Cloud Shell / WSL)
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/v2-beta/install.sh | bash
#
# For Windows PowerShell, use install.ps1 instead.

set -e

BRANCH="v2-beta"
ARCHIVE="https://github.com/GoogleCloudPlatform/gce-rescue/archive/${BRANCH}.tar.gz"
MIN_PYTHON_MINOR=9

info()  { echo "  $*"; }
ok()    { echo "  [OK] $*"; }
fail()  { echo "  [FAILED] $*"; exit 1; }

echo ""
echo "GCE Rescue - Installer"
echo "======================"
echo ""

# --- Step 1: Detect OS ---
OS="$(uname -s)"
case "$OS" in
  Linux*)  PLATFORM="linux" ;;
  Darwin*) PLATFORM="macos" ;;
  *)       fail "Unsupported platform: $OS. Use install.ps1 for Windows." ;;
esac
info "Platform: $PLATFORM"

# --- Step 2: Find or install Python >= 3.9 ---
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
    major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
    if [ "$major" -eq 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
      PYTHON="$cmd"
      break
    fi
  fi
done

if [ -z "$PYTHON" ]; then
  info "Python >= 3.${MIN_PYTHON_MINOR} not found. Attempting to install..."
  if [ "$PLATFORM" = "linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip python3-venv >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
      sudo yum install -y -q python3 python3-pip >/dev/null 2>&1
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y -q python3 python3-pip >/dev/null 2>&1
    else
      fail "Cannot auto-install Python. Install Python >= 3.${MIN_PYTHON_MINOR} manually and re-run."
    fi
  elif [ "$PLATFORM" = "macos" ]; then
    if command -v brew >/dev/null 2>&1; then
      brew install -q python3 2>/dev/null
    else
      fail "Install Python >= 3.${MIN_PYTHON_MINOR}: https://www.python.org/downloads/"
    fi
  fi
  # Re-check
  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
      minor=$("$cmd" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
      major=$("$cmd" -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
      if [ "$major" -eq 3 ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]; then
        PYTHON="$cmd"
        break
      fi
    fi
  done
  [ -z "$PYTHON" ] && fail "Python installation failed. Install Python >= 3.${MIN_PYTHON_MINOR} manually."
fi
ok "Python: $($PYTHON --version 2>&1)"

# --- Step 3: Ensure pip is available ---
if ! $PYTHON -m pip --version >/dev/null 2>&1; then
  info "pip not found. Installing..."

  # Try ensurepip first
  $PYTHON -m ensurepip --upgrade >/dev/null 2>&1 || true

  # Try package manager
  if ! $PYTHON -m pip --version >/dev/null 2>&1; then
    if [ "$PLATFORM" = "linux" ]; then
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip >/dev/null 2>&1
      elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y -q python3-pip >/dev/null 2>&1
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y -q python3-pip >/dev/null 2>&1
      fi
    elif [ "$PLATFORM" = "macos" ]; then
      # macOS system Python often lacks pip; install via brew Python instead
      if command -v brew >/dev/null 2>&1; then
        info "macOS system Python lacks pip. Installing Python via Homebrew..."
        brew install -q python3 2>/dev/null
        # Refresh: brew Python includes pip
        for cmd in python3 python; do
          if command -v "$cmd" >/dev/null 2>&1; then
            if $cmd -m pip --version >/dev/null 2>&1; then
              PYTHON="$cmd"
              break
            fi
          fi
        done
      fi
    fi
  fi

  # Last resort: get-pip.py
  if ! $PYTHON -m pip --version >/dev/null 2>&1; then
    curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py \
      && $PYTHON /tmp/get-pip.py --quiet >/dev/null 2>&1 \
      && rm -f /tmp/get-pip.py \
      || fail "Cannot install pip. Try: brew install python3 (macOS) or sudo apt install python3-pip (Linux)"
  fi
fi
ok "pip: $($PYTHON -m pip --version 2>&1 | awk '{print $2}')"

# --- Step 4: Install gce-rescue ---
info "Installing gce-rescue..."

# Use archive URL (no git required)
$PYTHON -m pip install --quiet --upgrade "$ARCHIVE" 2>/dev/null \
  || $PYTHON -m pip install --quiet --upgrade "$ARCHIVE" --user 2>/dev/null \
  || fail "pip install failed. Check network connectivity and try again."

# --- Step 5: Verify ---
# Check multiple possible locations
if command -v gce-rescue >/dev/null 2>&1; then
  ok "gce-rescue $(gce-rescue --version 2>&1 | head -1)"
elif $PYTHON -m gce_rescue_v2.cli --version >/dev/null 2>&1; then
  # Installed but not in PATH (--user install)
  USER_BIN="$($PYTHON -c 'import site; print(site.getusersitepackages().replace("lib/python/site-packages","bin").replace("site-packages","../../bin"))' 2>/dev/null || echo "")"
  ok "gce-rescue installed (not in PATH)"
  echo ""
  echo "  Add to PATH by running:"
  echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  echo ""
  echo "  Or run directly:"
  echo "    $PYTHON -m gce_rescue_v2.cli --help"
  echo ""
  exit 0
else
  fail "Installation verification failed."
fi

# --- Done ---
echo ""
echo "  Usage:"
echo "    gce-rescue diagnose VM_NAME --zone=ZONE"
echo "    gce-rescue repair   VM_NAME --zone=ZONE"
echo "    gce-rescue rescue   VM_NAME --zone=ZONE"
echo "    gce-rescue restore  VM_NAME --zone=ZONE"
echo ""
echo "  Run 'gce-rescue --help' for more options."
echo ""
