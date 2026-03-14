#Requires -Version 5.1
<#
.SYNOPSIS
    GCE Rescue installer for Windows.

.DESCRIPTION
    Checks prerequisites, installs gce-rescue, configures PATH,
    and sets up authentication. Designed for quick setup during P1 incidents.

.EXAMPLE
    irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.ps1 | iex
#>

$ErrorActionPreference = "Continue"

# --- Configuration ---
$REPO_URL = "https://github.com/gokulr94/gce-rescue/archive/refs/heads/v2-beta.zip"
$MIN_PYTHON_VERSION = [version]"3.9"
$PACKAGE_NAME = "gce-rescue"

# --- Helper functions ---

function Write-Step {
    param([string]$Step, [string]$Message)
    Write-Host "`n[$Step] $Message" -ForegroundColor Cyan
}

function Write-OK {
    param([string]$Message)
    Write-Host "  $Message" -ForegroundColor Green
}

function Write-Warn {
    param([string]$Message)
    Write-Host "  $Message" -ForegroundColor Yellow
}

function Write-Fail {
    param([string]$Message)
    Write-Host "  $Message" -ForegroundColor Red
}

function Test-Command {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

function Get-PythonCommand {
    # Try common Python command names, return the first that works
    foreach ($cmd in @("python", "python3", "py")) {
        if (Test-Command $cmd) {
            try {
                $ver = & $cmd --version 2>&1
                if ($ver -match "Python (\d+\.\d+)") {
                    return $cmd
                }
            } catch {}
        }
    }
    return $null
}

# --- Main ---

Write-Host ""
Write-Host "=== GCE Rescue Installer ===" -ForegroundColor White
Write-Host "Sets up gce-rescue and all dependencies on this machine."
Write-Host ""

# ============================================================
# Step 1: Check Python
# ============================================================
Write-Step "1/5" "Checking Python..."

$pythonCmd = Get-PythonCommand

if (-not $pythonCmd) {
    Write-Fail "Python not found."
    Write-Host ""
    Write-Host "  Install Python using one of these methods:" -ForegroundColor White
    Write-Host ""

    Write-Host "  Install Python first, then re-run this installer:" -ForegroundColor White
    Write-Host ""
    if (Test-Command "winget") {
        Write-Host "    winget install Python.Python.3.12" -ForegroundColor Yellow
    } else {
        Write-Host "    Download from: https://www.python.org/downloads/"
        Write-Host "    IMPORTANT: Check 'Add Python to PATH' during installation."
    }
    Write-Host ""
    Write-Host "  After installing, open a new PowerShell window and run:" -ForegroundColor White
    Write-Host "    irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.ps1 | iex" -ForegroundColor Yellow
    exit 1
}

# Verify Python version
$verOutput = & $pythonCmd --version 2>&1
if ($verOutput -match "Python (\d+\.\d+\.\d+)") {
    $pyVersion = [version]$Matches[1]
    if ($pyVersion -lt $MIN_PYTHON_VERSION) {
        Write-Fail "Python $pyVersion found, but >= $MIN_PYTHON_VERSION required."
        Write-Host "  Update Python: https://www.python.org/downloads/"
        exit 1
    }
    Write-OK "Python $pyVersion ($pythonCmd)"
} else {
    Write-Fail "Could not determine Python version."
    exit 1
}

# ============================================================
# Step 2: Check gcloud CLI
# ============================================================
Write-Step "2/5" "Checking gcloud CLI..."

if (-not (Test-Command "gcloud")) {
    Write-Fail "gcloud CLI not found."
    Write-Host ""

    Write-Host "  Install gcloud CLI first, then re-run this installer:" -ForegroundColor White
    Write-Host ""
    if (Test-Command "winget") {
        Write-Host "    winget install Google.CloudSDK" -ForegroundColor Yellow
    } else {
        Write-Host "    https://cloud.google.com/sdk/docs/install"
    }
    Write-Host ""
    Write-Host "  After installing, open a new PowerShell window and run:" -ForegroundColor White
    Write-Host "    irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.ps1 | iex" -ForegroundColor Yellow
    exit 1
}

try {
    $gcloudVer = (& gcloud --version 2>&1 | Select-Object -First 1) -replace "Google Cloud SDK ", ""
} catch {
    $gcloudVer = "installed"
}
Write-OK "gcloud CLI $gcloudVer"

# ============================================================
# Step 3: Install gce-rescue
# ============================================================
Write-Step "3/5" "Installing gce-rescue..."

# Check if already installed
$installed = $false
try {
    $existingVer = & $pythonCmd -m pip show gce-rescue 2>$null | Select-String "Version:"
    if ($existingVer) {
        $ver = ($existingVer -split ": ")[1]
        Write-Warn "gce-rescue $ver is already installed."
        $upgrade = Read-Host "  Reinstall/upgrade? (y/N)"
        if ($upgrade -eq "y" -or $upgrade -eq "Y") {
            Write-Host "  Upgrading..." -ForegroundColor Cyan
            & $pythonCmd -m pip install --upgrade --force-reinstall $REPO_URL --quiet 2>&1 | Out-Null
        } else {
            $installed = $true
        }
    }
} catch {}

if (-not $installed) {
    Write-Host "  Downloading and installing from GitHub..." -ForegroundColor Cyan
    $pipOutput = & $pythonCmd -m pip install $REPO_URL --quiet 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Installation failed:"
        Write-Host $pipOutput
        exit 1
    }
}

