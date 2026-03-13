#!/bin/bash
# GCE Rescue installer (Linux / macOS / Cloud Shell / WSL / Google Corp)
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

# --- Step 2: Find Python >= 3.9 ---
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

# --- Step 3: Find a working pip ---
PIP=""

# Option 1: python -m pip
if $PYTHON -m pip --version >/dev/null 2>&1; then
  PIP="$PYTHON -m pip"
fi

# Option 2: standalone pip3 / pip
if [ -z "$PIP" ]; then
  for pipcmd in pip3 pip; do
    if command -v "$pipcmd" >/dev/null 2>&1; then
      # Verify it's for the right Python
      pip_python=$("$pipcmd" --version 2>/dev/null | grep -oP 'python \K[0-9]+\.[0-9]+' || echo "")
      if [ -n "$pip_python" ]; then
        pip_major=$(echo "$pip_python" | cut -d. -f1)
        pip_minor=$(echo "$pip_python" | cut -d. -f2)
        if [ "$pip_major" -eq 3 ] && [ "$pip_minor" -ge "$MIN_PYTHON_MINOR" ]; then
          PIP="$pipcmd"
          break
        fi
      else
        # Can't verify version, use it anyway
        PIP="$pipcmd"
        break
      fi
    fi
  done
fi

# Option 3: Try to install pip
if [ -z "$PIP" ]; then
  info "pip not found. Attempting to install..."

  # Try ensurepip
  $PYTHON -m ensurepip --upgrade >/dev/null 2>&1 && PIP="$PYTHON -m pip" || true

  # Try package manager
  if [ -z "$PIP" ] && [ "$PLATFORM" = "linux" ]; then
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip >/dev/null 2>&1
    elif command -v yum >/dev/null 2>&1; then
      sudo yum install -y -q python3-pip >/dev/null 2>&1
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y -q python3-pip >/dev/null 2>&1
    fi
    $PYTHON -m pip --version >/dev/null 2>&1 && PIP="$PYTHON -m pip" || true
  fi

  # macOS: try brew Python (includes pip)
  if [ -z "$PIP" ] && [ "$PLATFORM" = "macos" ]; then
    if command -v brew >/dev/null 2>&1; then
      info "Installing Python via Homebrew (includes pip)..."
      brew install -q python3 2>/dev/null
      for cmd in python3 python; do
        if command -v "$cmd" >/dev/null 2>&1 && $cmd -m pip --version >/dev/null 2>&1; then
          PYTHON="$cmd"
          PIP="$cmd -m pip"
          break
        fi
      done
    fi
  fi

  # Last resort: get-pip.py
  if [ -z "$PIP" ]; then
    curl -sSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py 2>/dev/null \
      && $PYTHON /tmp/get-pip.py --user --quiet 2>/dev/null \
      && rm -f /tmp/get-pip.py || true
    $PYTHON -m pip --version >/dev/null 2>&1 && PIP="$PYTHON -m pip" || true
  fi

  [ -z "$PIP" ] && fail "Cannot install pip.\n\n  Try one of:\n    brew install python3          (macOS)\n    sudo apt install python3-pip  (Debian/Ubuntu)\n    sudo yum install python3-pip  (RHEL/CentOS)\n    curl https://bootstrap.pypa.io/get-pip.py | python3"
fi

ok "pip: $($PIP --version 2>&1 | awk '{print $2}')"

# --- Step 4: Install gce-rescue ---
info "Installing gce-rescue..."

# Use archive URL (no git required). Show full errors on failure.
$PIP install --upgrade "$ARCHIVE" \
  || $PIP install --upgrade "$ARCHIVE" --user \
  || fail "pip install failed. Try manually: $PIP install $ARCHIVE"

# --- Step 5: Verify ---
if command -v gce-rescue >/dev/null 2>&1; then
  ok "gce-rescue $(gce-rescue --version 2>&1 | head -1)"
elif $PYTHON -m gce_rescue_v2.cli --version >/dev/null 2>&1; then
  ok "gce-rescue installed (not in PATH)"
  echo ""
  echo "  Add to PATH:"
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
