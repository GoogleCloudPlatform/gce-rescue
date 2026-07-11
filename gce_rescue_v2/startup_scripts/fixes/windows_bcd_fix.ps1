# GCE Rescue - Windows BCD repair fix script (for use with: repair --fix-script)
#
# Rebuilds the Boot Configuration Data (BCD) store and boot files of an
# OFFLINE Windows installation - the fix for boot failures like
#   0xc000000e / 0xc0000225 / 0xc000000f "Windows Boot Manager ... a required
#   device isn't connected", "Boot Configuration Data ... missing/corrupt".
#
# This is NOT a diagnosis-driven auto-repair category (Windows auto-repair is
# not wired into the orchestrator). It is a self-contained script an engineer
# passes explicitly:
#   gce-rescue repair VM --zone=ZONE --project=PROJECT \
#       --fix-script=gce_rescue_v2/startup_scripts/fixes/windows_bcd_fix.ps1
#
# The repair flow rescues the VM (mounting the affected disk at a drive letter
# via rescue_mount_windows.ps1), runs THIS script, then restores. Write-Log,
# and the affected Windows volume mounted read-write, are already available.
#
# Contract (like the Linux fix scripts): emit GCE-REPAIR-LINE for each action
# and exactly ONE GCE-REPAIR-RESULT (SUCCESS:<n> | NO_ISSUES:0 | FAILED:<reason>)
# on every path. Never call 'exit' - it would abort the composed startup script
# before the completion marker. A single result-emission block at the end.
#
# Mechanism: bcdboot rebuilds the BCD + boot files on the EFI System Partition
# (or the BIOS system partition) from the offline install's \Windows. It is the
# supported offline equivalent of bootrec /rebuildbcd, which only runs against
# the live system. bcdboot never touches user data; the pre-rescue snapshot is
# the rollback.

$FixReason = ""
$Fixes = 0

# --- Locate the affected Windows volume (the drive the rescue mount attached).
# The rescue mount script assigns D: (or the next free letter) to the affected
# disk's large partition. The Windows install is the one carrying \Windows.
$winVolume = $null
foreach ($vol in Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveLetter -ne 'C' }) {
    $winPath = "$($vol.DriveLetter):\Windows\System32\config\SYSTEM"
    if (Test-Path $winPath) {
        $winVolume = $vol
        break
    }
}

if (-not $winVolume) {
    $FixReason = "no offline Windows installation found on the attached disk (no <drive>:\Windows\System32\config\SYSTEM)"
} else {
    $winLetter = $winVolume.DriveLetter
    Write-Log "GCE-REPAIR-LINE:[INFO] bcd: found offline Windows at ${winLetter}:\Windows"

    # --- Identify the affected disk number from the Windows partition, so we
    # look for its OWN system partition (never the rescue disk's).
    $winPartition = Get-Partition -DriveLetter $winLetter -ErrorAction SilentlyContinue
    $diskNumber = $winPartition.DiskNumber

    # --- Detect firmware: an EFI System Partition on the affected disk means
    # UEFI; otherwise treat as BIOS/MBR.
    $espGuid = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    $esp = Get-Partition -DiskNumber $diskNumber -ErrorAction SilentlyContinue |
        Where-Object { $_.GptType -eq $espGuid } | Select-Object -First 1

    $sysLetter = $null
    $assignedAccessPath = $false
    if ($esp) {
        # Ensure the ESP has an access path so bcdboot can write to it. ESPs are
        # small (~100MB) and normally get no drive letter, so assign a temp one.
        $sysLetter = ($esp.DriveLetter)
        if (-not $sysLetter) {
            foreach ($letter in 'S', 'T', 'U', 'V', 'W') {
                $used = (Get-Partition | Where-Object { $_.DriveLetter }).DriveLetter
                if ($letter -notin $used) {
                    try {
                        Set-Partition -DiskNumber $diskNumber -PartitionNumber $esp.PartitionNumber `
                            -NewDriveLetter $letter -ErrorAction Stop
                        $sysLetter = $letter
                        $assignedAccessPath = $true
                        Write-Log "GCE-REPAIR-LINE:[INFO] bcd: mounted EFI system partition at ${letter}:"
                    } catch {
                        Write-Log "  WARNING: could not assign a letter to the ESP: $_"
                    }
                    break
                }
            }
        }
    }

    # --- Rebuild the BCD + boot files with bcdboot from the offline install.
    if ($sysLetter) {
        # UEFI: write to the ESP with the UEFI firmware target.
        $bcdCmd = "bcdboot ${winLetter}:\Windows /s ${sysLetter}: /f UEFI"
    } else {
        # BIOS/MBR: no ESP; bcdboot writes to the active system partition and
        # updates the BIOS boot code. /f BIOS targets legacy firmware.
        $bcdCmd = "bcdboot ${winLetter}:\Windows /f BIOS"
    }
    Write-Log "GCE-REPAIR-LINE:[INFO] bcd: running $bcdCmd"

    try {
        $output = & cmd.exe /c "$bcdCmd 2>&1"
        $rc = $LASTEXITCODE
        Write-Log "  bcdboot output: $output"
        if ($rc -eq 0) {
            $Fixes = 1
            Write-Log "GCE-REPAIR-LINE:[FIXED] bcd: Rebuilt the Boot Configuration Data for ${winLetter}:\Windows"
        } else {
            $FixReason = "bcdboot failed (exit $rc): $output"
        }
    } catch {
        $FixReason = "bcdboot threw: $_"
    }

    # --- Release the temp ESP letter so restore is clean.
    if ($assignedAccessPath -and $sysLetter) {
        try {
            Remove-PartitionAccessPath -DiskNumber $diskNumber `
                -PartitionNumber $esp.PartitionNumber `
                -AccessPath "${sysLetter}:\" -ErrorAction Stop
            Write-Log "  Released temporary ESP letter ${sysLetter}:"
        } catch {
            Write-Log "  WARNING: could not release ESP letter ${sysLetter}: $_"
        }
    }
}

# --- Single result-emission point (no 'exit' anywhere above) -----------------
if ($FixReason -ne "") {
    Write-Log "GCE-REPAIR-RESULT:FAILED:$FixReason"
} elseif ($Fixes -gt 0) {
    Write-Log "GCE-REPAIR-RESULT:SUCCESS:$Fixes"
} else {
    Write-Log "GCE-REPAIR-RESULT:NO_ISSUES:0"
}
