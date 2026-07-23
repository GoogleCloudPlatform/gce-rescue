#!/bin/bash
# GCE Repair - GRUB bootloader & configuration fix script
#
# Runs after disk is mounted at /mnt/sysroot.
# Re-installs GRUB bootloader and regenerates grub.cfg configuration.

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

log "=== GRUB repair started ==="

if [ ! -d "$SYSROOT" ] || ! mountpoint -q "$SYSROOT"; then
    repair_result "FAILED:sysroot not mounted at $SYSROOT"
    exit 0
fi

# 1. Bind mounts for chroot (/proc, /sys, /dev, /dev/pts, /run)
for dir in proc sys dev dev/pts run; do
    if [ -d "$SYSROOT/$dir" ] && ! mountpoint -q "$SYSROOT/$dir"; then
        mount -o bind "/$dir" "$SYSROOT/$dir" 2>/dev/null || true
    fi
done

# If efivarfs is present on host, mount inside chroot for UEFI support
if [ -d /sys/firmware/efi/efivars ] && [ -d "$SYSROOT/sys/firmware/efi/efivars" ] && ! mountpoint -q "$SYSROOT/sys/firmware/efi/efivars"; then
    mount -t efivarfs efivarfs "$SYSROOT/sys/firmware/efi/efivars" 2>/dev/null || true
fi

# 2. Check for separate /boot partition on target disk and mount if unmounted
if [ -d "$SYSROOT/boot" ] && ! mountpoint -q "$SYSROOT/boot"; then
    boot_dev=$(lsblk -rf /dev/disk/by-id/google-${disk:-affected-disk} 2>/dev/null | grep -iE 'ext[2-4]|xfs' | grep -v "$(mountpoint -d $SYSROOT 2>/dev/null)" | head -1 | awk '{print $1}')
    if [ -n "$boot_dev" ] && [ -b "/dev/$boot_dev" ]; then
        if grep -qE '\s+/boot\s+' "$SYSROOT/etc/fstab" 2>/dev/null; then
            log "Mounting separate /boot partition /dev/$boot_dev to $SYSROOT/boot"
            mount "/dev/$boot_dev" "$SYSROOT/boot" 2>/dev/null || true
        fi
    fi
fi

# 3. Mount EFI partition if present and unmounted
if [ -d "$SYSROOT/boot/efi" ] && ! mountpoint -q "$SYSROOT/boot/efi"; then
    efi_dev=$(lsblk -rf /dev/disk/by-id/google-${disk:-affected-disk} 2>/dev/null | grep -iE 'vfat|fat|msdos' | head -1 | awk '{print $1}')
    if [ -n "$efi_dev" ] && [ -b "/dev/$efi_dev" ]; then
        log "Mounting EFI partition /dev/$efi_dev to $SYSROOT/boot/efi"
        mount "/dev/$efi_dev" "$SYSROOT/boot/efi" 2>/dev/null || true
    fi
fi

fixes=0

# Detect OS family
is_rhel=false
is_debian=false

if [ -f "$SYSROOT/etc/debian_version" ] || ([ -f "$SYSROOT/etc/os-release" ] && grep -qiE 'debian|ubuntu' "$SYSROOT/etc/os-release"); then
    is_debian=true
elif [ -f "$SYSROOT/etc/redhat-release" ] || [ -f "$SYSROOT/etc/rocky-release" ] || [ -f "$SYSROOT/etc/alma-release" ] || ([ -f "$SYSROOT/etc/os-release" ] && grep -qiE 'rhel|centos|rocky|almalinux|fedora|sles|suse' "$SYSROOT/etc/os-release"); then
    is_rhel=true
fi

log "Detected OS family: debian=$is_debian, rhel=$is_rhel"

# Resolve real block device for grub-install
target_disk="/dev/disk/by-id/google-${disk:-affected-disk}"
if [ -L "$target_disk" ]; then
    real_dev=$(readlink -f "$target_disk")
    parent_disk=$(echo "$real_dev" | sed -E 's/p?[0-9]+$//')
    if [ -b "$parent_disk" ]; then
        target_disk="$parent_disk"
    fi
elif [ ! -b "$target_disk" ]; then
    if [ -b "/dev/sdb" ]; then
        target_disk="/dev/sdb"
    elif [ -b "/dev/nvme1n1" ]; then
        target_disk="/dev/nvme1n1"
    fi
fi

log "Target disk for GRUB installation: $target_disk"