# Get installed version
$verInfo = & $pythonCmd -m pip show gce-rescue 2>$null | Select-String "Version:"
$installedVer = if ($verInfo) { ($verInfo -split ": ")[1] } else { "unknown" }
Write-OK "gce-rescue $installedVer installed"

# ============================================================
# Step 4: Configure PATH
# ============================================================
Write-Step "4/5" "Checking PATH..."

# Find Python Scripts directory
$scriptsDir = & $pythonCmd -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>$null
if (-not $scriptsDir) {
    # Fallback: derive from Python executable path
    $pyPath = & $pythonCmd -c "import sys; print(sys.executable)" 2>$null
    $scriptsDir = Join-Path (Split-Path $pyPath) "Scripts"
}

# Check if gce-rescue is already accessible
if (Test-Command "gce-rescue") {
    Write-OK "gce-rescue is on PATH"
} elseif ($scriptsDir -and (Test-Path $scriptsDir)) {
    # Add to user PATH
    $currentPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    if ($currentPath -notlike "*$scriptsDir*") {
        Write-Warn "Adding $scriptsDir to PATH..."
        [System.Environment]::SetEnvironmentVariable(
            "Path", "$currentPath;$scriptsDir", "User"
        )
        $env:Path = "$env:Path;$scriptsDir"
        Write-OK "PATH updated"
    } else {
        Write-OK "Scripts directory already in PATH"
    }

    # Verify
    if (-not (Test-Command "gce-rescue")) {
        Write-Warn "gce-rescue will be available after reopening PowerShell."
        Write-Host "  Or run directly: $scriptsDir\gce-rescue.exe"
    }
} else {
    Write-Warn "Could not find Python Scripts directory."
    Write-Host "  You may need to add it to PATH manually."
}

# ============================================================
# Step 5: Authentication
# ============================================================
Write-Step "5/5" "Checking authentication..."

# Check active gcloud account
$account = & gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>$null
if ($account) {
    Write-OK "gcloud account: $account"
} else {
    Write-Warn "No active gcloud account."
    $login = Read-Host "  Run 'gcloud auth login' now? (Y/n)"
    if ($login -ne "n" -and $login -ne "N") {
        & gcloud auth login
        $account = & gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>$null
        if (-not $account) {
            Write-Fail "Authentication failed."
            exit 1
        }
        Write-OK "gcloud account: $account"
    } else {
        Write-Fail "gcloud authentication required. Run: gcloud auth login"
        exit 1
    }
}

# Check project
$project = & gcloud config get-value project 2>$null
if ($project -and $project -ne "(unset)") {
    Write-OK "Project: $project"
} else {
    Write-Warn "No default project set."
    $proj = Read-Host "  Enter your GCP project ID"
    if ($proj) {
        & gcloud config set project $proj
        $project = $proj
        Write-OK "Project: $project"
    }
}

# Check Application Default Credentials
# ADC is found via: 1) GOOGLE_APPLICATION_CREDENTIALS env var,
# 2) ADC file, or 3) GCE metadata server (on GCP VMs)
$adcPath = Join-Path $env:APPDATA "gcloud\application_default_credentials.json"
if ($env:GOOGLE_APPLICATION_CREDENTIALS) {
    if (Test-Path $env:GOOGLE_APPLICATION_CREDENTIALS) {
        Write-OK "Service account credentials: $env:GOOGLE_APPLICATION_CREDENTIALS"
    } else {
        Write-Warn "GOOGLE_APPLICATION_CREDENTIALS is set but file not found:"
        Write-Host "  $env:GOOGLE_APPLICATION_CREDENTIALS"
    }
} elseif (Test-Path $adcPath) {
    Write-OK "Application Default Credentials found"
} else {
    # Check if running on GCE (metadata server available)
    $onGCE = $false
    try {
        $meta = Invoke-RestMethod -Uri "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email" `
            -Headers @{"Metadata-Flavor"="Google"} -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($meta) {
            Write-OK "Running on GCE, using VM service account: $meta"
            $onGCE = $true
        }
    } catch {}

    if (-not $onGCE) {
        Write-Warn "No credentials found for gce-rescue."
        Write-Host "  gce-rescue needs one of these to authenticate:"
        Write-Host "    1. Service account key (GOOGLE_APPLICATION_CREDENTIALS)"
        Write-Host "    2. Application Default Credentials (gcloud auth application-default login)"
        Write-Host "    3. GCE VM service account (automatic on GCP VMs)"
        Write-Host ""
        $setupAdc = Read-Host "  Run 'gcloud auth application-default login' now? (Y/n)"
        if ($setupAdc -ne "n" -and $setupAdc -ne "N") {
            & gcloud auth application-default login
            if (Test-Path $adcPath) {
                Write-OK "ADC configured"
            } else {
                Write-Warn "ADC setup may have failed. Try again later:"
                Write-Host "  gcloud auth application-default login"
            }
        } else {
            Write-Warn "Set up credentials before using gce-rescue."
        }
    }
}

# ============================================================
# Done!
# ============================================================
Write-Host ""
Write-Host "=== Installation complete! ===" -ForegroundColor Green
Write-Host ""
Write-Host "Quick start:" -ForegroundColor White
Write-Host "  gce-rescue diagnose VM_NAME --zone=ZONE"
Write-Host "  gce-rescue repair VM_NAME --zone=ZONE"
Write-Host "  gce-rescue rescue VM_NAME --zone=ZONE"
Write-Host "  gce-rescue restore VM_NAME --zone=ZONE"
Write-Host ""
Write-Host "Documentation: https://github.com/GoogleCloudPlatform/gce-rescue"
Write-Host ""
