# GCE Rescue - Windows BCD repair fix script (for use with: repair --fix-script)
#
# Rebuilds the Boot Configuration Data (BCD) store and boot files of an
# offline Windows installation via bcdboot.exe.
#
# Scope notes:
# - bcdboot writes a fresh DEFAULT store: custom BCD entries (extra boot
#   entries, debug/testsigning flags, custom timeouts) are not preserved.
# - BitLocker-locked OS volumes are detected and refused with unlock guidance.
# - Contract: emit GCE-REPAIR-LINE progress markers, emit strictly ONE
#   GCE-REPAIR-RESULT at the end, and never call 'exit'.

# Fallback so the script also works if tested standalone in an RDP session
if (-not (Get-Command Write-Log -ErrorAction SilentlyContinue)) {
    function Write-Log([string]$Message) { Write-Output $Message }
}

function Get-FreeDriveLetter {
    $usedLetters = @(
        (Get-Partition -ErrorAction SilentlyContinue | Where-Object { $_.DriveLetter }).DriveLetter
        (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue).Name
    ) | Where-Object { $_ } | ForEach-Object { ([string]$_).ToUpper() } | Select-Object -Unique

    foreach ($code in ([int][char]'D'..[int][char]'Z')) {
        $letter = [string][char]$code
        if ($letter -notin $usedLetters) {
            return $letter
        }
    }
    return $null
}

$FixReason = ""
$Fixes = 0
$bootLetter = $null
$bootDrive = $null
$assignedBootLetter = $false

# 1. Locate the offline Windows OS volume & partition (mounted by rescue_mount_windows.ps1,
#    with a fallback to probe unlettered Basic partitions >1 GB on non-boot disks)
$osVolume = $null
foreach ($vol in Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveLetter -ne 'C' }) {
    if ($vol.FileSystemType -match 'NTFS|ReFS' -and
        (Test-Path "$($vol.DriveLetter):\Windows\System32\config\SYSTEM")) {
        $osVolume = $vol
        break
    }
}

