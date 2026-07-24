#!/bin/bash
# GCE Repair - GRUB fix script
#
# Runs after the original disk is mounted at /mnt/sysroot (proc/sys/dev
# already bind-mounted by the base script). Reinstalls the GRUB bootloader
# to the original disk and regenerates its configuration from a chroot,
# using the target's OWN grub tooling so the installed binaries always
# match the target distro (the rescue image's grub packages are never used).
#
# Firmware handling - the target's boot mode is a property of the TARGET
# disk (GCE picks BIOS vs UEFI from the boot disk's guest-os-features), and
# the rescue image can boot in a DIFFERENT mode than the original disk did,
# so /sys/firmware/efi on the rescue VM is not authoritative. The target is
# treated as UEFI when its disk carries an EFI System Partition:
#   BIOS: grub-install (Debian-family) / grub2-install (RHEL/SUSE-family)
#         to the whole disk, then config regeneration.
#   UEFI Debian-family: grub-install --efi-directory=/boot/efi --no-nvram
#         + regen. --no-nvram is required: the chroot's /sys is a fresh
#         sysfs with no efivarfs, so an NVRAM write would fail - and the
#         VM's existing NVRAM entry already points at the ESP files being
#         reinstalled, so skipping the NVRAM update is correct.
#   UEFI RHEL/SUSE-family: config regeneration ONLY. Their EFI binaries are
#         prebuilt signed images owned by packages (shim/grub2-efi);
#         grub2-install would replace them with unsigned ones and can break
#         Secure Boot. The regen target depends on the layout: RHEL 8.5+
#         keeps a redirect stub on the ESP pointing at /boot/grub2/grub.cfg,
#         while older layouts keep the FULL config on the ESP - the config
#         is written to the file the firmware actually reads.
#
# A grub-install failure does NOT skip config regeneration: regenerating
# grub.cfg is the fix for the most common breakage (missing/stale config)
# and must run even when the reinstall step fails. The overall result is
# still FAILED in that case so the user knows the reinstall did not land.
#
# Contract (see fstab_fix.sh): never call 'exit' - an exit here would abort
# the whole startup script, killing later fix scripts and the completion
# marker. Instead of deeply nested if/else, each stage is gated on
# FAIL_REASON being empty; the first failure records a reason and every
# later stage is skipped. Exactly ONE GCE-REPAIR-RESULT is emitted at the
# single result point at the bottom, after cleanup.

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

# Reduce multi-line tool output to one line usable as a FAILED reason
# (the result marker must stay on a single serial line).
last_line() {
    printf '%s\n' "$1" | tr -d '\r' | grep -v '^[[:space:]]*$' | tail -n 1 | cut -c1-200
}

# Run a command inside the sysroot chroot with a sane PATH.
# stdout+stderr are merged so the caller can log and report tool errors.
# GRUB_DISABLE_OS_PROBER=true: during rescue the RESCUE VM's boot disk is
# also attached, and os-prober would bake menu entries for it into the
# target's grub.cfg - entries that reference a disk that no longer exists
# after restore (observed live on Rocky 9).
run_chroot() {
    chroot "$SYSROOT" /bin/bash -c \
        "export PATH=/usr/sbin:/usr/bin:/sbin:/bin GRUB_DISABLE_OS_PROBER=true; $1" 2>&1
}

# Walk lsblk parents up to the whole-disk device.
# Handles plain partitions (sdb1 -> sdb, nvme0n1p1 -> nvme0n1) and stacked
# devices (LVM/dm: dm-0 -> sdb2 -> sdb). -d/--nodeps is REQUIRED: without
# it lsblk prints the device AND its children, and a whole-disk argument
# reports its own name as a child's pkname, looping forever.
parent_disk() {
    local dev="$1" parent guard=0
    parent=$(lsblk -dno pkname "$dev" 2>/dev/null | head -n 1)
    while [ -n "$parent" ] && [ "/dev/$parent" != "$dev" ] && [ "$guard" -lt 8 ]; do
        dev="/dev/$parent"
        parent=$(lsblk -dno pkname "$dev" 2>/dev/null | head -n 1)
        guard=$((guard + 1))
    done
    printf '%s\n' "$dev"
}

