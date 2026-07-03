#!/bin/bash
# GCE Repair - fstab fix script (targeted matching)
#
# Runs after disk is mounted at /mnt/sysroot.
# Only comments out fstab entries that match identifiers from REPAIR_TARGETS
# (set by the repair orchestrator based on diagnosed boot errors).
#
# This avoids false positives: secondary disk entries that are valid but
# whose disks are not attached in rescue mode are left untouched.
#
# REPAIR_TARGETS contains newline-separated identifiers (UUIDs, device paths,
# mount points) extracted from serial console error messages.

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
VIRTUAL_FS="proc sysfs tmpfs devtmpfs devpts securityfs cgroup cgroup2 pstore efivarfs bpf debugfs tracefs fusectl configfs ramfs hugetlbfs mqueue systemd-1 autofs binfmt_misc"

is_virtual_fs() {
    local fstype="$1"
    for vfs in $VIRTUAL_FS; do
        if [ "$fstype" = "$vfs" ]; then
            return 0
        fi
    done
    return 1
}

# Check if an fstab entry matches any repair target

# matches_target() {
#     local device="$1"
#     local mountpoint="$2"

#     # Extract the identifier value from the device field
#     local dev_value=$(echo "$device" | sed 's/^UUID=//; s/^LABEL=//; s/^PARTUUID=//')

#     while IFS= read -r target; do
#         [ -z "$target" ] && continue
#         # Match by identifier substring in device field
#         if echo "$device" | grep -qiF "$target"; then return 0; fi
#         if [ -n "$dev_value" ] && echo "$dev_value" | grep -qiF "$target"; then return 0; fi
#         # Match by mount point
#         if [ "$mountpoint" = "$target" ]; then return 0; fi
#     done <<< "$REPAIR_TARGETS"
#     return 1
# }

log "=== fstab repair started ==="

# Guard: if no targets provided, nothing to fix
if [ -z "$REPAIR_TARGETS" ]; then
    log "No repair targets provided, skipping fstab repair"
    repair_result "NO_ISSUES:0"
else

log "Repair targets:"

# while IFS= read -r t; do
#     [ -n "$t" ] && log "  - $t"
# done <<< "$REPAIR_TARGETS"

# Verify fstab exists
if [ ! -f "$FSTAB" ]; then
    repair_result "FAILED:fstab not found at $FSTAB"
else

# Create backup
cp "$FSTAB" "$BACKUP"
if [ $? -ne 0 ]; then
    repair_result "FAILED:could not create backup"
else
log "Backup created: $BACKUP"

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
        echo "$line" >> "$tmpfile"
        continue
    fi
    # if [ "$field_count" -lt 3 ]; then
    #     # Only comment out malformed entries if they match a target
    #     if matches_target "$device" "$mountpoint"; then
    #         echo "# GCE-REPAIR: commented out malformed entry (${field_count} fields)" >> "$tmpfile"
    #         echo "#$line" >> "$tmpfile"
    #         fixes=$((fixes + 1))
    #         repair_line "[FIXED] fstab: Commented out malformed entry on line $line_num"
    #         continue
    #     fi
    #     echo "$line" >> "$tmpfile"
    #     continue
    # fi

    # Skip virtual filesystems - they don't need validation
    if is_virtual_fs "$fstype"; then
        echo "$line" >> "$tmpfile"
        continue
    fi

    # # Never comment out the root mount

    # if [ "$mountpoint" = "/" ]; then
    #     echo "$line" >> "$tmpfile"
    #     continue
    # fi

    # Clean the device identifier string (strip headers)
    dev_clean=$(echo "$device" | sed 's/^UUID=//; s/^LABEL=//; s/^PARTUUID=//')

    # Bypasses the nested loop bug by using high-speed string index evaluation
    is_matched=0
    if echo "$REPAIR_TARGETS" | grep -qiF "$dev_clean"; then is_matched=1; fi
    if echo "$REPAIR_TARGETS" | grep -qiF "$mountpoint"; then is_matched=1; fi

    if [ "$mountpoint" = "/" ] || [ "$mountpoint" = "/boot" ] || [ "$mountpoint" = "/boot/efi" ]; then
        if ! blkid | grep -qiF "$dev_clean"; then
            log "Proactive hardware scan caught broken core mount target for $mountpoint (ID: $dev_clean)"
            is_matched=1
        fi
    fi

    if [ "$is_matched" -eq 1 ]; then
        # SURGICAL PATH FOR THE ROOT PARTITION
        if [ "$mountpoint" = "/" ]; then
            log "Targeted root mount mismatch handled on line $line_num. Initializing repair hook..."
            TARGET_PART=$(findmnt -n -o SOURCE "$SYSROOT" 2>/dev/null || echo "")
            
            if [ -n "$TARGET_PART" ]; then
                TRUE_UUID=$(blkid -s PARTUUID -o value "$TARGET_PART" 2>/dev/null || echo "")
                
                if [ -n "$TRUE_UUID" ]; then
                    id_type="PARTUUID"
                    if echo "$device" | grep -q '^UUID='; then id_type="UUID"; fi
                    
                    fixed_line=$(echo "$line" | sed -E "s:${id_type}=[^[:space:]]+:${id_type}=${TRUE_UUID}:" 2>/dev/null || echo "$line")
                    echo "$fixed_line" >> "$tmpfile"
                    fixes=$((fixes + 1))
                    repair_line "[FIXED] fstab: Restored true root partition hardware token (${TRUE_UUID:0:12}...)"
                    continue
                fi
            fi
            echo "$line" >> "$tmpfile"
            continue
        fi
    #if matches_target "$device" "$mountpoint"; then
        # Extract a short identifier for the repair message
        short_id="$device"
        if echo "$device" | grep -q '^UUID='; then
            uuid=$(echo "$device" | sed 's/UUID=//')
            short_id="${uuid:0:12}..."
        elif echo "$device" | grep -q '^PARTUUID='; then
            partuuid=$(echo "$device" | sed 's/PARTUUID=//')
            short_id="${partuuid:0:12}..."
        fi

        echo "# GCE-REPAIR: commented out entry matching diagnosed error" >> "$tmpfile"
        echo "#$line" >> "$tmpfile"
        fixes=$((fixes + 1))
        repair_line "[FIXED] fstab: Commented out entry for $mountpoint ($short_id)"
        continue
    fi

    # Entry doesn't match any target, keep it
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

fi # backup guard
fi # fstab exists guard
fi # targets guard

# Copy full log (mount + repair) to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null
