#!/bin/bash
# GCE Repair - fstab fix script
#
# Runs after disk is mounted at /mnt/sysroot.
# Scans /etc/fstab for invalid entries and comments them out.
# Emits structured markers to stderr (serial console) for orchestrator parsing.
#
# Checks performed:
#   - UUID= entries: verified against blkid output
#   - LABEL= entries: verified against blkid output
#   - PARTUUID= entries: verified against blkid output
#   - /dev/ entries: boot disk partitions mapped + validated; secondary disk
#                    entries flagged (not present on rescue VM)
#   - Malformed entries: fewer than 3 fields
#   - Virtual filesystems (proc, tmpfs, etc.): always skipped

SYSROOT="/mnt/sysroot"
FSTAB="$SYSROOT/etc/fstab"
BACKUP="$SYSROOT/etc/fstab.gce-repair-backup"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [REPAIR] $1" | tee -a "$LOGFILE"
}

repair_line() {
    echo "GCE-REPAIR-LINE:$1" >&2
    log "$1"
}

repair_result() {
    echo "GCE-REPAIR-RESULT:$1" >&2
    log "Repair result: $1"
}

# Virtual filesystem types that are always valid (no device check needed)
VIRTUAL_FS="proc sysfs tmpfs devtmpfs devpts securityfs cgroup cgroup2 pstore efivarfs bpf debugfs tracefs fusectl configfs ramfs hugetlbfs mqueue systemd-1 autofs binfmt_misc swap"

is_virtual_fs() {
    local fstype="$1"
    for vfs in $VIRTUAL_FS; do
        if [ "$fstype" = "$vfs" ]; then
            return 0
        fi
    done
    return 1
}

log "=== fstab repair started ==="

# Verify fstab exists
if [ ! -f "$FSTAB" ]; then
    repair_result "FAILED:fstab not found at $FSTAB"
    exit 1
fi

# Create backup
cp "$FSTAB" "$BACKUP"
if [ $? -ne 0 ]; then
    repair_result "FAILED:could not create backup"
    exit 1
fi
log "Backup created: $BACKUP"

# Get available UUIDs, PARTUUIDs, and LABELs from all block devices
AVAILABLE_UUIDS=$(blkid -o value -s UUID 2>/dev/null | sort -u)
AVAILABLE_PARTUUIDS=$(blkid -o value -s PARTUUID 2>/dev/null | sort -u)
AVAILABLE_LABELS=$(blkid -o value -s LABEL 2>/dev/null | sort -u)
log "Found $(echo "$AVAILABLE_UUIDS" | grep -c .) UUIDs on system"

# --- Device mapping for /dev/ entries ---
# Find the target disk's device on the rescue VM.
# The rescue_mount.sh script uses: /dev/disk/by-id/google-${disk}
# 'disk' variable is set by rescue_mount.sh before this script runs.
TARGET_DISK_DEV=""
TARGET_DISK_BASE=""
if [ -n "$disk" ] && [ -e "/dev/disk/by-id/google-${disk}" ]; then
    TARGET_DISK_DEV=$(readlink -f "/dev/disk/by-id/google-${disk}")
    # Extract base device (e.g., /dev/sdb from /dev/sdb1 or /dev/sdb)
    TARGET_DISK_BASE=$(echo "$TARGET_DISK_DEV" | sed 's/[0-9]*$//')
    log "Target disk on rescue VM: $TARGET_DISK_DEV (base: $TARGET_DISK_BASE)"
else
    log "WARNING: Could not determine target disk device (disk=$disk)"
fi

# Build a map of target disk partitions and their filesystem types
# e.g., "1:ext4" "2:vfat" (partition_number:fstype)
declare -A TARGET_PART_FSTYPE
TARGET_DISK_SHORT=$(basename "$TARGET_DISK_BASE")  # e.g., "sdb"
if [ -n "$TARGET_DISK_BASE" ]; then
    while IFS= read -r bline; do
        part_dev=$(echo "$bline" | awk '{print $1}')
        part_fstype=$(echo "$bline" | awk '{print $2}')
        if [ -n "$part_dev" ] && [ -n "$part_fstype" ]; then
            # Extract partition number from short device name
            # lsblk outputs short names like "sdb1", strip the base "sdb" to get "1"
            part_num=$(echo "$part_dev" | sed "s|^${TARGET_DISK_SHORT}||")
            if [ -n "$part_num" ]; then
                TARGET_PART_FSTYPE["$part_num"]="$part_fstype"
                log "  Target partition ${part_num}: ${part_fstype} (${part_dev})"
            fi
        fi
    done < <(lsblk -rno NAME,FSTYPE "$TARGET_DISK_BASE" 2>/dev/null | grep -v "^${TARGET_DISK_SHORT} ")
