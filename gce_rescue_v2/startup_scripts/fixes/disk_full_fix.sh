#!/bin/bash
# GCE Repair - Boot disk full fix script (non-destructive)
#
# Runs in two phases:
# 1. Pre-mount: Expands the root partition (growpart) and ext2/3/4 filesystem
#    (resize2fs) after the boot disk has been resized via the GCE API.
# 2. Post-mount: Completes XFS online expansion (xfs_growfs) if applicable, and
#    reports the top space-consuming directories on the boot disk so the
#    customer can review and clean them up manually if desired.
#    NOTE: Never deletes any files from the customer's disk.

# === GCE-REPAIR-PREMOUNT-BEGIN ===
DISK_FULL_PART_EXPANDED=false
DISK_FULL_FS_EXPANDED=false
DISK_FULL_EXPANDED_DEV=""

udevadm settle 2>/dev/null || sleep 2

disk_p_pre=$(lsblk -rf /dev/disk/by-id/google-${disk} 2>/dev/null | grep -iE 'ext[2-4]|xfs' | head -1)
if [ -n "$disk_p_pre" ]; then
    part_dev_name=$(echo "$disk_p_pre" | awk '{print $1}')
    part_fs_type=$(echo "$disk_p_pre" | awk '{print $2}')
    if [ -n "$part_dev_name" ] && [ -b "/dev/$part_dev_name" ]; then
        real_part=$(readlink -f "/dev/$part_dev_name")
        DISK_FULL_EXPANDED_DEV="$real_part"
        if echo "$real_part" | grep -qE '(nvme|mmcblk|loop)[0-9a-z]+p[0-9]+$'; then
            parent_disk=$(echo "$real_part" | sed -E 's/p[0-9]+$//')
        else
            parent_disk=$(echo "$real_part" | sed -E 's/[0-9]+$//')
        fi
        part_num=$(echo "$real_part" | grep -oE '[0-9]+$')

        if [ -b "$parent_disk" ] && [ -n "$part_num" ]; then
            log "[REPAIR] Expanding partition $real_part ($parent_disk partition $part_num)..."
            if command -v sgdisk >/dev/null 2>&1; then
                log "[REPAIR] Moving backup GPT header to end of $parent_disk with sgdisk -e..."
                sgdisk -e "$parent_disk" 2>&1 | tee -a "$LOGFILE" || true
                partprobe "$parent_disk" 2>/dev/null || true
                udevadm settle 2>/dev/null || true
            fi

            if command -v growpart >/dev/null 2>&1; then
                grow_out=$(growpart "$parent_disk" "$part_num" 2>&1)
                grow_rc=$?
                log "[REPAIR] growpart output (rc=$grow_rc): $grow_out"
                if [ $grow_rc -eq 0 ]; then
                    DISK_FULL_PART_EXPANDED=true
                fi
            fi

            if [ "$DISK_FULL_PART_EXPANDED" = "false" ] && command -v parted >/dev/null 2>&1; then
                parted_out=$(parted -s "$parent_disk" resizepart "$part_num" 100% 2>&1)
                parted_rc=$?
                log "[REPAIR] parted resizepart output (rc=$parted_rc): $parted_out"
                if [ $parted_rc -eq 0 ]; then
                    DISK_FULL_PART_EXPANDED=true
                fi
            fi

            if [ "$DISK_FULL_PART_EXPANDED" = "true" ]; then
                partprobe "$parent_disk" 2>/dev/null || true
                udevadm settle 2>/dev/null || true
                if echo "$part_fs_type" | grep -qiE '^ext[2-4]$'; then
                    log "[REPAIR] Running e2fsck and resize2fs on $real_part..."
                    e2fsck -fy "$real_part" 2>&1 | tee -a "$LOGFILE" || true
                    resize2fs "$real_part" 2>&1 | tee -a "$LOGFILE"
                    if [ ${PIPESTATUS[0]} -eq 0 ]; then
                        DISK_FULL_FS_EXPANDED=true
                    fi
                fi
            fi
        fi
    fi
fi
# === GCE-REPAIR-PREMOUNT-END ===

SYSROOT="/mnt/sysroot"

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

log "=== disk_full repair started ==="

if [ ! -d "$SYSROOT" ] || ! mountpoint -q "$SYSROOT"; then
    repair_result "FAILED:sysroot not mounted at $SYSROOT"
else

fixes=0

