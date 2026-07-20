# GCE Rescue Mode - Windows Startup Script
# This script runs on the Windows rescue VM to mount the affected disk
#
# Placeholders (replaced by Python code):
#   DISK_NAME_PLACEHOLDER - Name of the affected disk
#   PASSWORD_PLACEHOLDER - Pre-generated password for rescue_admin account

$ErrorActionPreference = "Continue"
$diskName = "DISK_NAME_PLACEHOLDER"
$logFile = "C:\gce-rescue.log"
$mountLetter = "D"
$rescueUser = "rescue_admin"
$rescuePassword = "PASSWORD_PLACEHOLDER"
$credentialsFile = "C:\rescue_credentials.txt"

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    Write-Host $logMessage
    Add-Content -Path $logFile -Value $logMessage
}

Write-Log "=== GCE Rescue Mode - Windows ==="
Write-Log "Affected disk: $diskName"
Write-Log "Target drive letter: $mountLetter`:"

# Create rescue admin account for RDP access
Write-Log ""
Write-Log "=== Creating Rescue Admin Account ==="
try {
    $securePassword = ConvertTo-SecureString $rescuePassword -AsPlainText -Force

    # Check if user already exists
    $userExists = Get-LocalUser -Name $rescueUser -ErrorAction SilentlyContinue

    if ($userExists) {
        # Update password if user exists
        Set-LocalUser -Name $rescueUser -Password $securePassword
        Write-Log "Updated password for existing user: $rescueUser"
    } else {
        # Create new user
        New-LocalUser -Name $rescueUser -Password $securePassword -FullName "GCE Rescue Admin" -Description "Temporary rescue mode admin account" -PasswordNeverExpires
        Write-Log "Created new user: $rescueUser"
    }

    # Add to Administrators group
    Add-LocalGroupMember -Group "Administrators" -Member $rescueUser -ErrorAction SilentlyContinue
    Write-Log "Added $rescueUser to Administrators group"

    # Save credentials to file (for reference inside the VM)
    $credContent = @"
========================================
GCE Rescue Mode - Login Credentials
========================================
Username: $rescueUser
Password: $rescuePassword

Use these credentials to connect via RDP.

This file will be deleted when you restore the VM.
========================================
"@
    $credContent | Out-File -FilePath $credentialsFile -Encoding UTF8
    Write-Log "Credentials saved to: $credentialsFile"
    Write-Log "Account setup complete for: $rescueUser"

} catch {
    Write-Log "ERROR creating rescue admin account: $_"
    Write-Log "You may need to use 'gcloud compute reset-windows-password' instead"
}

# Wait for disk to appear
Write-Log "Waiting for affected disk to be available..."
$maxAttempts = 60
$attempt = 0
$diskFound = $false

while ($attempt -lt $maxAttempts) {
    $attempt++

    # Get all disks except the boot disk (Disk 0)
    $disks = @(Get-Disk | Where-Object { $_.Number -ne 0 })

    if ($disks.Count -gt 0) {
        Write-Log "Found $($disks.Count) additional disk(s)"
        $diskFound = $true
        break
    }

    Write-Log "Waiting for disk... attempt $attempt/$maxAttempts"
    Start-Sleep -Seconds 5
}

if (-not $diskFound) {
    Write-Log "ERROR: Affected disk not found after $maxAttempts attempts"
    Write-Log "Please check if disk is properly attached"
    exit 1
}

# Disks whose GPT GUID Windows regenerated on online (signature collision
# with a same-image rescue disk) - warned about below. The orchestrator
# PREVENTS this by rescuing with a different image family (issue #126);
# this detection is the tripwire for cases prevention cannot cover
# (e.g. custom images cloned from the same base).
$guidChangedDisks = @()

