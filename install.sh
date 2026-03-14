#!/usr/bin/env bash
#
# GCE Rescue installer for Linux and macOS.
#
# Checks prerequisites, installs gce-rescue, configures PATH,
# and sets up authentication. Designed for quick setup during P1 incidents.
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.sh | bash
#

set -euo pipefail

# --- Configuration ---
REPO_URL="https://github.com/gokulr94/gce-rescue/archive/refs/heads/v2-beta.zip"
MIN_PYTHON_VERSION="3.9"
PACKAGE_NAME="gce-rescue"

# --- Helper functions ---

step() {
    printf "\n\033[36m[%s] %s\033[0m\n" "$1" "$2"
}

ok() {
    printf "  \033[32m%s\033[0m\n" "$1"
}

warn() {
    printf "  \033[33m%s\033[0m\n" "$1"
}

fail() {
    printf "  \033[31m%s\033[0m\n" "$1"
}

has_command() {
    command -v "$1" >/dev/null 2>&1
}

ask_yn() {
    # Usage: ask_yn "prompt" default
    # default: Y or N
    # Reads from /dev/tty so it works when script is piped via curl
    local prompt="$1"
    local default="${2:-Y}"
    local reply
    read -r -p "  $prompt " reply < /dev/tty
    reply="${reply:-$default}"
    [[ "$reply" =~ ^[Yy] ]]
}

version_ge() {
    # Returns 0 if $1 >= $2 (version comparison)
    printf '%s\n%s\n' "$2" "$1" | sort -V -C
}