# Resolve mounted device if pre-mount did not expand it yet
if [ "${DISK_FULL_PART_EXPANDED:-false}" = "false" ]; then
    src_dev=$(findmnt -n -o SOURCE "$SYSROOT" 2>/dev/null)
    if [ -n "$src_dev" ] && [ -b "$src_dev" ]; then
        real_part=$(readlink -f "$src_dev")
        DISK_FULL_EXPANDED_DEV="$real_part"
        if echo "$real_part" | grep -qE '(nvme|mmcblk|loop)[0-9a-z]+p[0-9]+$'; then
            parent_disk=$(echo "$real_part" | sed -E 's/p[0-9]+$//')
        else
            parent_disk=$(echo "$real_part" | sed -E 's/[0-9]+$//')
        fi
        part_num=$(echo "$real_part" | grep -oE '[0-9]+$')
        if [ -b "$parent_disk" ] && [ -n "$part_num" ]; then
            if command -v sgdisk >/dev/null 2>&1; then
                sgdisk -e "$parent_disk" 2>&1 | tee -a "$LOGFILE" || true
            fi
            if command -v growpart >/dev/null 2>&1 && growpart "$parent_disk" "$part_num" 2>&1 | tee -a "$LOGFILE"; then
                if [ ${PIPESTATUS[0]} -eq 0 ]; then
                    DISK_FULL_PART_EXPANDED=true
                fi
            fi
            if [ "${DISK_FULL_PART_EXPANDED:-false}" = "false" ] && command -v parted >/dev/null 2>&1; then
                parted -s "$parent_disk" resizepart "$part_num" 100% 2>&1 | tee -a "$LOGFILE"
                if [ ${PIPESTATUS[0]} -eq 0 ]; then
                    DISK_FULL_PART_EXPANDED=true
                fi
            fi
            partprobe "$parent_disk" 2>/dev/null || true
            udevadm settle 2>/dev/null || true
        fi
    fi
fi

# 1. Record or complete partition/filesystem expansion
if [ "${DISK_FULL_FS_EXPANDED:-false}" = "true" ]; then
    new_size=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $2}')
    avail_after=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $4}')
    fixes=$((fixes + 1))
    repair_line "[FIXED] disk_full: Expanded root partition and filesystem on ${DISK_FULL_EXPANDED_DEV:-boot disk} (size: ${new_size:-expanded}, available: ${avail_after:-unknown})"
elif [ "${DISK_FULL_PART_EXPANDED:-false}" = "true" ]; then
    mounted_fs=$(findmnt -n -o FSTYPE "$SYSROOT" 2>/dev/null)
    if [ "$mounted_fs" = "xfs" ] && command -v xfs_growfs >/dev/null 2>&1; then
        log "Expanding XFS filesystem at $SYSROOT..."
        xfs_growfs "$SYSROOT" 2>&1 | tee -a "$LOGFILE"
        if [ ${PIPESTATUS[0]} -eq 0 ]; then
            new_size=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $2}')
            avail_after=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $4}')
            fixes=$((fixes + 1))
            repair_line "[FIXED] disk_full: Expanded root partition and XFS filesystem (size: ${new_size:-expanded}, available: ${avail_after:-unknown})"
        fi
    elif echo "$mounted_fs" | grep -qiE '^ext[2-4]$' && [ -n "$DISK_FULL_EXPANDED_DEV" ]; then
        log "Expanding ext filesystem on $DISK_FULL_EXPANDED_DEV..."
        resize2fs "$DISK_FULL_EXPANDED_DEV" 2>&1 | tee -a "$LOGFILE"
        if [ ${PIPESTATUS[0]} -eq 0 ]; then
            new_size=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $2}')
            avail_after=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $4}')
            fixes=$((fixes + 1))
            repair_line "[FIXED] disk_full: Expanded root partition and filesystem on $DISK_FULL_EXPANDED_DEV (size: ${new_size:-expanded}, available: ${avail_after:-unknown})"
        fi
    fi
fi

# 2. Non-destructively list top space-consuming directories so customer can review
top_dirs=$(du -xh "$SYSROOT" --max-depth=2 2>/dev/null | grep -v -E "^[0-9.]+[KMGTP]?\s+${SYSROOT}/?$" | sort -rh | head -n 5 | sed "s|${SYSROOT}||g" | awk '{printf "%s (%s), ", $2, $1}' | sed 's/, $//')
if [ -n "$top_dirs" ]; then
    repair_line "[INFO] disk_full: Largest directories on boot disk: $top_dirs"
fi

avail_kb=$(df -k "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $4}')
avail_human=$(df -h "$SYSROOT" 2>/dev/null | tail -1 | awk '{print $4}')
log "=== disk_full repair completed: $fixes fixes applied, free space: ${avail_human:-unknown} ==="

if [ $fixes -gt 0 ]; then
    repair_result "SUCCESS:$fixes"
elif [ -n "$avail_kb" ] && [ "$avail_kb" -lt 51200 ]; then
    repair_result "FAILED:Boot disk could not be expanded (${avail_human:-0B} free)"
else
    repair_result "NO_ISSUES:0"
fi

fi # end sysroot guard

# Copy full log to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null || true