if (-not $osVolume) {
    $unletteredBasic = @(Get-Disk | Where-Object { $_.Number -ne 0 } |
        Get-Partition -ErrorAction SilentlyContinue |
        Where-Object { $_.Type -eq 'Basic' -and -not $_.DriveLetter -and $_.Size -gt 1GB })

    foreach ($part in $unletteredBasic) {
        $freeOsLetter = Get-FreeDriveLetter
        if (-not $freeOsLetter) { break }

        try {
            Set-Partition -DiskNumber $part.DiskNumber -PartitionNumber $part.PartitionNumber `
                -NewDriveLetter $freeOsLetter -Confirm:$false -ErrorAction Stop
            $candidateVol = Get-Volume -DriveLetter $freeOsLetter -ErrorAction Stop
            if ($candidateVol.FileSystemType -match 'NTFS|ReFS' -and
                (Test-Path "${freeOsLetter}:\Windows\System32\config\SYSTEM")) {
                $osVolume = $candidateVol
                break
            }
            # Not the OS volume; release temporary probe letter
            Remove-PartitionAccessPath -DiskNumber $part.DiskNumber `
                -PartitionNumber $part.PartitionNumber `
                -AccessPath "${freeOsLetter}:\" -ErrorAction SilentlyContinue
        } catch {
            Write-Log "  WARNING: Could not inspect partition $($part.PartitionNumber) on Disk $($part.DiskNumber): $_"
        }
    }
}

if (-not $osVolume) {
    # Distinguish BitLocker-locked OS volumes from disks with no Windows installation
    $lockedSuspects = @()
    foreach ($vol in Get-Volume | Where-Object { $_.DriveLetter -and $_.DriveLetter -ne 'C' }) {
        if ($vol.FileSystemType -eq 'Unknown' -and $vol.Size -gt 10GB) {
            $suspect = "$($vol.DriveLetter):"
            if (Get-Command manage-bde.exe -ErrorAction SilentlyContinue) {
                $bde = & manage-bde.exe -status "$suspect" 2>&1 | Out-String
                if ($bde -match 'Lock Status:\s+Locked') {
                    $suspect = "$suspect (BitLocker: Locked)"
                }
            }
            $lockedSuspects += $suspect
        }
    }

    if ($lockedSuspects.Count -gt 0) {
        $FixReason = ("Volume(s) $($lockedSuspects -join ', ') appear BitLocker-locked/unreadable - " +
            "unlock first (manage-bde -unlock <drive>: -RecoveryPassword <key>) before rebuilding BCD")
    } else {
        $FixReason = "Could not find an offline Windows installation (<Drive>:\Windows\System32\config\SYSTEM) on any attached disk"
    }
} else {
    $osLetter = $osVolume.DriveLetter
    $osDrive = "${osLetter}:"
    $windowsPath = Join-Path -Path "${osDrive}\" -ChildPath 'Windows'
    Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Windows installation available at ${windowsPath}"

    $windowsPartition = Get-Partition -DriveLetter $osLetter -ErrorAction SilentlyContinue
    $diskNumber = $windowsPartition.DiskNumber

    # 2. Locate the Boot (EFI System) partition on the SAME disk as the Windows partition
    $espGuid = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    $bootPartition = Get-Partition -DiskNumber $diskNumber -ErrorAction SilentlyContinue |
        Where-Object { $_.Type -eq 'System' -or $_.GptType -eq $espGuid } |
        Select-Object -First 1

    if ($bootPartition) {
        # UEFI boot layout: ensure the FAT32 EFI System Partition has a drive letter
        if ($bootPartition.DriveLetter) {
            $bootLetter = $bootPartition.DriveLetter
        } else {
            $candidate = Get-FreeDriveLetter
            if (-not $candidate) {
                $FixReason = "Could not assign a drive letter to the Boot partition (D-Z all in use)"
            } else {
                try {
                    Set-Partition -DiskNumber $diskNumber -PartitionNumber $bootPartition.PartitionNumber `
                        -NewDriveLetter $candidate -Confirm:$false -ErrorAction Stop
                    $bootLetter = $candidate
                    $assignedBootLetter = $true
                    Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Mounted EFI System Partition at ${bootLetter}:"
                } catch {
                    $FixReason = "Could not assign a drive letter to the Boot partition: $_"
                }
            }
        }

        if (-not $FixReason) {
            try {
                $bootVolume = Get-Volume -DriveLetter $bootLetter -ErrorAction Stop
                $bootDrive = "${bootLetter}:"
                if ($bootVolume.FileSystemType -ne 'FAT32') {
                    $FixReason = "Boot partition on ${bootDrive} is $($bootVolume.FileSystemType) (expected FAT32)"
                } else {
                    $bcdPath = Join-Path -Path "${bootDrive}\" -ChildPath 'EFI\Microsoft\Boot\BCD'
                    $isBcdStore = Test-Path $bcdPath
                    if ($isBcdStore) {
                        Copy-Item -Path $bcdPath -Destination "${bcdPath}.gce-repair-backup" -Force -ErrorAction SilentlyContinue
                        Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Existing BCD at ${bcdPath} backed up to ${bcdPath}.gce-repair-backup"
                    } else {
                        Write-Log "GCE-REPAIR-LINE:[INFO] bcd: BCD missing at ${bcdPath}; creating a new store"
                    }
                }
            } catch {
                $FixReason = "Could not read the Boot partition on ${bootLetter}: $_"
            }
        }
    } else {
        # Legacy BIOS/MBR layout (no EFI System Partition on this disk)
        Write-Log "GCE-REPAIR-LINE:[INFO] bcd: No EFI System Partition on Disk ${diskNumber}; targeting BIOS boot"
    }

    # 3. Run bcdboot.exe and check $LASTEXITCODE
    if (-not $FixReason) {
        try {
            $bcdExe = "$env:SystemRoot\System32\bcdboot.exe"
            if ($bootDrive) {
                Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Running $bcdExe $windowsPath /s $bootDrive /f UEFI"
                $bcdOutput = & $bcdExe $windowsPath /s $bootDrive /f UEFI 2>&1 | Out-String
            } else {
                Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Running $bcdExe $windowsPath /f BIOS"
                $bcdOutput = & $bcdExe $windowsPath /f BIOS 2>&1 | Out-String
            }
            $rc = $LASTEXITCODE
            Write-Log "  bcdboot output (rc $rc): $($bcdOutput.Trim())"

            if ($rc -eq 0) {
                $Fixes = 1
                $targetLabel = if ($bootDrive) { $bootDrive } else { "Disk ${diskNumber} (BIOS)" }
                Write-Log "GCE-REPAIR-LINE:[FIXED] bcd: BCD store on ${targetLabel} was repaired from ${windowsPath}"
            } else {
                $FixReason = "bcdboot.exe failed (rc $rc): $($bcdOutput.Trim())"
            }
        } catch {
            $FixReason = "Could not repair the BCD store: $_"
        }
    }

    # 4. Release the temporary EFI drive letter so restore is clean
    if ($assignedBootLetter -and $bootLetter) {
        try {
            Remove-PartitionAccessPath -DiskNumber $diskNumber `
                -PartitionNumber $bootPartition.PartitionNumber `
                -AccessPath "${bootLetter}:\" -ErrorAction Stop
            Write-Log "  Released temporary EFI drive letter ${bootLetter}:"
        } catch {
            Write-Log "  WARNING: Could not release temporary EFI drive letter ${bootLetter}: $_"
        }
    }
}

# --- Single result-emission point (never call 'exit') ---
if ($FixReason -ne "") {
    Write-Log "GCE-REPAIR-RESULT:FAILED:$FixReason"
} elseif ($Fixes -gt 0) {
    Write-Log "GCE-REPAIR-RESULT:SUCCESS:$Fixes"
} else {
    Write-Log "GCE-REPAIR-RESULT:NO_ISSUES:0"
}
