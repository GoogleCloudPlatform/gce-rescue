#!/bin/bash
# GCE Repair - initramfs fix script (chroot-based rebuild)
#
# Runs after the affected disk is mounted at /mnt/sysroot with proc/sys/dev
# bound for chroot support. Rebuilds the initramfs for the newest installed
# kernel using the TARGET disk's own tooling inside the chroot:
#   Debian-family: update-initramfs -c|-u -k VERSION
#   RHEL-family:   dracut -f /boot/initramfs-VERSION.img VERSION
#   SUSE-family:   dracut -f /boot/initrd-VERSION VERSION
# then regenerates the GRUB config so boot entries reference the rebuilt
# image (harmless if the grub fix also runs later - it redoes the regen).
#
# Separate /boot and /boot/efi partitions from the target's fstab are
# mounted inside the sysroot before chrooting and unmounted in reverse
# order at the end. /run is bind-mounted for tools that expect it.

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

# Squash multi-line tool output into one short line (FAILED reasons must
# be a single line).
one_line() {
    printf '%s' "$1" | tr '\n' ' ' | sed 's/  */ /g' | cut -c1-300
}

# Mounts made by this script, newest first, so iterating the list
# unmounts in reverse order of mounting.
INITRAMFS_UNWIND_MOUNTS=""

unwind_mounts() {
    local m
    for m in $INITRAMFS_UNWIND_MOUNTS; do
        if umount "$m" 2>/dev/null; then
            log "Unmounted $m"
        else
            log "WARNING: could not unmount $m"
        fi
    done
    INITRAMFS_UNWIND_MOUNTS=""
}

# Resolve UUID=/LABEL=/PARTUUID=/PARTLABEL= specs against the RESCUED
# disk's partitions ONLY. mount(8) resolves these tags across every disk
# on the VM, and the rescue disk is often built from the same image family
# as the target, so tag values can collide - resolving globally could
# mount (and rebuild the initramfs on) the rescue disk itself.
resolve_on_target_disk() {
    local spec="$1" tag="${1%%=*}" value="${1#*=}" part found
    for part in /dev/disk/by-id/google-"${disk}"-part*; do
        [ -e "$part" ] || continue
        found=$(blkid -o value -s "$tag" "$part" 2>/dev/null)
        if [ "$found" = "$value" ]; then
            printf '%s\n' "$part"
            return 0
        fi
    done
    return 1
}

