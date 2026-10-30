# GCE Rescue - Windows BCD repair fix script (for use with: repair --fix-script)
#
# Rebuilds the Boot Configuration Data (BCD) store and boot files of an
# offline Windows installation via bcdboot.exe.
#
# Scope notes:
# - By default bcdboot migrates entries from an existing store where it can
#   (backup kept at BCD.gce-repair-backup); custom BCD entries or flags that
#   cannot be merged may not be preserved.
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
        Where-Object { $_.Type -in @('Basic', 'IFS') -and -not $_.DriveLetter -and $_.Size -gt 1GB })

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

    # 2. Locate the Boot partition on the SAME disk as the Windows partition
    #    - UEFI/GPT: EFI System Partition (FAT32)
    #    - BIOS/MBR: Active partition (e.g. System Reserved), falling back to OS partition
    $diskStyle = (Get-Disk -Number $diskNumber -ErrorAction SilentlyContinue).PartitionStyle
    $espGuid = '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}'
    $espPartition = Get-Partition -DiskNumber $diskNumber -ErrorAction SilentlyContinue |
        Where-Object { $_.GptType -eq $espGuid -or ($diskStyle -eq 'GPT' -and $_.Type -eq 'System') } |
        Select-Object -First 1

    if ($espPartition) {
        $firmwareType = 'UEFI'
        $bootPartition = $espPartition
    } else {
        $firmwareType = 'BIOS'
        $bootPartition = Get-Partition -DiskNumber $diskNumber -ErrorAction SilentlyContinue |
            Where-Object { $_.IsActive } |
            Select-Object -First 1
        if (-not $bootPartition) {
            $bootPartition = $windowsPartition
        }
        Write-Log ("GCE-REPAIR-LINE:[INFO] bcd: No EFI System Partition on Disk ${diskNumber}; " +
            "targeting BIOS system partition $($bootPartition.PartitionNumber)")
    }

    if (-not $bootPartition) {
        $FixReason = "Could not locate a boot/system partition on Disk ${diskNumber}"
    } elseif ($bootPartition.DriveLetter) {
        $bootLetter = [string]$bootPartition.DriveLetter
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
                Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Mounted ${firmwareType} boot partition at ${bootLetter}:"
            } catch {
                $FixReason = "Could not assign a drive letter to the Boot partition: $_"
            }
        }
    }

    if (-not $FixReason) {
        try {
            $bootVolume = Get-Volume -DriveLetter $bootLetter -ErrorAction Stop
            $bootDrive = "${bootLetter}:"
            if ($firmwareType -eq 'UEFI' -and $bootVolume.FileSystemType -ne 'FAT32') {
                $FixReason = "Boot partition on ${bootDrive} is $($bootVolume.FileSystemType) (expected FAT32)"
            } elseif ($firmwareType -eq 'BIOS' -and $bootVolume.FileSystemType -notmatch 'NTFS|FAT32') {
                $FixReason = "BIOS system partition on ${bootDrive} is $($bootVolume.FileSystemType) (expected NTFS or FAT32)"
            } else {
                $bcdRelPath = if ($firmwareType -eq 'UEFI') { 'EFI\Microsoft\Boot\BCD' } else { 'Boot\BCD' }
                $bcdPath = Join-Path -Path "${bootDrive}\" -ChildPath $bcdRelPath
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

    # 3. Run bcdboot.exe with explicit /s <bootDrive> on BOTH UEFI and BIOS
    if (-not $FixReason) {
        try {
            $bcdExe = "$env:SystemRoot\System32\bcdboot.exe"
            if ($firmwareType -eq 'UEFI') {
                Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Running $bcdExe $windowsPath /s $bootDrive /f UEFI"
                $bcdOutput = & $bcdExe $windowsPath /s $bootDrive /f UEFI 2>&1 | Out-String
            } else {
                Write-Log "GCE-REPAIR-LINE:[INFO] bcd: Running $bcdExe $windowsPath /s $bootDrive /f BIOS"
                $bcdOutput = & $bcdExe $windowsPath /s $bootDrive /f BIOS 2>&1 | Out-String
            }
            $rc = $LASTEXITCODE
            Write-Log "  bcdboot output (rc $rc): $($bcdOutput.Trim())"

            if ($rc -eq 0) {
                $Fixes = 1
                Write-Log "GCE-REPAIR-LINE:[FIXED] bcd: BCD store on ${bootDrive} (${firmwareType}) was repaired from ${windowsPath}"
            } else {
                $FixReason = "bcdboot.exe failed (rc $rc): $($bcdOutput.Trim())"
            }
        } catch {
            $FixReason = "Could not repair the BCD store: $_"
        }
    }

    # 4. Release any temporary boot-partition drive letter so restore is clean
    if ($assignedBootLetter -and $bootLetter) {
        try {
            Remove-PartitionAccessPath -DiskNumber $diskNumber `
                -PartitionNumber $bootPartition.PartitionNumber `
                -AccessPath "${bootLetter}:\" -ErrorAction Stop
            Write-Log "  Released temporary ${firmwareType} boot drive letter ${bootLetter}:"
        } catch {
            Write-Log "  WARNING: Could not release temporary ${firmwareType} boot drive letter ${bootLetter}: $_"
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