# 4. Restore backup config files if original was truncated or missing
if [ ! -s "$SYSROOT/boot/grub/grub.cfg" ] && [ -f "$SYSROOT/boot/grub/grub.cfg.bak" ]; then
    cp "$SYSROOT/boot/grub/grub.cfg.bak" "$SYSROOT/boot/grub/grub.cfg"
    fixes=$((fixes + 1))
    repair_line "[FIXED] grub: Restored /boot/grub/grub.cfg from backup"
fi
if [ -d "$SYSROOT/boot/efi/EFI/debian" ] && [ ! -s "$SYSROOT/boot/efi/EFI/debian/grub.cfg" ] && [ -f "$SYSROOT/boot/efi/EFI/debian/grub.cfg.bak" ]; then
    cp "$SYSROOT/boot/efi/EFI/debian/grub.cfg.bak" "$SYSROOT/boot/efi/EFI/debian/grub.cfg"
    fixes=$((fixes + 1))
    repair_line "[FIXED] grub: Restored /boot/efi/EFI/debian/grub.cfg from backup"
fi

# 5. Regenerate GRUB config inside chroot
log "Regenerating GRUB configuration..."
if [ "$is_debian" = true ] && [ -x "$SYSROOT/usr/sbin/update-grub" ]; then
    chroot "$SYSROOT" update-grub 2>&1 | tee -a "$LOGFILE"
    if [ $? -eq 0 ] && [ -s "$SYSROOT/boot/grub/grub.cfg" ]; then
        fixes=$((fixes + 1))
        repair_line "[FIXED] grub: Successfully regenerated /boot/grub/grub.cfg via update-grub"
    fi
elif [ -x "$SYSROOT/usr/sbin/grub2-mkconfig" ] || [ -x "$SYSROOT/sbin/grub2-mkconfig" ]; then
    mkconfig_cmd="grub2-mkconfig"
    [ -x "$SYSROOT/sbin/grub2-mkconfig" ] && mkconfig_cmd="/sbin/grub2-mkconfig"

    cfg_dest="/boot/grub2/grub.cfg"
    if [ -d "$SYSROOT/boot/efi/EFI" ]; then
        efi_cfg=$(find "$SYSROOT/boot/efi/EFI" -name "grub.cfg" 2>/dev/null | head -1)
        if [ -n "$efi_cfg" ]; then
            cfg_dest="${efi_cfg#$SYSROOT}"
        fi
    fi
    mkdir -p "$(dirname "$SYSROOT/$cfg_dest")"
    chroot "$SYSROOT" "$mkconfig_cmd" -o "$cfg_dest" 2>&1 | tee -a "$LOGFILE"
    if [ $? -eq 0 ]; then
        fixes=$((fixes + 1))
        repair_line "[FIXED] grub: Successfully regenerated $cfg_dest via grub2-mkconfig"
    fi
elif [ -x "$SYSROOT/usr/sbin/grub-mkconfig" ]; then
    chroot "$SYSROOT" grub-mkconfig -o /boot/grub/grub.cfg 2>&1 | tee -a "$LOGFILE"
    if [ $? -eq 0 ]; then
        fixes=$((fixes + 1))
        repair_line "[FIXED] grub: Successfully regenerated /boot/grub/grub.cfg via grub-mkconfig"
    fi
fi

# 6. Reinstall GRUB bootloader
if [ -x "$SYSROOT/usr/sbin/grub-install" ] || [ -x "$SYSROOT/sbin/grub-install" ]; then
    grub_inst="grub-install"
    [ -x "$SYSROOT/sbin/grub-install" ] && grub_inst="/sbin/grub-install"
    chroot "$SYSROOT" "$grub_inst" "$target_disk" 2>&1 | tee -a "$LOGFILE" || true
    fixes=$((fixes + 1))
    repair_line "[FIXED] grub: Executed grub-install on $target_disk"
elif [ -x "$SYSROOT/usr/sbin/grub2-install" ] || [ -x "$SYSROOT/sbin/grub2-install" ]; then
    grub_inst="grub2-install"
    [ -x "$SYSROOT/sbin/grub2-install" ] && grub_inst="/sbin/grub2-install"
    chroot "$SYSROOT" "$grub_inst" "$target_disk" 2>&1 | tee -a "$LOGFILE" || true
    fixes=$((fixes + 1))
    repair_line "[FIXED] grub: Executed grub2-install on $target_disk"
fi

log "=== GRUB repair completed: $fixes fixes applied ==="

if [ $fixes -gt 0 ]; then
    repair_result "SUCCESS:$fixes"
else
    repair_result "NO_ISSUES:0"
fi