# First non-comment fstab device spec for a given mount point (or empty).
fstab_device_for() {
    awk -v mp="$1" '$1 !~ /^#/ && NF >= 2 && $2 == mp { print $1 }' \
        "$SYSROOT/etc/fstab" 2>/dev/null | head -n 1
}

# Resolve an fstab device spec (UUID=/LABEL=/PARTUUID=/PARTLABEL=/path) to a
# device node, ONLY accepting matches that live on the target disk: the
# rescue disk is built from the same image family as many targets, so
# filesystem UUIDs/labels can collide between the two disks, and resolving
# to a rescue-disk partition would make the repair modify the WRONG disk.
# Bare /dev/* specs name the TARGET's own layout, which maps to different
# devices on the rescue VM - they are remapped by partition number onto the
# rescued disk's stable by-id link ($disk comes from the base mount script).
resolve_device() {
    local spec="$1" tag="" value="" dev candidates partnum
    case "$spec" in
        UUID=*)      tag="UUID";      value="${spec#UUID=}" ;;
        LABEL=*)     tag="LABEL";     value="${spec#LABEL=}" ;;
        PARTUUID=*)  tag="PARTUUID";  value="${spec#PARTUUID=}" ;;
        PARTLABEL=*) tag="PARTLABEL"; value="${spec#PARTLABEL=}" ;;
        /dev/*)
            partnum=$(printf '%s' "$spec" | grep -oE '[0-9]+$')
            if [ -n "$partnum" ] \
                    && [ -e "/dev/disk/by-id/google-${disk}-part${partnum}" ]; then
                printf '%s\n' "/dev/disk/by-id/google-${disk}-part${partnum}"
                return 0
            fi
            return 1 ;;
        *)           return 1 ;;
    esac
    candidates=$(blkid -t "$tag=$value" -o device 2>/dev/null)
    for dev in $candidates; do
        if [ "$(parent_disk "$dev")" = "$DISK" ]; then
            printf '%s\n' "$dev"
            return 0
        fi
    done
    return 1
}

log "=== grub repair started ==="

FAIL_REASON=""
fixes=0
MOUNTED_BOOT=0
MOUNTED_EFI=0
BOUND_RUN=0
DISK=""
FAMILY=""
FIRMWARE="bios"
GRUB_CFG=""
CFG_INSIDE=""

# --- Guard: sysroot must be mounted and writable -------------------------
if ! mountpoint -q "$SYSROOT"; then
    FAIL_REASON="$SYSROOT is not mounted"
fi

if [ -z "$FAIL_REASON" ]; then
    if touch "$SYSROOT/.gce-repair-write-test" 2>/dev/null; then
        rm -f "$SYSROOT/.gce-repair-write-test"
    else
        FAIL_REASON="$SYSROOT is mounted read-only, cannot repair GRUB"
    fi
fi

# --- Detect distro family from the TARGET's os-release -------------------
if [ -z "$FAIL_REASON" ]; then
    if [ -f "$SYSROOT/etc/os-release" ]; then
        # Parse with sed instead of sourcing: never execute content that
        # came from the (potentially damaged) target disk.
        OS_ID=$(sed -n 's/^ID=//p' "$SYSROOT/etc/os-release" | tr -d '"' | head -n 1)
        OS_LIKE=$(sed -n 's/^ID_LIKE=//p' "$SYSROOT/etc/os-release" | tr -d '"' | head -n 1)
        ids=$(echo " $OS_ID $OS_LIKE " | tr '[:upper:]' '[:lower:]')
        case "$ids" in
            *debian*|*ubuntu*)        FAMILY="debian" ;;
            *rhel*|*fedora*|*centos*) FAMILY="rhel" ;;
            *suse*|*sles*)            FAMILY="suse" ;;
        esac
        if [ -z "$FAMILY" ]; then
            FAIL_REASON="unsupported distro family (ID=$OS_ID ID_LIKE=$OS_LIKE), GRUB not touched"
        else
            log "Detected distro family: $FAMILY (ID=$OS_ID ID_LIKE=$OS_LIKE)"
        fi
    else
        FAIL_REASON="cannot detect distro: $SYSROOT/etc/os-release not found"
    fi
fi

# --- Detect the target disk and ITS firmware mode ------------------------
if [ -z "$FAIL_REASON" ]; then
    src=$(findmnt -no SOURCE "$SYSROOT" 2>/dev/null)
    if [ -n "$src" ]; then
        DISK=$(parent_disk "$src")
    fi
    if [ -b "$DISK" ]; then
        log "Target disk: $DISK (sysroot source: $src)"
    else
        FAIL_REASON="could not determine target disk device for $SYSROOT"
    fi
fi

if [ -z "$FAIL_REASON" ]; then
    # The rescue image may boot in a different mode than the target disk
    # (GCE derives the mode from the boot disk's guest-os-features), so
    # /sys/firmware/efi reflects the RESCUE image, not the target. The
    # target's own EFI System Partition is the authoritative signal.
    esp_part=$(lsblk -pnro NAME,PARTTYPE "$DISK" 2>/dev/null \
        | awk '$2 == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b" {print $1; exit}')
    if [ -n "$esp_part" ]; then
        FIRMWARE="uefi"
    fi
    rescue_efi="no"
    [ -d /sys/firmware/efi ] && rescue_efi="yes"
    log "Target firmware: $FIRMWARE (ESP: ${esp_part:-none}, rescue VM EFI-booted: $rescue_efi)"
fi

# --- Mount separate /boot and /boot/efi inside the sysroot ---------------
# GCE Debian images are typically single-partition (no /boot entry), RHEL
# images carry separate /boot and /boot/efi. Skip cleanly when the entry is
# absent or the path is already mounted; only mounts WE create are undone.
if [ -z "$FAIL_REASON" ]; then
    spec=$(fstab_device_for /boot)
    if [ -n "$spec" ] && ! mountpoint -q "$SYSROOT/boot"; then
        dev=$(resolve_device "$spec")
        if [ -n "$dev" ] && mount "$dev" "$SYSROOT/boot" >>"$LOGFILE" 2>&1; then
            MOUNTED_BOOT=1
            log "Mounted separate /boot from $dev"
        else
            FAIL_REASON="could not mount separate /boot ($spec) inside $SYSROOT"
        fi
    fi
fi

if [ -z "$FAIL_REASON" ]; then
    spec=$(fstab_device_for /boot/efi)
    if [ -n "$spec" ] && ! mountpoint -q "$SYSROOT/boot/efi"; then
        dev=$(resolve_device "$spec")
        if [ -n "$dev" ] && mount "$dev" "$SYSROOT/boot/efi" >>"$LOGFILE" 2>&1; then
            MOUNTED_EFI=1
            log "Mounted EFI system partition from $dev"
        else
            FAIL_REASON="could not mount /boot/efi ($spec) inside $SYSROOT"
        fi
    fi
fi

# --- Bind /run: some grub tooling (os-prober, grub2-mkconfig) expects it --
# The base script binds only proc/sys/dev. A missing /run bind is not fatal.
if [ -z "$FAIL_REASON" ] && [ -d "$SYSROOT/run" ] && ! mountpoint -q "$SYSROOT/run"; then
    if mount --bind /run "$SYSROOT/run" >>"$LOGFILE" 2>&1; then
        BOUND_RUN=1
        log "Bind-mounted /run into $SYSROOT"
    else
        log "WARNING: could not bind-mount /run (continuing without it)"
    fi
fi

# --- Back up the existing grub.cfg before regenerating -------------------
if [ -z "$FAIL_REASON" ]; then
    if [ "$FAMILY" = "debian" ]; then
        GRUB_CFG="$SYSROOT/boot/grub/grub.cfg"
        CFG_INSIDE="/boot/grub/grub.cfg"
    else
        GRUB_CFG="$SYSROOT/boot/grub2/grub.cfg"
        CFG_INSIDE="/boot/grub2/grub.cfg"
        if [ "$FIRMWARE" = "uefi" ]; then
            # RHEL/SUSE UEFI layouts differ by generation: 8.5+ keeps a
            # small redirect stub on the ESP (contains 'configfile',
            # points at /boot/grub2/grub.cfg), older releases keep the
            # FULL config on the ESP and have no /boot/grub2/grub.cfg.
            # Regenerating the wrong file leaves the firmware reading a
            # stale config while the repair reports SUCCESS - write to
            # the file the firmware actually reads.
            for candidate in "$SYSROOT"/boot/efi/EFI/*/grub.cfg; do
                [ -f "$candidate" ] || continue
                if [ "$(wc -l < "$candidate")" -lt 20 ] \
                        && grep -q 'configfile' "$candidate"; then
                    log "ESP grub.cfg is a redirect stub - regenerating its target $CFG_INSIDE"
                else
                    GRUB_CFG="$candidate"
                    CFG_INSIDE="${candidate#$SYSROOT}"
                    log "Pre-stub UEFI layout - regenerating the ESP config $CFG_INSIDE"
                fi
                break
            done
        fi
    fi
    if [ -f "$GRUB_CFG" ]; then
        if cp "$GRUB_CFG" "$GRUB_CFG.gce-repair-backup" 2>>"$LOGFILE"; then
            log "Backup created: $GRUB_CFG.gce-repair-backup"
        else
            FAIL_REASON="could not back up existing grub.cfg at $GRUB_CFG"
        fi
    else
        log "No existing grub.cfg at $GRUB_CFG (nothing to back up)"
    fi