find_python() {
    for cmd in python3 python; do
        if has_command "$cmd"; then
            local ver
            ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
            if [ -n "$ver" ]; then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

# --- Main ---

echo ""
echo "=== GCE Rescue Installer ==="
echo "Sets up gce-rescue and all dependencies on this machine."
echo ""

OS="$(uname -s)"

# ============================================================
# Step 1: Check Python
# ============================================================
step "1/5" "Checking Python..."

PYTHON_CMD=$(find_python) || true

if [ -z "$PYTHON_CMD" ]; then
    fail "Python not found."
    echo ""
    if [ "$OS" = "Darwin" ]; then
        echo "  Install Python on macOS:"
        if has_command brew; then
            echo "    brew install python@3.12"
            echo ""
            if ask_yn "Install Python via Homebrew now? (Y/n)" "Y"; then
                echo "  Installing Python..."
                brew install python@3.12
                PYTHON_CMD=$(find_python) || true
                if [ -z "$PYTHON_CMD" ]; then
                    fail "Python installed but not found in PATH."
                    echo "  Restart your terminal and run this script again."
                    exit 1
                fi
            else
                exit 1
            fi
        else
            echo "    1. Install Homebrew: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
            echo "    2. Then: brew install python@3.12"
            echo "    Or download from: https://www.python.org/downloads/"
            exit 1
        fi
    else
        # Linux
        echo "  Install Python on Linux:"
        if has_command apt-get; then
            echo "    sudo apt-get update && sudo apt-get install -y python3 python3-pip python3-venv"
            echo ""
            if ask_yn "Install Python via apt now? (Y/n)" "Y"; then
                echo "  Installing Python..."
                sudo apt-get update -qq && sudo apt-get install -y -qq python3 python3-pip python3-venv
                PYTHON_CMD=$(find_python) || true
            else
                exit 1
            fi
        elif has_command dnf; then
            echo "    sudo dnf install -y python3 python3-pip"
            echo ""
            if ask_yn "Install Python via dnf now? (Y/n)" "Y"; then
                echo "  Installing Python..."
                sudo dnf install -y -q python3 python3-pip
                PYTHON_CMD=$(find_python) || true
            else
                exit 1
            fi
        elif has_command yum; then
            echo "    sudo yum install -y python3 python3-pip"
            echo ""
            if ask_yn "Install Python via yum now? (Y/n)" "Y"; then
                echo "  Installing Python..."
                sudo yum install -y -q python3 python3-pip
                PYTHON_CMD=$(find_python) || true
            else
                exit 1
            fi
        else
            echo "    Download from: https://www.python.org/downloads/"
            exit 1
        fi

        if [ -z "$PYTHON_CMD" ]; then
            fail "Python installation failed."
            exit 1
        fi
    fi
fi

# Verify Python version
PY_VERSION=$("$PYTHON_CMD" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ -z "$PY_VERSION" ]; then
    fail "Could not determine Python version."
    exit 1
fi

if ! version_ge "$PY_VERSION" "$MIN_PYTHON_VERSION"; then
    fail "Python $PY_VERSION found, but >= $MIN_PYTHON_VERSION required."
    echo "  Update Python: https://www.python.org/downloads/"
    exit 1
fi

ok "Python $PY_VERSION ($PYTHON_CMD)"

# Check pip is available
if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
    warn "pip not found."
    if [ "$OS" != "Darwin" ]; then
        if has_command apt-get; then
            echo "  Installing python3-pip..."
            if ask_yn "Install pip via apt now? (Y/n)" "Y"; then
                sudo apt-get update -qq && sudo apt-get install -y -qq python3-pip python3-venv
            else
                fail "pip is required. Install with: sudo apt-get install python3-pip"
                exit 1
            fi
        elif has_command dnf; then
            echo "  Installing python3-pip..."
            if ask_yn "Install pip via dnf now? (Y/n)" "Y"; then
                sudo dnf install -y -q python3-pip
            else
                fail "pip is required. Install with: sudo dnf install python3-pip"
                exit 1
            fi
        elif has_command yum; then
            echo "  Installing python3-pip..."
            if ask_yn "Install pip via yum now? (Y/n)" "Y"; then
                sudo yum install -y -q python3-pip
            else
                fail "pip is required. Install with: sudo yum install python3-pip"
                exit 1
            fi
        else
            fail "pip is required. Install it manually."
            exit 1
        fi
    else
        # macOS: pip should come with Python from brew
        fail "pip not found. Reinstall Python: brew install python@3.12"
        exit 1
    fi

    # Verify pip works now
    if ! "$PYTHON_CMD" -m pip --version >/dev/null 2>&1; then
        fail "pip still not available after install."
        exit 1
    fi
    ok "pip installed"
fi

# ============================================================
# Step 2: Check gcloud CLI
# ============================================================
step "2/5" "Checking gcloud CLI..."

if ! has_command gcloud; then
    fail "gcloud CLI not found."
    echo ""
    echo "  Install from: https://cloud.google.com/sdk/docs/install"
    echo ""

    if [ "$OS" = "Darwin" ] && has_command brew; then
        echo "  Or via Homebrew:"
        echo "    brew install --cask google-cloud-sdk"
        echo ""
        if ask_yn "Install gcloud via Homebrew now? (Y/n)" "Y"; then
            echo "  Installing gcloud CLI..."
            brew install --cask google-cloud-sdk
            # Source completion
            if [ -f "$(brew --prefix)/share/google-cloud-sdk/path.bash.inc" ]; then
                source "$(brew --prefix)/share/google-cloud-sdk/path.bash.inc"
            fi
            if ! has_command gcloud; then
                fail "gcloud installed but not found in PATH."
                echo "  Restart your terminal and run this script again."
                exit 1
            fi
        else
            exit 1
        fi
    elif [ "$OS" = "Linux" ]; then
        echo "  Quick install:"
        echo "    curl https://sdk.cloud.google.com | bash"
        exit 1
    else
        exit 1
    fi
fi

GCLOUD_VER=$(gcloud --version 2>/dev/null | head -1 | sed 's/Google Cloud SDK //')
ok "gcloud CLI $GCLOUD_VER"

# ============================================================
# Step 3: Install gce-rescue
# ============================================================
step "3/5" "Installing gce-rescue..."

INSTALL_DIR="$HOME/.gce-rescue"
BIN_DIR="$INSTALL_DIR/bin"
FILTER_NOISE='grep -v "^WARNING:" | grep -v "^ERROR: pip" | grep -v "requires protobuf" | grep -v "which is incompatible" | grep -v "^\[notice\]"'

# Check if already installed
INSTALLED=false
if [ -f "$BIN_DIR/gce-rescue" ]; then
    EXISTING_VER=$("$BIN_DIR/python" -m pip show gce-rescue 2>/dev/null | grep "^Version:" | awk '{print $2}') || true
    if [ -n "$EXISTING_VER" ]; then
        warn "gce-rescue $EXISTING_VER is already installed."
        if ask_yn "Reinstall/upgrade? (y/N)" "N"; then
            echo "  Upgrading..."
            "$BIN_DIR/pip" install --upgrade --force-reinstall "$REPO_URL" --quiet 2>&1 | eval "$FILTER_NOISE" || true
        else
            INSTALLED=true
        fi
    fi
fi

if [ "$INSTALLED" = false ]; then
    echo "  Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$INSTALL_DIR" 2>/dev/null
    if [ ! -f "$BIN_DIR/pip" ]; then
        fail "Failed to create virtual environment."
        echo "  You may need: sudo apt-get install python3-venv"
        echo "  Or: sudo dnf install python3-virtualenv"
        exit 1
    fi

    echo "  Downloading and installing from GitHub..."
    PIP_OUTPUT=$("$BIN_DIR/pip" install "$REPO_URL" 2>&1)
    PIP_EXIT=$?
    if [ $PIP_EXIT -ne 0 ]; then
        fail "Installation failed:"
        echo "$PIP_OUTPUT" | tail -20
        echo ""
        echo "  Try manually: $BIN_DIR/pip install $REPO_URL"
        exit 1
    fi
fi

# Get installed version
INSTALLED_VER=$("$BIN_DIR/python" -m pip show gce-rescue 2>/dev/null | grep "^Version:" | awk '{print $2}') || echo "unknown"
ok "gce-rescue $INSTALLED_VER installed"

# ============================================================
# Step 4: Configure PATH
# ============================================================
step "4/5" "Checking PATH..."

SCRIPTS_DIR="$BIN_DIR"

if has_command gce-rescue; then
    ok "gce-rescue is on PATH"
elif [ -d "$SCRIPTS_DIR" ]; then
    # Determine shell config file
    SHELL_NAME=$(basename "$SHELL" 2>/dev/null || echo "bash")
    case "$SHELL_NAME" in
        zsh)  SHELL_RC="$HOME/.zshrc" ;;
        bash) SHELL_RC="$HOME/.bashrc" ;;
        *)    SHELL_RC="$HOME/.profile" ;;
    esac

    # Check if already in PATH config
    if grep -q "$SCRIPTS_DIR" "$SHELL_RC" 2>/dev/null; then
        ok "Scripts directory already in $SHELL_RC"
    else
        warn "Adding $SCRIPTS_DIR to PATH in $SHELL_RC..."
        echo "" >> "$SHELL_RC"
        echo "# Added by gce-rescue installer" >> "$SHELL_RC"
        echo "export PATH=\"\$PATH:$SCRIPTS_DIR\"" >> "$SHELL_RC"
        export PATH="$PATH:$SCRIPTS_DIR"
        ok "PATH updated in $SHELL_RC"
    fi

    # Verify
    if ! has_command gce-rescue; then
        warn "gce-rescue will be available after restarting your terminal."
        echo "  Or run: source $SHELL_RC"
    fi