fi

# --- Determine original boot disk device base from fstab root entry ---
# On GCE: boot disk is typically /dev/sda, secondary disks are /dev/sdb, sdc, etc.
# On rescue VM: rescue disk = /dev/sda, original boot disk = /dev/sdb.
# We need to know the original boot base to distinguish boot disk partitions
# from secondary disk entries that may reference detached disks.
ORIGINAL_BOOT_BASE=""
while IFS= read -r fline; do
    echo "$fline" | grep -qE '^\s*$|^\s*#' && continue
    fmountpoint=$(echo "$fline" | awk '{print $2}')
    fdevice=$(echo "$fline" | awk '{print $1}')
    if [ "$fmountpoint" = "/" ]; then
        if echo "$fdevice" | grep -q '^/dev/'; then
            ORIGINAL_BOOT_BASE=$(basename "$fdevice" | sed 's/[0-9]*$//')
        fi
        break
    fi
done < "$FSTAB"

# Default: GCE boot disk is almost always /dev/sda
if [ -z "$ORIGINAL_BOOT_BASE" ]; then
    ORIGINAL_BOOT_BASE="sda"
    log "Root uses UUID/PARTUUID, assuming original boot device base: sda"
else
    log "Original boot device base from fstab: $ORIGINAL_BOOT_BASE"
fi

# Function to check /dev/ device entries
# Return codes: 0=valid, 1=missing device, 2=fstype mismatch, 3=secondary disk not present
check_dev_entry() {
    local device="$1"
    local fstab_fstype="$2"
    local mountpoint="$3"

    # Skip /dev/disk/by-* entries (handled by UUID/LABEL/PARTUUID checks)
    if echo "$device" | grep -q '^/dev/disk/by-'; then
        return 0  # valid
    fi

    # Skip swap entries
    if [ "$fstab_fstype" = "swap" ]; then
        return 0  # handled separately
    fi

    # Extract device base and partition number
    # e.g., /dev/sda1 -> base=sda, partnum=1
    local dev_basename=$(basename "$device")
    local dev_base=$(echo "$dev_basename" | sed 's/[0-9]*$//')
    local dev_partnum=$(echo "$dev_basename" | sed "s/^${dev_base}//")

    # If this is a root mount (/) or /boot/efi, don't touch it
    if [ "$mountpoint" = "/" ]; then
        return 0  # never comment out root
    fi

    # Check if this is a boot disk partition or a secondary disk
    if [ -n "$ORIGINAL_BOOT_BASE" ] && [ "$dev_base" != "$ORIGINAL_BOOT_BASE" ]; then
        # Different device base than boot disk — this is a secondary disk.
        # Secondary disks are not attached to the rescue VM, so we can't
        # validate them. Flag as missing.
        log "Device $device: secondary disk (base $dev_base != boot base $ORIGINAL_BOOT_BASE)"
        return 3  # secondary disk not present
    fi

    # This is a boot disk partition — map to target disk and validate
    if [ -n "$TARGET_DISK_BASE" ] && [ -n "$dev_partnum" ]; then
        # Check if this partition exists on the target disk
        local mapped_dev="${TARGET_DISK_BASE}${dev_partnum}"
        if [ ! -e "$mapped_dev" ]; then
            log "Device $device -> mapped to $mapped_dev (not found)"
            return 1  # invalid: partition doesn't exist on target disk
        fi

        # Check filesystem type matches
        local actual_fstype="${TARGET_PART_FSTYPE[$dev_partnum]}"
        if [ -n "$actual_fstype" ] && [ -n "$fstab_fstype" ] && [ "$actual_fstype" != "$fstab_fstype" ]; then
            log "Device $device -> mapped to $mapped_dev (fstype mismatch: fstab=$fstab_fstype, actual=$actual_fstype)"
            return 2  # invalid: filesystem type mismatch
        fi

        log "Device $device -> mapped to $mapped_dev (OK, fstype=$actual_fstype)"
        return 0  # valid
    fi

    # Fallback: if no target disk mapping available, check device existence
    if [ ! -e "$device" ] && [ ! -e "$SYSROOT$device" ]; then
        return 1  # device not found
    fi

    return 0  # assume valid
}

