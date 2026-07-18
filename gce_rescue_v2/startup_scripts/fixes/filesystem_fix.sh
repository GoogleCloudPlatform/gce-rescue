#!/bin/bash
# GCE Repair - filesystem fix script (fsck before mount)
#
# Filesystem corruption (bad superblock, XFS log damage, btrfs tree errors)
# usually makes the original disk unmountable, so the repair has to run
# BEFORE the base mount script's mount attempt. The block between the
# PREMOUNT markers below is lifted out by the composer and injected right
# after the disk-wait loop, while the filesystem is still unmounted (the
# only safe time to run fsck).
#
# Safety model: the pre-rescue snapshot taken by the rescue flow is the
# rollback point for anything fsck changes. Even so, this script stays
# conservative: xfs_repair -L (discards pending log transactions) and
# btrfs check --repair are never run automatically - those stay manual.
#
# Scope: the pre-mount block sees log(), $LOGFILE and $disk (device is
# /dev/disk/by-id/google-${disk}); /mnt/sysroot is NOT mounted yet. The
# post-mount part at the bottom additionally sees the mounted /mnt/sysroot.
# Both parts run in the same shell process, so plain FSFIX_* variables
# carry state from the pre-mount block to the result decision.

# === GCE-REPAIR-PREMOUNT-BEGIN ===
FSFIX_FIXED=0
FSFIX_FAIL_REASONS=""

# Prefixed helpers: redefining log() here would tag the rest of the base
# mount script's output with [REPAIR], so the pre-mount block uses its own.
fsfix_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [REPAIR] $1" | tee -a "$LOGFILE"
}

fsfix_line() {
    echo "GCE-REPAIR-LINE:$1" >&2
    fsfix_log "$1"
}

fsfix_add_failure() {
    if [ -z "$FSFIX_FAIL_REASONS" ]; then
        FSFIX_FAIL_REASONS="$1"
    else
        FSFIX_FAIL_REASONS="$FSFIX_FAIL_REASONS; $1"
    fi
    # Also emit a LINE marker right away: when the disk still fails to
    # mount, the base script exits before the post-mount result marker
    # runs, and these lines are the only way the reason reaches the user.
    echo "GCE-REPAIR-LINE:[ERROR] filesystem: $1" >&2
    fsfix_log "ERROR: $1"
}

# Ensure a tool exists, installing its package if the rescue image lacks it.
# $1 = command, $2 = Debian package. Returns non-zero if still unavailable.
fsfix_ensure_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fsfix_log "$1 not found on rescue image, installing $2..."
        DEBIAN_FRONTEND=noninteractive timeout 60 apt-get update \
            >> "$LOGFILE" 2>&1 </dev/null
        DEBIAN_FRONTEND=noninteractive timeout 120 apt-get install -y "$2" \
            >> "$LOGFILE" 2>&1 </dev/null
    fi
    command -v "$1" >/dev/null 2>&1
}

fsfix_check_ext() {
    local dev="$1" fstype="$2" rc
    if ! command -v e2fsck >/dev/null 2>&1; then
        fsfix_add_failure "e2fsck not available to repair $dev; run fsck manually from rescue mode"
    else
        fsfix_log "Checking $dev ($fstype) with e2fsck -f -y..."
        e2fsck -f -y "$dev" >> "$LOGFILE" 2>&1 </dev/null
        rc=$?
        # e2fsck status is a bitmask: 0 = clean, 1 = errors corrected,
        # 2 = corrected + reboot needed (3 = both), >= 4 = errors remain.
        if [ "$rc" -eq 0 ]; then
            fsfix_log "$dev: filesystem clean, no repair needed"
        elif [ "$rc" -le 3 ]; then
            FSFIX_FIXED=$((FSFIX_FIXED + 1))
            fsfix_line "[FIXED] filesystem: e2fsck repaired errors on $dev ($fstype)"
        else
            fsfix_add_failure "e2fsck could not repair $dev (status $rc); from rescue mode try a backup superblock (fsck -b 32768 -y $dev) or restore from the snapshot"
        fi
    fi
}