else
    warn "Could not find gce-rescue binary."
fi

# ============================================================
# Step 5: Authentication
# ============================================================
step "5/5" "Checking authentication..."

# Check active gcloud account
ACCOUNT=$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null) || true
if [ -n "$ACCOUNT" ]; then
    ok "gcloud account: $ACCOUNT"
else
    warn "No active gcloud account."
    if ask_yn "Run 'gcloud auth login' now? (Y/n)" "Y"; then
        gcloud auth login
        ACCOUNT=$(gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>/dev/null) || true
        if [ -z "$ACCOUNT" ]; then
            fail "Authentication failed."
            exit 1
        fi
        ok "gcloud account: $ACCOUNT"
    else
        fail "gcloud authentication required. Run: gcloud auth login"
        exit 1
    fi
fi

# Check project
PROJECT=$(gcloud config get-value project 2>/dev/null) || true
if [ -n "$PROJECT" ] && [ "$PROJECT" != "(unset)" ]; then
    ok "Project: $PROJECT"
else
    warn "No default project set."
    if [ -t 0 ] || [ -e /dev/tty ]; then
        printf "  Enter your GCP project ID: "
        read -r PROJECT_INPUT < /dev/tty
        if [ -n "$PROJECT_INPUT" ]; then
            gcloud config set project "$PROJECT_INPUT" 2>/dev/null
            PROJECT="$PROJECT_INPUT"
            ok "Project: $PROJECT"
        fi
    else
        echo "  Set project with: gcloud config set project PROJECT_ID"
    fi