# Process each non-boot disk
foreach ($disk in $disks) {
    Write-Log "Processing Disk $($disk.Number): $($disk.FriendlyName)"
    Write-Log "  Size: $([math]::Round($disk.Size / 1GB, 2)) GB"
    Write-Log "  Status: $($disk.OperationalStatus)"

    try {
        # Detect GPT GUID regeneration across the online operation (issue #126).
        # Disks built from the same GCE image share a GPT disk GUID. Bringing
        # the affected disk online next to a same-GUID rescue disk makes
        # Windows REGENERATE the affected disk's GUID, invalidating the device
        # references in that disk's own BCD - after restore the firmware halts
        # at 0xc000000e/0xc0000225. Restoring the old GUID does NOT work
        # (live-tested): the same-GUID rescue disk is still online, so the
        # collision immediately re-exists and Windows re-resolves it later.
        # Automatic BCD rewriting is deliberately NOT done either - it can
        # push BitLocker disks into recovery and drops custom BCD entries.
        # The orchestrator prevents the collision by selecting a different
        # rescue image family; if a change is detected anyway, WARN loudly
        # (see below) so the operator can repair consciously.
        $originalGuid = $null
        try {
            $originalGuid = (Get-Disk -Number $disk.Number -ErrorAction Stop).Guid
            Write-Log "  Affected disk GPT GUID (pre-online): $originalGuid"
        } catch {
            Write-Log "  WARNING: could not read disk GUID before online: $_"
        }

        # Bring disk online if offline
        if ($disk.OperationalStatus -eq 'Offline') {
            Write-Log "  Bringing disk online..."
            Set-Disk -Number $disk.Number -IsOffline $false
        }

        # Remove read-only if set
        if ($disk.IsReadOnly) {
            Write-Log "  Removing read-only flag..."
            Set-Disk -Number $disk.Number -IsReadOnly $false
        }

        if ($originalGuid) {
            try {
                $currentGuid = (Get-Disk -Number $disk.Number -ErrorAction Stop).Guid
                if ($currentGuid -ne $originalGuid) {
                    Write-Log "  WARNING: Disk GUID was regenerated by Windows ($originalGuid -> $currentGuid) - GPT signature collision (issue #126)"
                    $guidChangedDisks += $disk.Number
                } else {
                    Write-Log "  Disk GUID unchanged after online - no collision detected"
                }
            } catch {
                Write-Log "  WARNING: could not re-read disk GUID after online: $_"
            }
        }

        # Get partitions
        $partitions = @(Get-Partition -DiskNumber $disk.Number | Where-Object { $_.Type -ne 'Reserved' -and $_.Size -gt 1GB })

        if ($partitions.Count -gt 0) {
            Write-Log "  Found $($partitions.Count) partition(s)"

            foreach ($partition in $partitions) {
                Write-Log "    Partition $($partition.PartitionNumber): $([math]::Round($partition.Size / 1GB, 2)) GB"

                # Check if partition already has a drive letter
                if ($partition.DriveLetter) {
                    Write-Log "    Already mounted as $($partition.DriveLetter):"
                    continue
                }

                # Assign drive letter
                try {
                    # Find available drive letter starting from D
                    $usedLetters = (Get-Partition | Where-Object { $_.DriveLetter }).DriveLetter
                    $availableLetter = $null

                    foreach ($letter in 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N') {
                        if ($letter -notin $usedLetters) {
                            $availableLetter = $letter
                            break
                        }
                    }

                    if ($availableLetter) {
                        Write-Log "    Assigning drive letter $availableLetter`:"
                        Set-Partition -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -NewDriveLetter $availableLetter
                        Write-Log "    SUCCESS: Partition mounted at $availableLetter`:"
                    } else {
                        Write-Log "    WARNING: No available drive letters"
                    }
                } catch {
                    Write-Log "    ERROR: Failed to assign drive letter: $_"
                }
            }
        } else {
            Write-Log "  No mountable partitions found"
        }

    } catch {
        Write-Log "  ERROR processing disk: $_"
    }
}

# GUID-collision warning (issue #126). The disk's BCD still references its
# OLD identity, so the VM will likely fail at 0xc000000e/0xc0000225 after
# restore. The script does NOT rewrite the BCD automatically - on BitLocker
# disks that can trigger recovery, and it would drop custom BCD entries.
# The operator decides, with the snapshot as the safety net.
foreach ($diskNumber in $guidChangedDisks) {
    Write-Log ""
    Write-Log "############################################################"
    Write-Log "WARNING: GPT disk GUID collision detected on Disk $diskNumber (issue #126)"
    Write-Log "Windows regenerated this disk's GUID; its BCD still references"
    Write-Log "the old GUID, so the VM may NOT BOOT after restore"
    Write-Log "(0xc000000e / 0xc0000225)."
    Write-Log ""
    Write-Log "Before restoring, repair the BCD from this rescue session:"
    Write-Log "  bcdboot <windows-drive>:\Windows"
    Write-Log "  (NOTE: rebuilds a default BCD - custom entries are lost;"
    Write-Log "  on BitLocker-protected disks this can trigger recovery."
    Write-Log "  A bundled fix script for this is tracked in issue #150.)"
    Write-Log "############################################################"
}