fi

# --- Reinstall the GRUB binaries (only where correct) ---------------------
# A reinstall failure is recorded in INSTALL_FAIL instead of FAIL_REASON so
# config regeneration below STILL RUNS - regenerating grub.cfg is the fix
# for the most common breakage and must not be skipped. The final result is
# still FAILED when INSTALL_FAIL is set.
INSTALL_FAIL=""
if [ -z "$FAIL_REASON" ]; then
    if [ "$FIRMWARE" = "bios" ]; then
        if [ "$FAMILY" = "debian" ]; then
            INSTALL_CMD="grub-install $DISK"
        else
            INSTALL_CMD="grub2-install $DISK"
        fi
        log "Reinstalling GRUB: chroot $SYSROOT $INSTALL_CMD"
        output=$(run_chroot "$INSTALL_CMD")
        rc=$?
        echo "$output" >> "$LOGFILE"
        if [ $rc -eq 0 ]; then
            fixes=$((fixes + 1))
            repair_line "[FIXED] grub: Reinstalled GRUB to $DISK (BIOS)"
        else
            INSTALL_FAIL="grub-install failed: $(last_line "$output")"
        fi
    elif [ "$FAMILY" = "debian" ]; then
        if mountpoint -q "$SYSROOT/boot/efi"; then
            # --no-nvram: the chroot's fresh sysfs has no efivarfs, so the
            # efibootmgr NVRAM update would fail the whole install - and
            # the VM's existing NVRAM entry already points at the ESP
            # files being reinstalled, so the update is unnecessary.
            log "Reinstalling GRUB (UEFI): grub-install --efi-directory=/boot/efi --no-nvram"
            output=$(run_chroot "grub-install --efi-directory=/boot/efi --no-nvram")
            rc=$?
            echo "$output" >> "$LOGFILE"
            if [ $rc -eq 0 ]; then
                fixes=$((fixes + 1))
                repair_line "[FIXED] grub: Reinstalled GRUB to the EFI system partition"
            else
                INSTALL_FAIL="grub-install failed: $(last_line "$output")"
            fi
        else
            # Conservative: without a mounted ESP a UEFI grub-install would
            # write to the wrong place. Config regeneration alone still
            # repairs missing/stale grub.cfg cases.
            repair_line "grub: No EFI system partition mounted at /boot/efi - skipped GRUB reinstall, regenerating config only"
        fi
    else
        # RHEL/SUSE-family UEFI: signed, package-managed EFI binaries.
        repair_line "grub: UEFI $FAMILY system - GRUB binaries left untouched (package-managed signed images), regenerating config only"
    fi