fsfix_check_xfs() {
    local dev="$1" out rc tmpmnt
    if ! fsfix_ensure_tool xfs_repair xfsprogs; then
        fsfix_add_failure "xfs_repair unavailable (xfsprogs install failed) so $dev was not repaired; install xfsprogs and run xfs_repair from rescue mode"
    else
        # Dry check first: status 0 means the filesystem is clean.
        out=$(xfs_repair -n "$dev" 2>&1 </dev/null)
        rc=$?
        echo "$out" >> "$LOGFILE"
        if [ "$rc" -eq 0 ]; then
            fsfix_log "$dev: XFS clean, no repair needed"
        else
            fsfix_log "Repairing $dev (xfs) with xfs_repair..."
            out=$(xfs_repair "$dev" 2>&1 </dev/null)
            rc=$?
            echo "$out" >> "$LOGFILE"
            if [ "$rc" -ne 0 ] && echo "$out" | grep -qi 'replay the log\|needs to be replayed'; then
                # Dirty log: replay it with a mount/unmount cycle and retry
                # once. Never fall back to -L automatically - zeroing the
                # log destroys the transactions it holds.
                fsfix_log "Dirty XFS log on $dev, replaying via mount/unmount cycle..."
                tmpmnt=$(mktemp -d)
                mount -o nouuid "$dev" "$tmpmnt" >> "$LOGFILE" 2>&1 </dev/null
                if mountpoint -q "$tmpmnt"; then
                    umount "$tmpmnt" >> "$LOGFILE" 2>&1
                fi
                rmdir "$tmpmnt" 2>/dev/null
                out=$(xfs_repair "$dev" 2>&1 </dev/null)
                rc=$?
                echo "$out" >> "$LOGFILE"
            fi
            if [ "$rc" -eq 0 ]; then
                FSFIX_FIXED=$((FSFIX_FIXED + 1))
                fsfix_line "[FIXED] filesystem: xfs_repair completed on $dev"
            else
                fsfix_add_failure "xfs_repair could not repair $dev (status $rc); manual last resort from rescue mode is xfs_repair -L $dev, which discards the last log transactions"
            fi
        fi
    fi
}

# A wiped/corrupt primary superblock leaves lsblk FSTYPE EMPTY: libblkid
# only probes primary superblocks, so the category's flagship case
# (filesystem_bad_superblock) shows up as "no filesystem" here. Attempt
# ext backup-superblock recovery instead of skipping. e2fsck -b on a
# device that was never ext fails cleanly (status 8) without writing.
fsfix_check_blank() {
    local dev="$1" rc sb
    if ! command -v e2fsck >/dev/null 2>&1; then
        fsfix_log "Skipping $dev (no filesystem signature; e2fsck unavailable)"
    else
        for sb in 32768 8193; do  # 4k then 1k block-size backup locations
            fsfix_log "No filesystem signature on $dev - trying ext backup superblock $sb..."
            e2fsck -b "$sb" -y "$dev" >> "$LOGFILE" 2>&1 </dev/null
            rc=$?
            if [ "$rc" -le 3 ]; then
                FSFIX_FIXED=$((FSFIX_FIXED + 1))
                fsfix_line "[FIXED] filesystem: recovered $dev from ext backup superblock $sb"
                return
            fi
            if [ "$rc" -ne 8 ]; then
                # 8 = operational error (no backup superblock there either,
                # keep probing); anything else means e2fsck engaged the
                # filesystem but could not repair it.
                fsfix_add_failure "e2fsck -b $sb could not repair $dev (status $rc); restore from the snapshot or run fsck manually from rescue mode"
                return
            fi
        done
        # LINE marker (not just a log): if the disk then fails to mount,
        # this is the only trace of WHY that reaches the user.
        fsfix_line "[WARNING] filesystem: no filesystem signature on $dev and no ext backup superblock - not repaired (if this held the root filesystem, restore from the snapshot)"
    fi
}

fsfix_check_btrfs() {
    local dev="$1" rc
    if ! fsfix_ensure_tool btrfs btrfs-progs; then
        fsfix_log "WARNING: btrfs-progs unavailable, cannot check $dev"
    else
        fsfix_log "Running read-only btrfs check on $dev..."
        btrfs check --readonly "$dev" >> "$LOGFILE" 2>&1 </dev/null
        rc=$?
        if [ "$rc" -eq 0 ]; then
            fsfix_log "$dev: btrfs clean, no repair needed"
        else
            # Automatic btrfs repair is unsafe - report findings only.
            fsfix_line "[INFO] filesystem: btrfs errors found on $dev (read-only check only, not auto-repaired)"
            fsfix_add_failure "btrfs errors on $dev were not auto-repaired; from rescue mode try mount -o ro,rescue=usebackuproot, run btrfs check manually, or restore from the snapshot"
        fi
    fi
}

# Live-probe a device's filesystem type. lsblk's FSTYPE column reads the
# udev/libblkid CACHE, which is often not yet populated for a disk that was
# hot-attached moments earlier - observed live: an XFS root typed as blank
# 200ms after attach, misrouting it away from xfs_repair. blkid -p probes
# the device directly, bypassing the cache.
fsfix_probe_type() {
    local dev="$1" t
    t=$(blkid -p -o value -s TYPE "$dev" 2>/dev/null | head -1)
    if [ -z "$t" ]; then
        t=$(blkid -o value -s TYPE "$dev" 2>/dev/null | head -1)
    fi
    printf '%s' "$t"
}

