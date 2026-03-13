# GCE Rescue installer (Windows PowerShell)
#
# Usage:
#   irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/v2-beta/install.ps1 | iex
#
# For Linux/macOS, use install.sh instead.

$ErrorActionPreference = "Stop"
$Branch = "v2-beta"
$Archive = "https://github.com/GoogleCloudPlatform/gce-rescue/archive/${Branch}.zip"
$MinPythonMinor = 9

function Write-Info  { param($msg) Write-Host "  $msg" }
function Write-Ok    { param($msg) Write-Host "  [OK] $msg" -ForegroundColor Green }
function Write-Fail  { param($msg) Write-Host "  [FAILED] $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "GCE Rescue - Installer"
Write-Host "======================"
Write-Host ""

# --- Step 1: Find Python >= 3.9 ---
$Python = $null
foreach ($cmd in @("python3", "python", "py -3")) {
    try {
        $ver = & $cmd.Split()[0] $cmd.Split()[1..99] -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge $MinPythonMinor) {
                $Python = $cmd
                break
            }
        }
    } catch {}
}

if (-not $Python) {
    Write-Info "Python >= 3.$MinPythonMinor not found. Attempting to install..."

    # Try winget
    $hasWinget = Get-Command winget -ErrorAction SilentlyContinue
    if ($hasWinget) {
        winget install Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements 2>$null
        # Refresh PATH
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    }

    # Re-check
    foreach ($cmd in @("python3", "python", "py -3")) {
        try {
            $ver = & $cmd.Split()[0] $cmd.Split()[1..99] -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($ver) {
                $parts = $ver.Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge $MinPythonMinor) {
                    $Python = $cmd
                    break
                }
            }
        } catch {}
    }

    if (-not $Python) {
        Write-Fail "Python >= 3.$MinPythonMinor required. Download from https://www.python.org/downloads/"
    }
}

$pyVer = & $Python.Split()[0] $Python.Split()[1..99] --version 2>&1
Write-Ok "Python: $pyVer"

# --- Step 2: Ensure pip ---
$pipCheck = & $Python.Split()[0] $Python.Split()[1..99] -m pip --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Info "Installing pip..."
    & $Python.Split()[0] $Python.Split()[1..99] -m ensurepip --upgrade 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Cannot install pip. Run: $Python -m ensurepip"
    }
}
$pipVer = & $Python.Split()[0] $Python.Split()[1..99] -m pip --version 2>&1
Write-Ok "pip: $(($pipVer -split ' ')[1])"

# --- Step 3: Install gce-rescue ---
Write-Info "Installing gce-rescue..."
& $Python.Split()[0] $Python.Split()[1..99] -m pip install --quiet --upgrade $Archive 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python.Split()[0] $Python.Split()[1..99] -m pip install --quiet --upgrade $Archive --user 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "pip install failed. Check network connectivity and try again."
    }
}

# --- Step 4: Verify ---
$gceRescue = Get-Command gce-rescue -ErrorAction SilentlyContinue
if ($gceRescue) {
    $ver = & gce-rescue --version 2>&1 | Select-Object -First 1
    Write-Ok "gce-rescue $ver"
} else {
    $modCheck = & $Python.Split()[0] $Python.Split()[1..99] -m gce_rescue_v2.cli --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "gce-rescue installed (not in PATH)"
        Write-Host ""
        Write-Info "Run directly:"
        Write-Info "  $Python -m gce_rescue_v2.cli --help"
        Write-Host ""
        Write-Info "Or add Python Scripts to PATH:"
        $scriptsDir = & $Python.Split()[0] $Python.Split()[1..99] -c "import sysconfig; print(sysconfig.get_path('scripts'))" 2>$null
        Write-Info "  `$env:Path += ';$scriptsDir'"
        Write-Host ""
        exit 0
    } else {
        Write-Fail "Installation verification failed."
    }
}

# --- Done ---
Write-Host ""
Write-Info "Usage:"
Write-Info "  gce-rescue diagnose VM_NAME --zone=ZONE"
Write-Info "  gce-rescue repair   VM_NAME --zone=ZONE"
Write-Info "  gce-rescue rescue   VM_NAME --zone=ZONE"
Write-Info "  gce-rescue restore  VM_NAME --zone=ZONE"
Write-Host ""
Write-Info "Run 'gce-rescue --help' for more options."
Write-Host ""