fi

# --- Regenerate the GRUB configuration ------------------------------------
if [ -z "$FAIL_REASON" ]; then
    if [ "$FAMILY" = "debian" ]; then
        REGEN_CMD="update-grub"
    else
        REGEN_CMD="grub2-mkconfig -o $CFG_INSIDE"
    fi
    log "Regenerating GRUB config: chroot $SYSROOT $REGEN_CMD"
    output=$(run_chroot "$REGEN_CMD")
    rc=$?
    if [ $rc -ne 0 ] && [ "$FAMILY" = "debian" ] \
            && echo "$output" | grep -q 'command not found'; then
        # Minimal Debian-family images may lack the update-grub wrapper.
        REGEN_CMD="grub-mkconfig -o $CFG_INSIDE"
        log "update-grub not found, falling back to: $REGEN_CMD"
        output=$(run_chroot "$REGEN_CMD")
        rc=$?
    fi
    echo "$output" >> "$LOGFILE"
    if [ $rc -eq 0 ]; then
        fixes=$((fixes + 1))
        repair_line "[FIXED] grub: Regenerated GRUB configuration ($CFG_INSIDE)"
    else
        FAIL_REASON="GRUB config regeneration failed: $(last_line "$output")"
        # A failed regeneration can leave a truncated grub.cfg - worse than
        # the pre-repair state. Put the backup back so the disk is no worse
        # off; the result stays FAILED so nobody mistakes this for a fix.
        if [ -f "$GRUB_CFG.gce-repair-backup" ]; then
            if cp "$GRUB_CFG.gce-repair-backup" "$GRUB_CFG" 2>>"$LOGFILE"; then
                repair_line "grub: Regeneration failed - restored the pre-repair grub.cfg from backup"
            fi
        fi
    fi