# Mount the target's fstab entry for a mount point (/boot, /boot/efi)
# inside the sysroot. Bare /dev/* specs are remapped onto the rescued disk
# via its /dev/disk/by-id/google-<disk> partition links, because the
# target's /dev/sdX names refer to DIFFERENT disks on the rescue VM;
# UUID=/LABEL= specs are resolved against the rescued disk's partitions
# only (see resolve_on_target_disk).
# Returns: 0 = mounted or already mounted, 1 = no fstab entry, 2 = failed.
mount_sysroot_boot_entry() {
    local mp="$1"
    local entry spec fstype opts partnum resolved

    if mountpoint -q "${SYSROOT}${mp}"; then
        return 0
    fi

    entry=$(awk -v mp="$mp" \
        '$0 !~ /^[ \t]*#/ && NF >= 3 && $2 == mp {print $1" "$3" "$4}' \
        "$SYSROOT/etc/fstab" | head -1)
    if [ -z "$entry" ]; then
        return 1
    fi

    spec=$(echo "$entry" | awk '{print $1}')
    fstype=$(echo "$entry" | awk '{print $2}')
    opts=$(echo "$entry" | awk '{print $3}')
    if [ -z "$opts" ]; then
        opts="defaults"
    fi

    case "$spec" in
        /dev/*)
            partnum=$(echo "$spec" | grep -oE '[0-9]+$')
            if [ -n "$partnum" ] && \
               [ -e "/dev/disk/by-id/google-${disk}-part${partnum}" ]; then
                log "Remapping $spec -> /dev/disk/by-id/google-${disk}-part${partnum}"
                spec="/dev/disk/by-id/google-${disk}-part${partnum}"
            else
                log "WARNING: cannot remap $spec onto the rescued disk"
                return 2
            fi
            ;;
        UUID=*|LABEL=*|PARTUUID=*|PARTLABEL=*)
            resolved=$(resolve_on_target_disk "$spec")
            if [ -n "$resolved" ]; then
                log "Resolved $spec -> $resolved (on the rescued disk)"
                spec="$resolved"
            else
                log "WARNING: $spec not found on the rescued disk - refusing to mount a foreign device"
                return 2
            fi
            ;;
    esac

    mkdir -p "${SYSROOT}${mp}"
    log "Mounting $spec ($fstype) at ${SYSROOT}${mp}..."
    mount -t "$fstype" -o "$opts" "$spec" "${SYSROOT}${mp}" 2>&1 | tee -a "$LOGFILE"
    if mountpoint -q "${SYSROOT}${mp}"; then
        INITRAMFS_UNWIND_MOUNTS="${SYSROOT}${mp} ${INITRAMFS_UNWIND_MOUNTS}"
        return 0
    fi
    return 2
}

# Distro family from the TARGET's os-release (grep, not source - never
# execute content read from the broken disk).
detect_family() {
    local ids
    if [ -f "$SYSROOT/etc/os-release" ]; then
        ids=$(grep -E '^(ID|ID_LIKE)=' "$SYSROOT/etc/os-release" \
            | tr -d '"' | tr '[:upper:]' '[:lower:]' | tr '\n' ' ')
        case " $ids " in
            *debian*|*ubuntu*) echo "debian"; return 0 ;;
            *rhel*|*fedora*|*centos*|*rocky*|*alma*) echo "rhel"; return 0 ;;
            *suse*|*sles*) echo "suse"; return 0 ;;
        esac
    fi
    # Fallback: probe for the target's own rebuild tool
    if chroot "$SYSROOT" sh -c 'command -v update-initramfs' >/dev/null 2>&1; then
        echo "debian"
    elif chroot "$SYSROOT" sh -c 'command -v dracut' >/dev/null 2>&1; then
        echo "rhel"
    fi
    return 0
}

# Best-effort GRUB config regeneration so entries reference the rebuilt
# initrd. Never fatal: the grub fix (composed after this script) redoes
# the regeneration with full firmware awareness.
regen_grub_config() {
    local cfg out rc candidate
    # GRUB_DISABLE_OS_PROBER=true: the rescue VM's own boot disk is attached
    # during repair, and os-prober would bake dead menu entries for it into
    # the target's config (observed live on Rocky 9).
    if chroot "$SYSROOT" sh -c 'command -v update-grub' >/dev/null 2>&1; then
        log "Regenerating GRUB config: update-grub"
        out=$(chroot "$SYSROOT" sh -c 'GRUB_DISABLE_OS_PROBER=true update-grub' 2>&1)
        rc=$?
    elif chroot "$SYSROOT" sh -c 'command -v grub2-mkconfig' >/dev/null 2>&1; then
        cfg="/boot/grub2/grub.cfg"
        if [ ! -f "${SYSROOT}${cfg}" ]; then
            for candidate in "$SYSROOT"/boot/efi/EFI/*/grub.cfg; do
                if [ -f "$candidate" ]; then
                    # An RHEL 8.5+/9 ESP grub.cfg is a small redirect stub
                    # (contains 'configfile', points at /boot/grub2/grub.cfg).
                    # NEVER overwrite the stub with a full config - keep the
                    # /boot/grub2 target instead; mkconfig -o creates it and
                    # the stub then finds it. Only a pre-stub FULL config on
                    # the ESP is regenerated in place.
                    if [ "$(wc -l < "$candidate")" -lt 20 ] \
                            && grep -q 'configfile' "$candidate"; then
                        log "ESP grub.cfg is a redirect stub - regenerating its target $cfg"
                    else
                        cfg="${candidate#$SYSROOT}"
                    fi
                    break
                fi
            done
        fi
        log "Regenerating GRUB config: grub2-mkconfig -o $cfg"
        out=$(chroot "$SYSROOT" sh -c "GRUB_DISABLE_OS_PROBER=true grub2-mkconfig -o $cfg" 2>&1)
        rc=$?
    else
        log "WARNING: no GRUB config tool found in the target - skipping regeneration"
        return 0
    fi
    printf '%s\n' "$out" >> "$LOGFILE"
    if [ "$rc" -ne 0 ]; then
        log "WARNING: GRUB config regeneration failed (rc=$rc): $(one_line "$out")"
    else
        log "GRUB config regenerated"
    fi
    return 0
}