fsfix_log "=== filesystem repair (pre-mount) started ==="

# Let udev finish processing the freshly-attached disk before enumerating
# (best effort; the per-device live probe below is the real safety net).
udevadm settle --timeout=30 2>/dev/null

fsfix_disk_path="/dev/disk/by-id/google-${disk}"

# Enumerate the disk and its partitions with filesystem types. -p gives full
# device paths; a filesystem written directly on the whole disk (no partition
# table) shows up as the disk row with a non-empty FSTYPE and is handled too.
fsfix_devlist=$(lsblk -pnro NAME,FSTYPE "$fsfix_disk_path" 2>/dev/null)
fsfix_log "Block devices on $fsfix_disk_path:"
fsfix_log "$fsfix_devlist"

fsfix_disk_real=$(readlink -f "$fsfix_disk_path" 2>/dev/null)
fsfix_row_count=$(printf '%s\n' "$fsfix_devlist" | grep -c .)

# Read the list on fd 3 so fsck/mount/apt-get inside the loop cannot consume
# the remaining lines from the loop's stdin.
while IFS=' ' read -r fsfix_dev fsfix_fstype <&3; do
    if [ -z "$fsfix_dev" ]; then
        continue
    fi
    if [ -z "$fsfix_fstype" ]; then
        # The whole-disk row has an empty FSTYPE whenever a partition table
        # exists - only partitions (or an unpartitioned whole disk) can
        # hold the wiped superblock this recovers from.
        if [ "$fsfix_dev" = "$fsfix_disk_real" ] && [ "$fsfix_row_count" -gt 1 ]; then
            continue
        fi
        # Raw-by-design partitions must not be probed: a bios_grub
        # partition (GCE Debian sda14) or any tiny partition never held a
        # root filesystem, and a repurposed partition with STALE ext
        # backup superblocks could be spuriously "recovered" by e2fsck -b
        # (observed live: e2e probe of the 3MB bios_grub partition).
        fsfix_parttype=$(lsblk -pnro PARTTYPE "$fsfix_dev" 2>/dev/null \
            | head -1 | tr '[:upper:]' '[:lower:]')
        if [ "$fsfix_parttype" = "21686148-6449-6e6f-744e-656564454649" ]; then
            fsfix_log "Skipping $fsfix_dev: bios_grub partition (raw by design)"
            continue
        fi
        fsfix_size_b=$(lsblk -pnrbo SIZE "$fsfix_dev" 2>/dev/null | head -1)
        if [ -n "$fsfix_size_b" ] && [ "$fsfix_size_b" -lt 67108864 ]; then
            fsfix_log "Skipping $fsfix_dev: smaller than 64MB (not a recoverable filesystem)"
            continue
        fi
        # The blank FSTYPE may just be a stale udev cache on the freshly
        # attached disk - live-probe before treating it as signatureless.
        fsfix_fstype=$(fsfix_probe_type "$fsfix_dev")
        if [ -n "$fsfix_fstype" ]; then
            fsfix_log "Live probe typed $fsfix_dev as $fsfix_fstype (udev cache was stale)"
        else
            fsfix_check_blank "$fsfix_dev"
            continue
        fi
    fi
    case "$fsfix_fstype" in
        ext2|ext3|ext4)
            fsfix_check_ext "$fsfix_dev" "$fsfix_fstype"
            ;;
        xfs)
            fsfix_check_xfs "$fsfix_dev"
            ;;
        btrfs)
            fsfix_check_btrfs "$fsfix_dev"
            ;;
        *)
            fsfix_log "Skipping $fsfix_dev ($fsfix_fstype): not handled by filesystem repair"
            ;;
    esac
done 3<<< "$fsfix_devlist"

fsfix_log "=== filesystem repair (pre-mount) finished: $FSFIX_FIXED partition(s) repaired ==="
# === GCE-REPAIR-PREMOUNT-END ===

# Post-mount part: runs after the base script mounted /mnt/sysroot. Decides
# the single final result from the state the pre-mount block recorded.

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

log "=== filesystem repair result ==="

if [ -n "${FSFIX_FAIL_REASONS:-}" ]; then
    repair_result "FAILED:$FSFIX_FAIL_REASONS"
elif [ "${FSFIX_FIXED:-0}" -gt 0 ]; then
    if mountpoint -q "$SYSROOT"; then
        repair_result "SUCCESS:$FSFIX_FIXED"
    else
        repair_result "FAILED:fsck repaired ${FSFIX_FIXED} partition(s) but $SYSROOT is still not mounted"
    fi
else
    repair_result "NO_ISSUES:0"
fi

# Copy full log (mount + repair) to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null