fi

# Check Application Default Credentials
# ADC is found via: 1) GOOGLE_APPLICATION_CREDENTIALS env var,
# 2) ADC file, or 3) GCE metadata server (on GCP VMs)
ADC_PATH="${HOME}/.config/gcloud/application_default_credentials.json"

if [ -n "${GOOGLE_APPLICATION_CREDENTIALS:-}" ]; then
    if [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
        ok "Service account credentials: $GOOGLE_APPLICATION_CREDENTIALS"
    else
        warn "GOOGLE_APPLICATION_CREDENTIALS is set but file not found:"
        echo "  $GOOGLE_APPLICATION_CREDENTIALS"
    fi
elif [ -f "$ADC_PATH" ]; then
    ok "Application Default Credentials found"
else
    # Check if running on GCE (metadata server available)
    ON_GCE=false
    GCE_SA=$(curl -sf -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email" \
        --connect-timeout 2 2>/dev/null) || true
    if [ -n "$GCE_SA" ]; then
        ok "Running on GCE, using VM service account: $GCE_SA"
        ON_GCE=true
    fi

    if [ "$ON_GCE" = false ]; then
        warn "No credentials found for gce-rescue."
        echo "  gce-rescue needs one of these to authenticate:"
        echo "    1. Service account key (GOOGLE_APPLICATION_CREDENTIALS)"
        echo "    2. Application Default Credentials (gcloud auth application-default login)"
        echo "    3. GCE VM service account (automatic on GCP VMs)"
        echo ""
        if ask_yn "Run 'gcloud auth application-default login' now? (Y/n)" "Y"; then
            gcloud auth application-default login
            if [ -f "$ADC_PATH" ]; then
                ok "ADC configured"
            else
                warn "ADC setup may have failed. Try again later:"
                echo "  gcloud auth application-default login"
            fi
        else
            warn "Set up credentials before using gce-rescue."
        fi
    fi
fi

# ============================================================
# Done!
# ============================================================
echo ""
echo "=== Installation complete! ==="
echo ""

# Check if gce-rescue is usable in current session
if has_command gce-rescue; then
    echo "Quick start:"
    echo "  gce-rescue diagnose VM_NAME --zone=ZONE"
    echo "  gce-rescue repair VM_NAME --zone=ZONE"
    echo "  gce-rescue rescue VM_NAME --zone=ZONE"
    echo "  gce-rescue restore VM_NAME --zone=ZONE"
else
    echo "Run this to activate gce-rescue in your current terminal:"
    echo ""
    echo "  source ~/.bashrc && gce-rescue --help"
    echo ""
    echo "Quick start:"
    echo "  gce-rescue diagnose VM_NAME --zone=ZONE"
    echo "  gce-rescue repair VM_NAME --zone=ZONE"
    echo "  gce-rescue rescue VM_NAME --zone=ZONE"
    echo "  gce-rescue restore VM_NAME --zone=ZONE"
fi
echo ""
echo "Documentation: https://github.com/GoogleCloudPlatform/gce-rescue"
echo ""

# If PATH was updated, restart the shell so gce-rescue is immediately usable
if ! has_command gce-rescue && [ -n "$SCRIPTS_DIR" ]; then
    echo "Restarting shell to activate PATH..."
    exec bash
fi
