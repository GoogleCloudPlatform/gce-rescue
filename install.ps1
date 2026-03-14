<#
.SYNOPSIS
    GCE Rescue installer for Windows.

.DESCRIPTION
    Checks prerequisites, installs gce-rescue, configures PATH,
    and sets up authentication. Designed for quick setup during P1 incidents.

.EXAMPLE
    irm https://raw.githubusercontent.com/GoogleCloudPlatform/gce-rescue/main/install.ps1 | iex
#>

# Wrap in a function so 'return' works safely with iex
& {
    $ErrorActionPreference = "Continue"

    # --- Configuration ---
    $REPO_URL = "https://github.com/gokulr94/gce-rescue/archive/refs/heads/v2-beta.zip"
    $MIN_PYTHON_VERSION = [version]"3.9"

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

    function Get-PythonCommand {
        # Try common Python command names
        # Skip Windows Store aliases (AppExecLink) that open Microsoft Store
        foreach ($cmd in @("python", "python3", "py")) {
            $found = Get-Command $cmd -ErrorAction SilentlyContinue
            if ($found) {
                # Skip Microsoft Store app aliases
                if ($found.Source -like "*\WindowsApps\*") { continue }
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

    # Check if running as Administrator
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $isAdmin) {
        Write-Fail "Administrator privileges required."
        Write-Host ""
        Write-Host "  Right-click PowerShell > 'Run as administrator', then run:" -ForegroundColor White
        Write-Host "    irm https://raw.githubusercontent.com/gokulr94/gce-rescue/v2-beta/install.ps1 | iex" -ForegroundColor Yellow
        return
    }

    # ============================================================
    # Step 1: Check Python
    # ============================================================
    Write-Step "1/5" "Checking Python..."

    $pythonCmd = Get-PythonCommand

    if (-not $pythonCmd) {
        Write-Fail "Python not found."
        Write-Host ""
        $install = Read-Host "  Install Python 3.12 now? (Y/n)"
        if ($install -ne "n" -and $install -ne "N") {
            $pyInstalled = $false
            $pyUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
            $pyInstaller = "$env:TEMP\python-installer.exe"
            try {
                Write-Host "  Downloading Python 3.12..." -ForegroundColor Cyan
                Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing
                Write-Host "  Installing Python (this may take a minute)..." -ForegroundColor Cyan
                Start-Process $pyInstaller -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1" -Wait
                Remove-Item $pyInstaller -ErrorAction SilentlyContinue

                # Refresh PATH
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                $pythonCmd = Get-PythonCommand
                if ($pythonCmd) { $pyInstalled = $true }
            } catch {
                Write-Warn "Download failed: $_"
            }

            if (-not $pyInstalled) {
                Write-Fail "Python installation failed."
                Write-Host "  Download manually: https://www.python.org/downloads/"
                Write-Host "  IMPORTANT: Check 'Add Python to PATH' during installation."
                return
            }
        } else {
            Write-Host ""
            Write-Host "  Install Python manually:" -ForegroundColor White
            Write-Host "    https://www.python.org/downloads/" -ForegroundColor Yellow
            Write-Host "    IMPORTANT: Check 'Add Python to PATH' during installation."
            Write-Host ""
            Write-Host "  Then re-run:" -ForegroundColor White
            Write-Host "    irm https://raw.githubusercontent.com/gokulr94/gce-rescue/v2-beta/install.ps1 | iex" -ForegroundColor Yellow
            return
        }
    }

    # Verify Python version
    $verOutput = & $pythonCmd --version 2>&1
    if ($verOutput -match "Python (\d+\.\d+\.\d+)") {
        $pyVersion = [version]$Matches[1]
        if ($pyVersion -lt $MIN_PYTHON_VERSION) {
            Write-Fail "Python $pyVersion found, but >= $MIN_PYTHON_VERSION required."
            Write-Host "  Update Python: https://www.python.org/downloads/"
            return
        }
        Write-OK "Python $pyVersion ($pythonCmd)"
    } else {
        Write-Fail "Could not determine Python version."
        return
    }

    # ============================================================
    # Step 2: Check gcloud CLI
    # ============================================================
    Write-Step "2/5" "Checking gcloud CLI..."

    $hasGcloud = Get-Command "gcloud" -ErrorAction SilentlyContinue
    if (-not $hasGcloud) {
        Write-Fail "gcloud CLI not found."
        Write-Host ""
        $install = Read-Host "  Install gcloud CLI now? (Y/n)"
        if ($install -ne "n" -and $install -ne "N") {
            $gcloudUrl = "https://dl.google.com/dl/cloudsdk/channels/rapid/GoogleCloudSDKInstaller.exe"
            $gcloudInstaller = "$env:TEMP\gcloud-installer.exe"
            try {
                Write-Host "  Downloading gcloud CLI..." -ForegroundColor Cyan
                Invoke-WebRequest -Uri $gcloudUrl -OutFile $gcloudInstaller -UseBasicParsing
                Write-Host "  Installing gcloud CLI (this may take a few minutes)..." -ForegroundColor Cyan
                Start-Process $gcloudInstaller -ArgumentList "/S" -Wait
                Remove-Item $gcloudInstaller -ErrorAction SilentlyContinue

                # Refresh PATH
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                            [System.Environment]::GetEnvironmentVariable("Path", "User")
                $hasGcloud = Get-Command "gcloud" -ErrorAction SilentlyContinue
                if (-not $hasGcloud) {
                    Write-Warn "gcloud installed. Reopen PowerShell and re-run the installer."
                    return
                }
            } catch {
                Write-Fail "Download failed: $_"
                Write-Host "  Install manually: https://cloud.google.com/sdk/docs/install"
                return
            }
        } else {
            Write-Host ""
            Write-Host "  Install gcloud CLI manually:" -ForegroundColor White
            Write-Host "    https://cloud.google.com/sdk/docs/install" -ForegroundColor Yellow
            Write-Host ""
            Write-Host "  Then re-run:" -ForegroundColor White
            Write-Host "    irm https://raw.githubusercontent.com/gokulr94/gce-rescue/v2-beta/install.ps1 | iex" -ForegroundColor Yellow
            return
        }
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
            return
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
        $pyPath = & $pythonCmd -c "import sys; print(sys.executable)" 2>$null
        $scriptsDir = Join-Path (Split-Path $pyPath) "Scripts"
    }

    $hasGceRescue = Get-Command "gce-rescue" -ErrorAction SilentlyContinue
    if ($hasGceRescue) {
        Write-OK "gce-rescue is on PATH"
    } elseif ($scriptsDir -and (Test-Path $scriptsDir)) {
        $currentPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
        if ($currentPath -notlike "*$scriptsDir*") {
            Write-Warn "Adding $scriptsDir to PATH..."
            [System.Environment]::SetEnvironmentVariable(
                "Path", "$currentPath;$scriptsDir", "User"
            )
            $env:Path = "$env:Path;$scriptsDir"
            Write-OK "PATH updated (restart PowerShell to activate)"
        } else {
            Write-OK "Scripts directory already in PATH"
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
                return
            }
            Write-OK "gcloud account: $account"
        } else {
            Write-Fail "gcloud authentication required. Run: gcloud auth login"
            return
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
}