fixes=0
line_num=0
tmpfile=$(mktemp)

while IFS= read -r line; do
    line_num=$((line_num + 1))

    # Pass through empty lines and comments unchanged
    if echo "$line" | grep -qE '^\s*$|^\s*#'; then
        echo "$line" >> "$tmpfile"
        continue
    fi

    # Parse fstab fields: device mountpoint fstype options dump pass
    device=$(echo "$line" | awk '{print $1}')
    mountpoint=$(echo "$line" | awk '{print $2}')
    fstype=$(echo "$line" | awk '{print $3}')
    field_count=$(echo "$line" | awk '{print NF}')

    # Check for malformed entries (need at least device, mountpoint, fstype)
    if [ "$field_count" -lt 3 ]; then
        echo "# GCE-REPAIR: commented out malformed entry (${field_count} fields)" >> "$tmpfile"
        echo "#$line" >> "$tmpfile"
        fixes=$((fixes + 1))
        repair_line "[FIXED] fstab: Commented out malformed entry on line $line_num"
        continue
    fi

    # Skip virtual filesystems - they don't need device validation
    if is_virtual_fs "$fstype"; then
        echo "$line" >> "$tmpfile"
        continue
    fi

    # Check UUID= entries
    if echo "$device" | grep -q '^UUID='; then
        uuid=$(echo "$device" | sed 's/UUID=//')
        if ! echo "$AVAILABLE_UUIDS" | grep -qF "$uuid"; then
            short_uuid="${uuid:0:12}..."
            echo "# GCE-REPAIR: commented out invalid UUID entry (UUID not found on disk)" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out invalid UUID entry for $mountpoint ($short_uuid)"
            continue
        fi
    fi

    # Check PARTUUID= entries
    if echo "$device" | grep -q '^PARTUUID='; then
        partuuid=$(echo "$device" | sed 's/PARTUUID=//')
        if ! echo "$AVAILABLE_PARTUUIDS" | grep -qF "$partuuid"; then
            short_partuuid="${partuuid:0:12}..."
            echo "# GCE-REPAIR: commented out invalid PARTUUID entry (PARTUUID not found)" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out invalid PARTUUID entry for $mountpoint ($short_partuuid)"
            continue
        fi
    fi

    # Check LABEL= entries
    if echo "$device" | grep -q '^LABEL='; then
        label=$(echo "$device" | sed 's/LABEL=//')
        if ! echo "$AVAILABLE_LABELS" | grep -qF "$label"; then
            echo "# GCE-REPAIR: commented out invalid LABEL entry (label not found)" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out invalid LABEL entry for $mountpoint ($label)"
            continue
        fi
    fi

    # Check /dev/ entries with device mapping
    if echo "$device" | grep -q '^/dev/'; then
        check_dev_entry "$device" "$fstype" "$mountpoint"
        check_result=$?
        if [ $check_result -eq 3 ]; then
            echo "# GCE-REPAIR: commented out secondary disk entry (disk not attached)" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out secondary disk entry for $mountpoint ($device)"
            continue
        elif [ $check_result -eq 1 ]; then
            echo "# GCE-REPAIR: commented out missing device entry" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out missing device entry for $mountpoint ($device)"
            continue
        elif [ $check_result -eq 2 ]; then
            # Get the actual filesystem type for the error message
            local_dev_basename=$(basename "$device")
            local_dev_base=$(echo "$local_dev_basename" | sed 's/[0-9]*$//')
            local_dev_partnum=$(echo "$local_dev_basename" | sed "s/^${local_dev_base}//")
            actual_fs="${TARGET_PART_FSTYPE[$local_dev_partnum]}"
            echo "# GCE-REPAIR: commented out wrong filesystem type (fstab=$fstype, actual=$actual_fs)" >> "$tmpfile"
            echo "#$line" >> "$tmpfile"
            fixes=$((fixes + 1))
            repair_line "[FIXED] fstab: Commented out wrong fstype for $mountpoint ($device: fstab=$fstype, actual=$actual_fs)"
            continue
        fi
    fi

    # Entry looks valid, keep it
    echo "$line" >> "$tmpfile"

done < "$FSTAB"

# Replace fstab with fixed version
cp "$tmpfile" "$FSTAB"
rm -f "$tmpfile"

log "=== fstab repair completed: $fixes issues fixed ==="

if [ $fixes -gt 0 ]; then
    repair_result "SUCCESS:$fixes"
else
    repair_result "NO_ISSUES:0"
fi