# Summary
Write-Log ""
Write-Log "=== Mount Summary ==="
$mountedDrives = Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveLetter -ne 'C' } |
    Select-Object DriveLetter, FileSystemLabel, @{N='SizeGB';E={[math]::Round($_.Size/1GB,2)}}, FileSystem

foreach ($drive in $mountedDrives) {
    Write-Log "  $($drive.DriveLetter): - $($drive.FileSystemLabel) - $($drive.SizeGB) GB ($($drive.FileSystem))"
}

# Output completion marker to serial console FIRST (for orchestrator verification)
# This must happen before any operations that might fail (like desktop file creation)
Write-Log ""
Write-Log "GCE-RESCUE-COMPLETE"
# Reliable completion signal: set a guest attribute the orchestrator polls.
# The serial console can drop the script's final output burst before the
# process exits, so the marker above is best-effort; this is deterministic.
try {
    Invoke-RestMethod -Method PUT -Headers @{'Metadata-Flavor' = 'Google'} `
        -Uri 'http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/gce-rescue/status' `
        -Body 'COMPLETE' -TimeoutSec 10 -ErrorAction SilentlyContinue | Out-Null
    Write-Log "Set guest attribute gce-rescue/status=COMPLETE"
} catch {
    Write-Log "WARNING: could not set completion guest attribute: $_"
}
Write-Log "=== Startup script completed successfully ==="

Write-Log ""
Write-Log "=== GCE Rescue Ready ==="
Write-Log "Connect via RDP and access your affected disk at the mounted drive letter(s)"
Write-Log ""
Write-Log "=========================================="
Write-Log "RDP CONNECTION CREDENTIALS"
Write-Log "=========================================="
Write-Log "Username: $rescueUser"
Write-Log "Password: $rescuePassword"
Write-Log "=========================================="
Write-Log ""
Write-Log "Common repair tasks:"
Write-Log "  - Edit files: notepad D:\Windows\System32\config\SOFTWARE"
Write-Log "  - Registry: Load hive from D:\Windows\System32\config\ in regedit"
Write-Log "  - Boot repair: bcdboot D:\Windows /s C:"
Write-Log ""

# Create desktop shortcut with instructions (optional - may fail if running as SYSTEM)
try {
    $desktopPath = [Environment]::GetFolderPath("CommonDesktopDirectory")
    if (-not $desktopPath) {
        $desktopPath = "C:\Users\Public\Desktop"
    }
    $shortcutContent = @"
GCE Rescue Mode - Instructions
==============================

Your affected disk has been mounted. Look for additional drive letters (D:, E:, etc.)

Common Tasks:
1. Browse files: Open File Explorer and navigate to D:\
2. Edit config: notepad D:\path\to\config\file
3. Fix registry:
   - Open regedit
   - Select HKEY_LOCAL_MACHINE
   - File > Load Hive
   - Browse to D:\Windows\System32\config\SYSTEM (or SOFTWARE, SAM, etc.)
   - Give it a name like "OFFLINE_SYSTEM"
   - Make changes
   - File > Unload Hive when done

4. Fix boot: Open CMD as Admin and run:
   bcdboot D:\Windows /s C:

When done, run restore command from your local machine:
  python -m gce_rescue_v2.cli restore VM_NAME --zone ZONE
"@

    $shortcutContent | Out-File -FilePath "$desktopPath\GCE-Rescue-Instructions.txt" -Encoding UTF8
    Write-Log "Instructions saved to Desktop: $desktopPath\GCE-Rescue-Instructions.txt"
} catch {
    Write-Log "WARNING: Could not create desktop instructions file: $_"
    Write-Log "This is non-critical - rescue mode is still operational"
}