fi

# --- Cleanup: undo OUR mounts in reverse order so restore stays clean -----
# Runs even after a failure so the VM is never left with extra mounts.
if [ "$BOUND_RUN" -eq 1 ]; then
    umount "$SYSROOT/run" >>"$LOGFILE" 2>&1 || log "WARNING: could not unmount $SYSROOT/run"
fi
if [ "$MOUNTED_EFI" -eq 1 ]; then
    umount "$SYSROOT/boot/efi" >>"$LOGFILE" 2>&1 \
        || log "WARNING: could not unmount $SYSROOT/boot/efi"
fi
if [ "$MOUNTED_BOOT" -eq 1 ]; then
    umount "$SYSROOT/boot" >>"$LOGFILE" 2>&1 \
        || log "WARNING: could not unmount $SYSROOT/boot"
fi

log "=== grub repair completed: $fixes action(s), install failure: '${INSTALL_FAIL:-none}', failure reason: '${FAIL_REASON:-none}' ==="

# --- Single result emission point -----------------------------------------
# Diagnosis only schedules this script when GRUB errors were seen on the
# serial console, so the repair is always attempted: the outcome is either
# SUCCESS (reinstall and/or regeneration ran) or FAILED - NO_ISSUES is not
# an expected outcome for this category. An install failure is FAILED even
# when the config regeneration succeeded: the user must know the reinstall
# did not land (the regen result is visible in the fix lines).
if [ -n "$FAIL_REASON" ] && [ -n "$INSTALL_FAIL" ]; then
    repair_result "FAILED:$INSTALL_FAIL; $FAIL_REASON"
elif [ -n "$FAIL_REASON" ]; then
    repair_result "FAILED:$FAIL_REASON"
elif [ -n "$INSTALL_FAIL" ]; then
    repair_result "FAILED:$INSTALL_FAIL (GRUB config regeneration still completed)"
else
    repair_result "SUCCESS:$fixes"
fi

# Copy full log (mount + repair) to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null