log "=== initramfs repair started ==="

final_result=""
fixes=0

# Guard chain (no early return: the single repair_result call at the end
# reports whatever outcome the guards recorded)
if ! mountpoint -q "$SYSROOT"; then
    final_result="FAILED:sysroot is not mounted at $SYSROOT"
else
    if ! touch "$SYSROOT/.gce-repair-rw-probe" 2>/dev/null; then
        final_result="FAILED:$SYSROOT is mounted read-only - cannot rebuild the initramfs"
    else
        rm -f "$SYSROOT/.gce-repair-rw-probe"

        family=$(detect_family)
        if [ -z "$family" ]; then
            final_result="FAILED:could not determine distro family from $SYSROOT/etc/os-release"
        else
            log "Detected distro family: $family"

            # /lib/modules enumerates the installed kernels; the final
            # version choice happens AFTER /boot is mounted, so it can be
            # validated against the kernel images actually present there.
            modules_dir="$SYSROOT/lib/modules"
            if [ ! -d "$modules_dir" ]; then
                modules_dir="$SYSROOT/usr/lib/modules"
            fi
            kernel_list=$(ls -1 "$modules_dir" 2>/dev/null | sort -V)
            kernel_count=$(printf '%s\n' "$kernel_list" | grep -c .)

            if [ -z "$kernel_list" ]; then
                final_result="FAILED:no installed kernels found under $modules_dir"
            else
                log "Installed kernels under $(basename "$modules_dir"): $kernel_count"

                mount_sysroot_boot_entry /boot
                if [ $? -eq 2 ]; then
                    final_result="FAILED:could not mount the separate /boot partition from the target fstab"
                elif [ ! -d "$SYSROOT/boot" ]; then
                    final_result="FAILED:no /boot directory on the target disk"
                else
                    # ESP and /run are best-effort (only GRUB regen and
                    # some dracut modules benefit from them)
                    mount_sysroot_boot_entry /boot/efi
                    if [ $? -eq 2 ]; then
                        log "WARNING: could not mount /boot/efi - continuing without it"
                    fi
                    if [ -d "$SYSROOT/run" ] && ! mountpoint -q "$SYSROOT/run"; then
                        if mount -o bind /run "$SYSROOT/run" 2>/dev/null; then
                            INITRAMFS_UNWIND_MOUNTS="$SYSROOT/run $INITRAMFS_UNWIND_MOUNTS"
                            log "Bound /run into the sysroot"
                        fi
                    fi

                    # Pick the newest kernel that has a matching boot image:
                    # stale /lib/modules leftovers ('apt remove' without
                    # purge) sort NEWER than the real kernel, and rebuilding
                    # an initrd no boot entry references fixes nothing.
                    kver=""
                    for candidate_ver in $(printf '%s\n' "$kernel_list" | sort -Vr); do
                        if [ -e "$SYSROOT/boot/vmlinuz-$candidate_ver" ] \
                                || [ -e "$SYSROOT/boot/Image-$candidate_ver" ]; then
                            kver="$candidate_ver"
                            break
                        fi
                    done
                    if [ -z "$kver" ]; then
                        kver=$(printf '%s\n' "$kernel_list" | tail -1)
                        repair_line "[WARNING] initramfs: no kernel image in /boot matches any /lib/modules version - rebuilding for $kver anyway"
                    fi
                    log "Kernel selected for rebuild: $kver"

                    case "$family" in
                        debian) initrd_rel="/boot/initrd.img-${kver}" ;;
                        rhel)   initrd_rel="/boot/initramfs-${kver}.img" ;;
                        *)      initrd_rel="/boot/initrd-${kver}" ;;
                    esac
                    initrd_abs="${SYSROOT}${initrd_rel}"

                    # Free-space guard: initramfs rebuilds commonly fail
                    # on a full /boot
                    boot_df=$(df -Pk "$SYSROOT/boot" 2>/dev/null | tail -1)
                    boot_use=$(echo "$boot_df" | awk '{print $5}' | tr -d '%')
                    boot_avail_kb=$(echo "$boot_df" | awk '{print $4}')
                    boot_use=${boot_use:-0}
                    boot_avail_kb=${boot_avail_kb:-0}
                    log "/boot usage: ${boot_use}% (${boot_avail_kb} KB free)"

                    # Back up the existing image unless space is tight -
                    # images run 50-100MB, and a rebuild failing on a full
                    # disk is worse than no backup; the pre-rescue snapshot
                    # is the real safety net.
                    if [ -f "$initrd_abs" ]; then
                        initrd_kb=$(du -k "$initrd_abs" 2>/dev/null | awk '{print $1}')
                        initrd_kb=${initrd_kb:-0}
                        if [ "$boot_use" -gt 90 ] || [ "$boot_avail_kb" -le "$initrd_kb" ]; then
                            repair_line "[WARNING] initramfs: /boot is ${boot_use}% full - skipped backup of $(basename "$initrd_rel") (pre-rescue snapshot is the rollback)"
                        else
                            if cp "$initrd_abs" "${initrd_abs}.gce-repair-backup" 2>/dev/null; then
                                log "Backup created: ${initrd_rel}.gce-repair-backup"
                            else
                                log "WARNING: could not back up $initrd_rel - continuing"
                            fi
                        fi
                    else
                        log "No existing image at $initrd_rel - creating a new one"
                    fi

                    if [ "$family" = "debian" ]; then
                        if [ -f "$initrd_abs" ]; then
                            rebuild_cmd="update-initramfs -u -k ${kver}"
                        else
                            rebuild_cmd="update-initramfs -c -k ${kver}"
                        fi
                    else
                        rebuild_cmd="dracut -f ${initrd_rel} ${kver}"
                    fi

                    log "Rebuilding initramfs: chroot $SYSROOT $rebuild_cmd"
                    tool_output=$(chroot "$SYSROOT" $rebuild_cmd 2>&1)
                    rebuild_rc=$?
                    printf '%s\n' "$tool_output" >> "$LOGFILE"

                    initrd_backup="${initrd_abs}.gce-repair-backup"
                    if [ "$rebuild_rc" -ne 0 ]; then
                        if echo "$tool_output" | grep -qi 'no space left on device'; then
                            final_result="FAILED:initramfs rebuild failed: disk_full - no space left on device while writing ${initrd_rel}"
                        else
                            final_result="FAILED:initramfs rebuild failed (rc=$rebuild_rc): $(one_line "$tool_output")"
                        fi
                    elif [ ! -s "$initrd_abs" ]; then
                        final_result="FAILED:rebuild reported success but ${initrd_rel} is missing or empty"
                    else
                        fixes=1
                        repair_line "[FIXED] initramfs: Rebuilt $(basename "$initrd_rel") for kernel $kver ($family family)"
                        if [ "$kernel_count" -gt 1 ]; then
                            repair_line "[SKIPPED] initramfs: $((kernel_count - 1)) older kernel(s) not rebuilt - the newest kernel is sufficient to boot"
                        fi
                        regen_grub_config
                        final_result="SUCCESS:$fixes"
                    fi

                    # Backup lifecycle: a failed rebuild can leave the image
                    # missing/truncated - put the old one back so the disk is
                    # no worse off. On success the backup is deleted: initrd
                    # images run 50-100MB and would otherwise sit on the
                    # restored disk forever (the snapshot is the rollback).
                    if [ -f "$initrd_backup" ]; then
                        case "$final_result" in
                            SUCCESS:*)
                                rm -f "$initrd_backup"
                                log "Removed temporary initramfs backup"
                                ;;
                            *)
                                if [ ! -s "$initrd_abs" ]; then
                                    if cp "$initrd_backup" "$initrd_abs" 2>/dev/null; then
                                        log "Restored previous initramfs from backup after failed rebuild"
                                        rm -f "$initrd_backup"
                                    else
                                        log "WARNING: could not restore initramfs backup - left at $initrd_backup"
                                    fi
                                else
                                    log "Initramfs backup left at $initrd_backup for manual recovery"
                                fi
                                ;;
                        esac
                    fi
                fi
            fi
        fi
    fi
fi

# Clean up mounts made by this script (reverse order), then report the
# single result marker
unwind_mounts
if [ -z "$final_result" ]; then
    final_result="FAILED:internal error - repair finished without a recorded result"
fi
repair_result "$final_result"

log "=== initramfs repair completed ==="

# Copy full log (mount + repair) to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null
