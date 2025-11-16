#!/bin/bash
# GCE Rescue V2 - Auto-mount startup script
#
# This script runs on boot in rescue mode and:
# 1. Changes hostname to indicate rescue mode
# 2. Waits for original disk to appear
# 3. Mounts original disk at /mnt/sysroot
# 4. Mounts proc, sys, dev for chroot support

# Setup logging
LOGFILE="/var/log/gce-rescue.log"
STATUS_FILE="/var/run/gce-rescue.status"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}

log "=== GCE Rescue Auto-Mount Started ==="

# Change hostname to indicate rescue mode
log "Changing hostname to rescue mode..."
sed -i "s/$(hostname)/$(hostname)-rescue/g" /etc/hosts
hostname $(hostname)-rescue
log "Hostname changed to: $(hostname)"

# Wait for affected disk and mount it
disk=DISK_NAME_PLACEHOLDER
log "Looking for affected disk: $disk"

mkdir -p /mnt/sysroot
log "Created mount point: /mnt/sysroot"

# Wait for disk to appear
log "Waiting for disk to appear..."
attempt=0
while [ ! -e /dev/disk/by-id/google-${disk} ]; do
    attempt=$((attempt + 1))
    log "Attempt $attempt: Disk not found yet, waiting 5 seconds..."
    sleep 5
    if [ $attempt -gt 60 ]; then
        log "ERROR: Disk not found after 5 minutes! Giving up."
        echo "FAILED: Disk not found" > "$STATUS_FILE"
        exit 1
    fi
done

log "SUCCESS: Disk found at /dev/disk/by-id/google-${disk}"
log "Disk appears as: $(ls -l /dev/disk/by-id/google-${disk})"

# Find filesystem and mount
log "Detecting filesystem type..."
disk_p=$(lsblk -rf /dev/disk/by-id/google-${disk} | egrep -i 'ext[3-4]|xfs|microsoft' | head -1)

if [ -n "$disk_p" ]; then
    fs_type=$(echo "$disk_p" | awk '{print $2}')
    dev_name=$(echo "$disk_p" | awk '{print $1}')
    log "Detected filesystem: $fs_type on /dev/$dev_name"

    # Mount main filesystem
    dev_path="/dev/${disk_p%% *}"
    log "Mounting $dev_path to /mnt/sysroot..."

    mount_success=false

    # For XFS, use nouuid option to handle duplicate UUIDs
    # (common when mounting a boot disk as secondary)
    if [ "$fs_type" = "xfs" ]; then
        log "Using nouuid option for XFS filesystem"
        mount -o nouuid "$dev_path" /mnt/sysroot 2>&1 | tee -a "$LOGFILE"
    else
        mount "$dev_path" /mnt/sysroot 2>&1 | tee -a "$LOGFILE"
    fi

    # Check if mount actually succeeded (check mount point, not exit code)
    if mountpoint -q /mnt/sysroot; then
        mount_success=true
        log "SUCCESS: Main filesystem mounted"
        log "Mount info: $(df -h /mnt/sysroot | tail -1)"
    else
        log "First mount attempt did not succeed, trying alternatives..."

        # Try with nouuid and norecovery (for XFS with dirty journal)
        if [ "$fs_type" = "xfs" ]; then
            log "Trying XFS with nouuid,norecovery options..."
            mount -o nouuid,norecovery "$dev_path" /mnt/sysroot 2>&1 | tee -a "$LOGFILE"
            if mountpoint -q /mnt/sysroot; then
                mount_success=true
                log "SUCCESS: Mounted XFS with norecovery option"
                log "WARNING: Mounted read-only due to dirty journal"
                log "Mount info: $(df -h /mnt/sysroot | tail -1)"
            fi
        fi

        # Final fallback: try read-only
        if [ "$mount_success" = "false" ]; then
            log "Trying read-only mount..."
            mount -o ro,nouuid "$dev_path" /mnt/sysroot 2>&1 | tee -a "$LOGFILE"
            if mountpoint -q /mnt/sysroot; then
                mount_success=true
                log "SUCCESS: Mounted read-only"
                log "WARNING: Filesystem mounted as READ-ONLY"
                log "Mount info: $(df -h /mnt/sysroot | tail -1)"
            fi
        fi

        if [ "$mount_success" = "false" ]; then
            log "ERROR: All mount attempts failed"
            log "Manual intervention required. Try:"
            log "  mount -o nouuid $dev_path /mnt/sysroot"
            log "  or check dmesg for mount errors"
            echo "FAILED: Mount error" > "$STATUS_FILE"
            exit 1
        fi
    fi

    # Mount proc, sys, dev for chroot
    log "Mounting proc, sys, dev for chroot support..."

    if [ -d /mnt/sysroot/proc ]; then
        mount -t proc proc /mnt/sysroot/proc && log "  - proc mounted" || log "  - WARNING: proc mount failed"
    fi

    if [ -d /mnt/sysroot/sys ]; then
        mount -t sysfs sys /mnt/sysroot/sys && log "  - sys mounted" || log "  - WARNING: sys mount failed"
    fi

    if [ -d /mnt/sysroot/dev ]; then
        mount -o bind /dev /mnt/sysroot/dev && log "  - dev mounted" || log "  - WARNING: dev mount failed"
    fi

    log "=== GCE Rescue Auto-Mount Complete ==="
    echo "SUCCESS" > "$STATUS_FILE"

else
    log "ERROR: No supported filesystem found!"
    log "Supported: ext3, ext4, xfs, ntfs"
    log "Detected partitions:"
    lsblk -rf /dev/disk/by-id/google-${disk} | tee -a "$LOGFILE"
    echo "FAILED: Unsupported filesystem" > "$STATUS_FILE"
    exit 1
fi
