#!/bin/bash
# GCE Repair - initramfs fix script
#
# Runs after the affected disk is mounted at /mnt/sysroot. Repairs the
# "VFS: Unable to mount root fs" class of boot failures, following
# https://cloud.google.com/compute/docs/troubleshooting/kernel-panic:
#
#   0. Checks that the root= device of the failed boot (GCE_BOOT_ROOT,
#      injected from the serial console) exists: a UUID/LABEL that no disk
#      has cannot be fixed by a rebuild, so the repair stops there. Reports
#      configuration that strips storage drivers from new images
#      (dracut omit_drivers, modprobe install/blacklist, MODULES=dep).
#   1. Rebuilds the initramfs of ONE kernel: the kernel that failed to boot
#      (GCE_FAILING_KERNEL, injected by the repair orchestrator from the
#      serial console) or, when unknown, the newest installed kernel.
#        RHEL family : dracut -f /boot/initramfs-<ver>.img <ver>
#        SLES        : dracut -f /boot/initrd-<ver> <ver>
#        Debian/Ubuntu: mkinitramfs -o /boot/initrd.img-<ver> <ver>
#      Other kernels are never touched, so a working fallback is preserved.
#      Not done when /boot/vmlinuz-<ver> holds another kernel's binary
#      (step 3 then runs). A missing FIPS /boot/.vmlinuz-<ver>.hmac is put
#      back from the kernel package.
#   2. Makes sure the boot entry of that kernel references its initramfs
#      (adds a missing 'initrd' line to the BLS entry), moves damaged early
#      microcode images (/boot/intel-ucode.img, ...) out of /boot on
#      grub-mkconfig systems, and regenerates the GRUB configuration.
#   3. If the rebuild fails, makes the newest OTHER installed kernel with a
#      valid initramfs the persistent default boot entry (the persistent
#      equivalent of picking the previous kernel in the GRUB menu).
#   4. If there is no such kernel, reports FAILED and leaves the disk as it
#      was.
#
# Safety rules:
#   - Every file is backed up before it is changed (backups go to
#     /var/backups/gce-rescue/ on the root filesystem; failing that, they are
#     renamed in place). A failed step restores its backup.
#   - Kernels and kernel modules are never deleted.
#   - A valid existing initramfs is only replaced by a new image that has
#     been built next to it and validated.
#   - Nothing runs unless the mounted partition is a writable root
#     filesystem and a separate /boot (if any) is mounted.
#
# All logic lives in functions prefixed initramfs_ (fix scripts share one
# shell, so names must not collide). The script never calls 'exit': later
# fix scripts and the completion signal must still run.

SYSROOT="/mnt/sysroot"
LOGFILE="${LOGFILE:-/var/log/gce-rescue.log}"

# Logs go to the log file and stderr (serial console). Never stdout: several
# helpers print their result on stdout and are called as $(helper).
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [REPAIR] $1" | tee -a "$LOGFILE" >&2
}

repair_line() {
    echo "GCE-REPAIR-LINE:$1" >&2
    log "$1"
}

repair_result() {
    echo "GCE-REPAIR-RESULT:$1" >&2
    log "Repair result: $1"
}

# Time limits (seconds). The repair orchestrator waits at least 900s for the
# whole startup script, so one build plus one GRUB run must fit well inside.
INITRAMFS_BUILD_TIMEOUT=420
INITRAMFS_GRUB_TIMEOUT=180
INITRAMFS_LIST_TIMEOUT=120

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Runs a command inside the target system. A fixed PATH and C locale make
# the result independent of the startup-script environment.
initramfs_in_chroot() {
    chroot "$SYSROOT" /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C HOME=/root TERM=dumb "$@"
}

# Runs a command inside the target with a time limit; output goes to the
# log file only.
initramfs_chroot_run() {
    local limit="$1"
    shift
    log "chroot: $*"
    timeout "$limit" chroot "$SYSROOT" /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C HOME=/root TERM=dumb "$@" >> "$LOGFILE" 2>&1
}

# Whether a command exists inside the target system.
initramfs_has_cmd() {
    initramfs_in_chroot /bin/sh -c 'command -v "$1"' sh "$1" >/dev/null 2>&1
}

# A kernel version is only used if it is a plain release string.
initramfs_safe_version() {
    case "$1" in
        ''|*[!A-Za-z0-9._+~-]*) return 1 ;;
        [0-9]*) return 0 ;;
        *) return 1 ;;
    esac
}

# Prints the OS family of the target: debian, rhel, suse or unknown.
initramfs_os_family() {
    local ids=""
    if [ -f "$SYSROOT/etc/os-release" ]; then
        # Parsed, never sourced: the file belongs to the customer's system.
        ids=$(grep -E '^(ID|ID_LIKE)=' "$SYSROOT/etc/os-release" 2>/dev/null \
            | cut -d= -f2- | tr -d '"' | tr '\n' ' ' | tr 'A-Z' 'a-z')
    fi
    case " $ids " in
        *suse*|*sles*) echo suse; return ;;
        *debian*|*ubuntu*) echo debian; return ;;
        *rhel*|*fedora*|*centos*|*rocky*|*almalinux*|*ol\ *) echo rhel; return ;;
    esac
    if [ -f "$SYSROOT/etc/debian_version" ]; then echo debian
    elif [ -f "$SYSROOT/etc/redhat-release" ]; then echo rhel
    elif [ -f "$SYSROOT/etc/SUSE-brand" ] || [ -f "$SYSROOT/etc/SuSE-release" ]; then echo suse
    else echo unknown
    fi
}

# Prints the modules directory of a kernel (path inside the target), or
# nothing. /usr/lib/modules is checked first; /lib/modules only when /lib is
# a real directory, because an absolute /lib symlink would resolve to the
# RESCUE system's modules.
initramfs_modules_dir() {
    local ver="$1"
    if [ -d "$SYSROOT/usr/lib/modules/$ver/kernel" ]; then
        echo "/usr/lib/modules/$ver"
    elif [ ! -L "$SYSROOT/lib" ] && [ -d "$SYSROOT/lib/modules/$ver/kernel" ]; then
        echo "/lib/modules/$ver"
    fi
}

# Prints installed kernel versions (a regular /boot/vmlinuz-<ver> file),
# oldest to newest. Skips symlinks, rescue kernels and unsafe names.
initramfs_list_kernels() {
    local f ver
    for f in "$SYSROOT"/boot/vmlinuz-*; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        ver="${f##*/vmlinuz-}"
        case "$ver" in 0-rescue-*) continue ;; esac
        initramfs_safe_version "$ver" || continue
        echo "$ver"
    done | sort -V
}

# Prints the initramfs path (inside the target) for a kernel. An existing
# file in any known naming wins; otherwise the family default is used.
initramfs_image_path() {
    local ver="$1" family="$2" name
    for name in "initramfs-$ver.img" "initrd.img-$ver" "initrd-$ver"; do
        if [ -e "$SYSROOT/boot/$name" ] && [ ! -L "$SYSROOT/boot/$name" ]; then
            echo "/boot/$name"
            return
        fi
    done
    case "$family" in
        debian) echo "/boot/initrd.img-$ver" ;;
        suse) echo "/boot/initrd-$ver" ;;
        *) echo "/boot/initramfs-$ver.img" ;;
    esac
}

# Prints the initramfs builder available in the target: mkinitramfs
# (initramfs-tools) on Debian-family systems that have it, else dracut.
initramfs_builder() {
    local family="$1"
    if [ "$family" = "debian" ] && initramfs_has_cmd mkinitramfs; then
        echo mkinitramfs
    elif initramfs_has_cmd dracut; then
        echo dracut
    elif initramfs_has_cmd mkinitramfs; then
        echo mkinitramfs
    fi
}

# Returns 0 when an initramfs image is readable and contains the modules
# of its kernel. Uses the target's own listing tool.
initramfs_validate() {
    local img="$1" ver="$2" listing rc tool=""
    [ -f "$SYSROOT$img" ] && [ -s "$SYSROOT$img" ] || return 1
    if initramfs_has_cmd lsinitrd; then
        tool=lsinitrd
    elif initramfs_has_cmd lsinitramfs; then
        tool=lsinitramfs
    else
        log "No lsinitrd/lsinitramfs in the target; cannot validate $img"
        return 1
    fi
    listing=$(timeout "$INITRAMFS_LIST_TIMEOUT" chroot "$SYSROOT" /usr/bin/env -i \
        PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C "$tool" "$img" 2>/dev/null)
    rc=$?
    if [ $rc -ne 0 ]; then
        log "$tool $img failed (rc=$rc)"
        return 1
    fi
    if ! printf '%s\n' "$listing" | grep -qF "modules/$ver/"; then
        log "$img does not contain modules for $ver"
        return 1
    fi
    return 0
}

# Prints the release string embedded in an x86 bzImage kernel
# (/boot/vmlinuz-<ver>), or nothing when the file is not a bzImage (arm64
# Image, EFI zboot) or the header cannot be read. x86 boot protocol: magic
# "HdrS" at 0x202; at 0x20E a 16-bit little-endian offset (relative to
# 0x200) of the NUL-terminated "<release> (builder) #1 SMP ..." string.
initramfs_vmlinuz_release() {
    local f="$SYSROOT/boot/vmlinuz-$1" magic off rel
    [ -f "$f" ] || return 0
    magic=$(dd if="$f" bs=1 skip=514 count=4 2>/dev/null | tr -dc 'A-Za-z')
    [ "$magic" = "HdrS" ] || return 0
    off=$(od -An -tu1 -j 526 -N 2 "$f" 2>/dev/null | awk 'NF == 2 {print $1 + 256 * $2}')
    case "$off" in ''|*[!0-9]*) return 0 ;; esac
    [ "$off" -gt 0 ] || return 0
    rel=$(dd if="$f" bs=1 skip=$((off + 512)) count=200 2>/dev/null \
        | tr '\0' '\n' | head -1 | awk '{print $1}')
    initramfs_safe_version "$rel" && echo "$rel"
    return 0
}

# Returns 1 when /boot/vmlinuz-<ver> provably holds another kernel's binary
# (its embedded release differs from <ver>): its modules and initramfs then
# belong to a different kernel, and no rebuild can fix that. Returns 0 when
# the release matches or cannot be read. Sets INITRAMFS_IMAGE_RELEASE.
initramfs_kernel_image_ok() {
    INITRAMFS_IMAGE_RELEASE=$(initramfs_vmlinuz_release "$1")
    [ -z "$INITRAMFS_IMAGE_RELEASE" ] || [ "$INITRAMFS_IMAGE_RELEASE" = "$1" ]
}

# FIPS mode verifies /boot/.vmlinuz-<ver>.hmac inside the initramfs
# ("dracut: FATAL: FIPS integrity test failed" when it is missing). dracut
# does not create it: kernel-install copies it from the kernel package
# (<modules dir>/.vmlinuz.hmac). Puts the package copy back when the /boot
# file is missing. An existing file is never changed. Sets
# INITRAMFS_HMAC_RESTORED to the restored path; returns 0 when restored.
initramfs_restore_hmac() {
    local ver="$1" dst src mods
    INITRAMFS_HMAC_RESTORED=""
    dst="/boot/.vmlinuz-$ver.hmac"
    if [ -e "$SYSROOT$dst" ] || [ -L "$SYSROOT$dst" ]; then
        return 1
    fi
    mods=$(initramfs_modules_dir "$ver")
    [ -n "$mods" ] || return 1
    src="$mods/.vmlinuz.hmac"
    if [ ! -f "$SYSROOT$src" ] || [ -L "$SYSROOT$src" ] || [ ! -s "$SYSROOT$src" ]; then
        return 1
    fi
    if cp -p "$SYSROOT$src" "$SYSROOT$dst" 2>/dev/null; then
        INITRAMFS_HMAC_RESTORED="$dst"
        initramfs_touched "$dst"
        log "Restored $dst from $src"
        return 0
    fi
    rm -f "$SYSROOT$dst" 2>/dev/null
    return 1
}

# Early microcode images that grub-mkconfig puts IN FRONT OF the initramfs
# on every 'initrd' line when they exist in /boot (GRUB_EARLY_INITRD_LINUX_STOCK).
INITRAMFS_EARLY_IMAGES="intel-uc.img intel-ucode.img amd-uc.img amd-ucode.img early_ucode.cpio microcode.cpio"

# Returns 1 when an early image is damaged: it must start with a cpio header
# (070701/070702/070707) or a compression format the kernel unpacks (gzip,
# xz, zstd, lz4, bzip2, lzma, lzo). Empty files are harmless and pass.
initramfs_early_image_ok() {
    local f="$1" head
    [ -s "$f" ] || return 0
    head=$(od -An -tx1 -N 6 "$f" 2>/dev/null | tr -d ' \n')
    case "$head" in
        30373037303[127]*) return 0 ;;
        1f8b*|1f9e*|fd377a585a00|28b52ffd*|02214c18*|425a68*|5d0000*|894c5a4f*) return 0 ;;
    esac
    return 1
}

# Moves damaged early images out of /boot (to the backup directory) so the
# next grub-mkconfig run leaves them out. A damaged image makes the kernel
# reject the whole initramfs ("Initramfs unpacking failed: invalid magic"),
# for every kernel. Sets INITRAMFS_EARLY_MOVED to "original:backup" pairs.
initramfs_fix_early_images() {
    local name moved
    INITRAMFS_EARLY_MOVED=""
    for name in $INITRAMFS_EARLY_IMAGES; do
        [ -f "$SYSROOT/boot/$name" ] && [ ! -L "$SYSROOT/boot/$name" ] || continue
        initramfs_early_image_ok "$SYSROOT/boot/$name" && continue
        if moved=$(initramfs_backup_move "/boot/$name"); then
            INITRAMFS_EARLY_MOVED="$INITRAMFS_EARLY_MOVED /boot/$name:$moved"
            log "Moved damaged early image /boot/$name to $moved"
        else
            log "WARNING: could not move damaged early image /boot/$name"
        fi
    done
}

# Puts back the images moved by initramfs_fix_early_images.
initramfs_undo_early_images() {
    local pair
    for pair in $INITRAMFS_EARLY_MOVED; do
        mv -f "$SYSROOT${pair#*:}" "$SYSROOT${pair%%:*}" \
            || log "ERROR: could not move ${pair#*:} back to ${pair%%:*}"
    done
    INITRAMFS_EARLY_MOVED=""
}

# Moves a file out of the way, keeping it. Tries the backup directory on
# the root filesystem first (frees space on a separate /boot); if that
# fails, renames it in place. Prints the new location.
initramfs_backup_move() {
    local path="$1" base dest
    base=$(basename "$path")
    dest="$INITRAMFS_BACKUP_DIR/$base"
    if mv -f "$SYSROOT$path" "$SYSROOT$dest" 2>/dev/null; then
        echo "$dest"
        return 0
    fi
    rm -f "$SYSROOT$dest" 2>/dev/null
    dest="$path.gce-rescue-backup"
    if mv -f "$SYSROOT$path" "$SYSROOT$dest" 2>/dev/null; then
        echo "$dest"
        return 0
    fi
    return 1
}

# Saves the current state of a small config file in the backup directory,
# once per run (the first, original state is what a restore goes back to).
# A file that does not exist is recorded with an '.absent' marker so that a
# restore removes it again. Returns non-zero when the copy fails: callers
# must not change a file they could not back up.
initramfs_backup_copy() {
    local path="$1" dest
    dest="$INITRAMFS_BACKUP_DIR/$(echo "${path#/}" | tr '/' '_')"
    [ -f "$SYSROOT$dest" ] || [ -f "$SYSROOT$dest.absent" ] && return 0
    if [ -L "$SYSROOT$path" ] && [ ! -e "$SYSROOT$path" ]; then
        log "Not touching $path: it is a dangling symlink"
        return 1
    fi
    if [ -e "$SYSROOT$path" ]; then
        cp -pL "$SYSROOT$path" "$SYSROOT$dest" 2>/dev/null && return 0
        rm -f "$SYSROOT$dest" 2>/dev/null
        log "Could not back up $path"
        return 1
    fi
    : > "$SYSROOT$dest.absent"
}

# Puts back a file saved by initramfs_backup_copy (writing through the
# existing file, so symlinks such as grubenv -> ESP stay intact), or removes
# it when it did not exist originally.
initramfs_restore_copy() {
    local path="$1" src
    src="$INITRAMFS_BACKUP_DIR/$(echo "${path#/}" | tr '/' '_')"
    if [ -f "$SYSROOT$src" ]; then
        cat "$SYSROOT$src" > "$SYSROOT$path"
    elif [ -f "$SYSROOT$src.absent" ]; then
        rm -f "$SYSROOT$path"
    fi
}

initramfs_touched() {
    INITRAMFS_TOUCHED="$INITRAMFS_TOUCHED $1"
}

# ---------------------------------------------------------------------------
# Mounting /boot and /boot/efi of the target
# ---------------------------------------------------------------------------

# Prints the partition of the AFFECTED disk that an fstab spec names, or
# nothing. Only partitions of /dev/disk/by-id/google-$disk are considered:
# the rescue disk can carry the same UUIDs/labels when both come from the
# same image.
initramfs_resolve_spec() {
    local spec="$1" key="" val="" part num got
    case "$spec" in
        UUID=*) key=UUID; val="${spec#UUID=}" ;;
        LABEL=*) key=LABEL; val="${spec#LABEL=}" ;;
        PARTUUID=*) key=PARTUUID; val="${spec#PARTUUID=}" ;;
        PARTLABEL=*) key=PARTLABEL; val="${spec#PARTLABEL=}" ;;
        /dev/disk/by-uuid/*) key=UUID; val="${spec##*/}" ;;
        /dev/disk/by-label/*) key=LABEL; val="${spec##*/}" ;;
        /dev/disk/by-partuuid/*) key=PARTUUID; val="${spec##*/}" ;;
        /dev/disk/by-partlabel/*) key=PARTLABEL; val="${spec##*/}" ;;
        /dev/*) key=NUM; num=$(echo "$spec" | grep -oE '[0-9]+$') ;;
        *) return 1 ;;
    esac
    val=$(echo "$val" | tr -d '"' | tr 'A-Z' 'a-z')
    [ -n "${disk:-}" ] && [ -e "/dev/disk/by-id/google-$disk" ] || return 1
    for part in $(lsblk -lnpo NAME,TYPE "/dev/disk/by-id/google-$disk" 2>/dev/null \
            | awk '$2 == "part" {print $1}'); do
        if [ "$key" = "NUM" ]; then
            [ -n "$num" ] && echo "$part" | grep -qE "[^0-9]${num}\$" && { echo "$part"; return 0; }
            continue
        fi
        got=$(blkid -o value -s "$key" "$part" 2>/dev/null | tr 'A-Z' 'a-z')
        if [ -n "$got" ] && [ "$got" = "$val" ]; then
            echo "$part"
            return 0
        fi
    done
    return 1
}

# Mounts a target mount point (/boot or /boot/efi) listed in the target's
# fstab. Returns 0 when it is mounted or not listed, 1 when it is listed
# but could not be mounted.
initramfs_mount_target() {
    local mp="$1" spec fstype dev
    mountpoint -q "$SYSROOT$mp" && return 0
    read -r spec fstype <<EOF
$(awk -v mp="$mp" '$1 !~ /^#/ && $2 == mp {print $1, $3; exit}' "$SYSROOT/etc/fstab" 2>/dev/null)
EOF
    [ -z "$spec" ] && return 0
    dev=$(initramfs_resolve_spec "$spec")
    if [ -z "$dev" ]; then
        log "Could not find the partition for $mp ($spec) on the affected disk"
        return 1
    fi
    mkdir -p "$SYSROOT$mp" 2>/dev/null
    if [ "$fstype" = "xfs" ]; then
        mount -o nouuid "$dev" "$SYSROOT$mp" >> "$LOGFILE" 2>&1
    else
        mount "$dev" "$SYSROOT$mp" >> "$LOGFILE" 2>&1
    fi
    if mountpoint -q "$SYSROOT$mp"; then
        log "Mounted $dev ($spec) at $SYSROOT$mp"
        return 0
    fi
    log "Failed to mount $dev ($spec) at $SYSROOT$mp"
    return 1
}

# ---------------------------------------------------------------------------
# Boot entries and GRUB configuration
# ---------------------------------------------------------------------------

# Prints the BLS entry files whose 'linux' line boots the given kernel.
initramfs_bls_entries() {
    local ver="$1" f
    for f in "$SYSROOT"/boot/loader/entries/*.conf; do
        [ -f "$f" ] || continue
        if awk -v k="/vmlinuz-$ver" '$1 == "linux" {
                n = length($2) - length(k) + 1
                if (n >= 1 && substr($2, n) == k) found = 1
            } END { exit !found }' "$f"; then
            echo "$f"
        fi
    done
}

# Makes every BLS entry of a kernel reference its initramfs. Adds a missing
# 'initrd' line (same directory prefix as the entry's 'linux' line) and
# replaces a single-image 'initrd' line naming a file that does not exist.
# Sets INITRAMFS_BLS_CHANGED to the number of entries changed and
# INITRAMFS_BLS_FILES to their paths (inside the target). Must not be called
# in a subshell (it sets globals and records changed files).
initramfs_fix_bls_initrd() {
    local ver="$1" img="$2" f linux_path prefix want cur_n cur_img tmp
    INITRAMFS_BLS_CHANGED=0
    INITRAMFS_BLS_FILES=""
    for f in $(initramfs_bls_entries "$ver"); do
        linux_path=$(awk '$1 == "linux" {print $2; exit}' "$f")
        prefix="${linux_path%/*}"
        want="$prefix/$(basename "$img")"
        cur_n=$(awk '$1 == "initrd" {print NF; exit}' "$f")
        if [ -n "$cur_n" ]; then
            # Keep any initrd line with several images or variables, and any
            # line whose image exists - only a lone missing image is replaced.
            [ "$cur_n" -eq 2 ] || continue
            cur_img=$(awk '$1 == "initrd" {print $2; exit}' "$f")
            case "$cur_img" in \$*) continue ;; esac
            [ -e "$SYSROOT/boot/${cur_img##*/}" ] && continue
        fi
        initramfs_backup_copy "${f#"$SYSROOT"}" || continue
        tmp="$INITRAMFS_BACKUP_DIR/bls-entry.tmp"
        if [ -n "$cur_n" ]; then
            awk -v want="$want" '$1 == "initrd" && !done {print "initrd " want; done = 1; next} {print}' \
                "$f" > "$SYSROOT$tmp"
        else
            awk -v want="$want" '{print} $1 == "linux" && !done {print "initrd " want; done = 1}' \
                "$f" > "$SYSROOT$tmp"
        fi
        if [ -s "$SYSROOT$tmp" ] && cat "$SYSROOT$tmp" > "$f"; then
            INITRAMFS_BLS_CHANGED=$((INITRAMFS_BLS_CHANGED + 1))
            INITRAMFS_BLS_FILES="$INITRAMFS_BLS_FILES ${f#"$SYSROOT"}"
            initramfs_touched "${f#"$SYSROOT"}"
            log "Set 'initrd $want' in ${f#"$SYSROOT"}"
        fi
        rm -f "$SYSROOT$tmp"
    done
}

# Prints the path (inside the target) of the GRUB config GRUB reads. The
# ESP's grub.cfg is used only when it is a full generated config (RHEL 7/8
# UEFI style); a stub that chains to /boot is never overwritten (RHEL 9,
# SLES, Debian, Ubuntu). The decision is made from the files on the disk:
# the rescue boot's firmware comes from the rescue image, not the original.
initramfs_grub_cfg_path() {
    local family="$1" f
    if [ "$family" = "debian" ]; then
        echo "/boot/grub/grub.cfg"
        return
    fi
    for f in "$SYSROOT"/boot/efi/EFI/*/grub.cfg; do
        [ -f "$f" ] && [ ! -L "$f" ] || continue
        if grep -q '### BEGIN /etc/grub.d' "$f" 2>/dev/null; then
            echo "${f#"$SYSROOT"}"
            return
        fi
    done
    echo "/boot/grub2/grub.cfg"
}

# Regenerates the GRUB config (backup first, restored on failure). Sets
# INITRAMFS_GRUB_CFG to the config path. Returns 0 on success.
initramfs_update_grub() {
    local family="$1" cfg cmd=()
    cfg=$(initramfs_grub_cfg_path "$family")
    INITRAMFS_GRUB_CFG="$cfg"
    if [ "$family" = "debian" ] && initramfs_has_cmd update-grub; then
        cmd=(update-grub)
    elif initramfs_has_cmd grub2-mkconfig; then
        cmd=(grub2-mkconfig -o "$cfg")
    elif initramfs_has_cmd grub-mkconfig; then
        cmd=(grub-mkconfig -o "$cfg")
    else
        log "No GRUB config generator found in the target"
        return 1
    fi
    if [ ! -d "$SYSROOT$(dirname "$cfg")" ]; then
        log "GRUB directory $(dirname "$cfg") does not exist"
        return 1
    fi
    initramfs_backup_copy "$cfg" || return 1
    # os-prober would add the RESCUE system's OS to the customer's menu.
    if initramfs_chroot_run "$INITRAMFS_GRUB_TIMEOUT" \
            env GRUB_DISABLE_OS_PROBER=true "${cmd[@]}" \
            && [ -s "$SYSROOT$cfg" ] \
            && grep -q '### BEGIN /etc/grub.d' "$SYSROOT$cfg"; then
        initramfs_touched "$cfg"
        return 0
    fi
    log "GRUB config generation failed; restoring the previous $cfg"
    initramfs_restore_copy "$cfg"
    return 1
}

# Returns 0 when the boot configuration of a kernel references its
# initramfs: in its BLS entries, or in the GRUB config for non-BLS systems.
initramfs_boot_entry_ok() {
    local ver="$1" img="$2" cfg="$3" base f entries
    base=$(basename "$img")
    entries=$(initramfs_bls_entries "$ver")
    if [ -n "$entries" ]; then
        for f in $entries; do
            awk -v b="$base" '$1 == "initrd" && index($0, b) {found = 1} END {exit !found}' "$f" \
                || return 1
        done
        return 0
    fi
    [ -n "$cfg" ] && [ -f "$SYSROOT$cfg" ] || return 1
    grep -qF "vmlinuz-$ver" "$SYSROOT$cfg" && grep -qF "$base" "$SYSROOT$cfg"
}

# Prints the GRUB saved_entry value (menu entry id, prefixed with the
# submenu id when the entry sits in the 'Advanced options' submenu) for a
# kernel in a grub-mkconfig generated config.
initramfs_grub_entry_id() {
    local cfg="$1" ver="$2" entry sub
    entry=$(grep -F "gnulinux-$ver-advanced-" "$SYSROOT$cfg" 2>/dev/null \
        | grep -oE "menuentry_id_option '[^']+'" | head -1 | cut -d"'" -f2)
    [ -n "$entry" ] || return 1
    sub=$(grep -E '^submenu ' "$SYSROOT$cfg" 2>/dev/null \
        | grep -oE "menuentry_id_option 'gnulinux-advanced-[^']+'" | head -1 | cut -d"'" -f2)
    if [ -n "$sub" ]; then
        echo "$sub>$entry"
    else
        echo "$entry"
    fi
}

INITRAMFS_DROPIN="/etc/default/grub.d/99-gce-rescue-fallback.cfg"

# Makes GRUB boot the saved entry (GRUB_DEFAULT=saved). Sets
# INITRAMFS_DEFAULT_EDITED=1 when a file was changed (the GRUB config must
# then be regenerated). Returns 1 when it cannot be done.
initramfs_make_default_saved() {
    local family="$1" cur tmp
    INITRAMFS_DEFAULT_EDITED=0
    if [ "$family" = "debian" ]; then
        # A drop-in sorts after the cloud image's own GRUB_DEFAULT=0
        # (/etc/default/grub.d/50-cloudimg-settings.cfg on Ubuntu).
        INITRAMFS_DROPIN_DIR_CREATED=0
        if [ ! -d "$SYSROOT/etc/default/grub.d" ]; then
            mkdir -p "$SYSROOT/etc/default/grub.d" || return 1
            INITRAMFS_DROPIN_DIR_CREATED=1
        fi
        initramfs_backup_copy "$INITRAMFS_DROPIN" || return 1
        printf '%s\n' "# Added by gce-rescue: boot the saved (previous working) kernel." \
            "# Remove this file and run update-grub to boot the newest kernel again." \
            "GRUB_DEFAULT=saved" > "$SYSROOT$INITRAMFS_DROPIN" || return 1
        INITRAMFS_DEFAULT_EDITED=1
        initramfs_touched "$INITRAMFS_DROPIN"
        return 0
    fi
    [ -f "$SYSROOT/etc/default/grub" ] || return 1
    cur=$(grep -E '^[[:space:]]*GRUB_DEFAULT=' "$SYSROOT/etc/default/grub" | tail -1 \
        | cut -d= -f2- | tr -d "\"'[:space:]")
    [ "$cur" = "saved" ] && return 0
    initramfs_backup_copy "/etc/default/grub" || return 1
    tmp="$SYSROOT$INITRAMFS_BACKUP_DIR/grub-default.tmp"
    if [ -n "$cur" ] || grep -qE '^[[:space:]]*GRUB_DEFAULT=' "$SYSROOT/etc/default/grub"; then
        sed 's/^[[:space:]]*GRUB_DEFAULT=.*/GRUB_DEFAULT=saved/' "$SYSROOT/etc/default/grub" > "$tmp"
    else
        { cat "$SYSROOT/etc/default/grub"; echo "GRUB_DEFAULT=saved"; } > "$tmp"
    fi
    if [ -s "$tmp" ] && cat "$tmp" > "$SYSROOT/etc/default/grub"; then
        rm -f "$tmp"
        INITRAMFS_DEFAULT_EDITED=1
        initramfs_touched "/etc/default/grub"
        log "Set GRUB_DEFAULT=saved in /etc/default/grub (was '${cur}')"
        return 0
    fi
    rm -f "$tmp"
    initramfs_restore_copy "/etc/default/grub"
    return 1
}

# Makes a kernel the persistent default boot entry. Returns 0 on success;
# on failure every file it changed is put back.
initramfs_set_default() {
    local ver="$1" family="$2" saved env_cmd set_cmd got grubenv \
        cfg_regenerated=0
    INITRAMFS_DEFAULT_EDITED=0
    INITRAMFS_DROPIN_DIR_CREATED=0
    grubenv="/boot/grub2/grubenv"
    [ "$family" = "debian" ] && grubenv="/boot/grub/grubenv"
    initramfs_backup_copy "$grubenv" || return 1

    if initramfs_make_default_saved "$family"; then
        if [ "$INITRAMFS_DEFAULT_EDITED" = "1" ]; then
            initramfs_update_grub "$family" && cfg_regenerated=1
        fi
        if [ "$INITRAMFS_DEFAULT_EDITED" = "0" ] || [ "$cfg_regenerated" = "1" ]; then
            if [ -n "$(initramfs_bls_entries "$ver")" ] && initramfs_has_cmd grubby; then
                # BLS systems (RHEL 8+, Fedora): grubby owns the default entry.
                if initramfs_chroot_run 60 grubby --set-default "/boot/vmlinuz-$ver"; then
                    got=$(initramfs_in_chroot grubby --default-kernel 2>/dev/null)
                    if [ "${got##*/vmlinuz-}" = "$ver" ]; then
                        initramfs_touched "$grubenv"
                        return 0
                    fi
                    log "grubby reports default kernel '$got', expected $ver"
                fi
            else
                # grub-mkconfig systems: saved_entry is the menu entry id.
                [ -n "$INITRAMFS_GRUB_CFG" ] || INITRAMFS_GRUB_CFG=$(initramfs_grub_cfg_path "$family")
                saved=$(initramfs_grub_entry_id "$INITRAMFS_GRUB_CFG" "$ver")
                if initramfs_has_cmd grub2-set-default; then
                    set_cmd=grub2-set-default; env_cmd=grub2-editenv
                else
                    set_cmd=grub-set-default; env_cmd=grub-editenv
                fi
                if [ -z "$saved" ]; then
                    log "Could not find a GRUB menu entry for kernel $ver in $INITRAMFS_GRUB_CFG"
                elif initramfs_chroot_run 60 "$set_cmd" "$saved"; then
                    if initramfs_in_chroot "$env_cmd" "$grubenv" list 2>/dev/null \
                            | grep -qxF "saved_entry=$saved"; then
                        initramfs_touched "$grubenv"
                        return 0
                    fi
                    log "$grubenv does not show saved_entry=$saved"
                fi
            fi
        fi
    fi

    # Undo: the previous default stays in effect.
    log "Could not make kernel $ver the default; undoing the changes"
    if [ "$family" = "debian" ]; then
        initramfs_restore_copy "$INITRAMFS_DROPIN"
        if [ "$INITRAMFS_DROPIN_DIR_CREATED" = "1" ]; then
            rmdir "$SYSROOT/etc/default/grub.d" 2>/dev/null
        fi
    elif [ "$INITRAMFS_DEFAULT_EDITED" = "1" ]; then
        initramfs_restore_copy "/etc/default/grub"
    fi
    if [ "$cfg_regenerated" = "1" ]; then
        initramfs_restore_copy "$INITRAMFS_GRUB_CFG"
    fi
    initramfs_restore_copy "$grubenv"
    return 1
}

# Restores SELinux labels of changed files when the target uses SELinux:
# files written from the (non-SELinux) rescue system carry no label.
initramfs_selinux_relabel() {
    local mode fc
    [ -n "$INITRAMFS_TOUCHED" ] || return 0
    mode=$(awk -F= '$1 == "SELINUX" {print $2}' "$SYSROOT/etc/selinux/config" 2>/dev/null)
    case "$mode" in enforcing|permissive) ;; *) return 0 ;; esac
    fc=$(awk -F= '$1 == "SELINUXTYPE" {print $2}' "$SYSROOT/etc/selinux/config" 2>/dev/null)
    fc="/etc/selinux/${fc:-targeted}/contexts/files/file_contexts"
    if [ -f "$SYSROOT$fc" ] && initramfs_has_cmd setfiles; then
        # shellcheck disable=SC2086
        if initramfs_chroot_run 120 setfiles -F "$fc" $INITRAMFS_TOUCHED; then
            log "Restored SELinux labels of changed files"
            return 0
        fi
    fi
    log "WARNING: could not restore SELinux labels of: $INITRAMFS_TOUCHED"
}

# ---------------------------------------------------------------------------
# Rebuild and fallback
# ---------------------------------------------------------------------------

# Builds an initramfs image at the given path. Returns the builder status.
initramfs_build() {
    local builder="$1" out="$2" ver="$3"
    case "$builder" in
        dracut) initramfs_chroot_run "$INITRAMFS_BUILD_TIMEOUT" dracut -f "$out" "$ver" ;;
        mkinitramfs) initramfs_chroot_run "$INITRAMFS_BUILD_TIMEOUT" mkinitramfs -o "$out" "$ver" ;;
        *) return 1 ;;
    esac
}

# Debian: run the hooks update-initramfs would run after an update.
initramfs_post_update_hooks() {
    local ver="$1" img="$2"
    if [ -d "$SYSROOT/etc/initramfs/post-update.d" ]; then
        initramfs_chroot_run 120 run-parts --arg="$ver" --arg="$img" \
            /etc/initramfs/post-update.d/ || log "WARNING: post-update hooks failed"
    fi
}

# Rebuilds the initramfs of one kernel. Leaves the original image in place
# (or puts it back) when the rebuild fails. Returns 0 on success.
initramfs_rebuild() {
    local ver="$1" family="$2" img="$3" builder moved="" tmp avail_kb size_kb
    if [ -z "$(initramfs_modules_dir "$ver")" ]; then
        log "Kernel modules for $ver are missing; cannot rebuild its initramfs"
        INITRAMFS_FAIL_REASON="kernel modules for $ver are missing"
        return 1
    fi
    builder=$(initramfs_builder "$family")
    if [ -z "$builder" ]; then
        log "Neither dracut nor mkinitramfs is installed in the target"
        INITRAMFS_FAIL_REASON="no initramfs builder (dracut/mkinitramfs) installed"
        return 1
    fi
    log "Rebuilding $img for kernel $ver with $builder"

    if [ -e "$SYSROOT$img" ] && initramfs_validate "$img" "$ver"; then
        # Valid image: build next to it, swap only after validation.
        tmp="$img.gce-rescue-new"
        rm -f "$SYSROOT$tmp"
        if initramfs_build "$builder" "$tmp" "$ver" && initramfs_validate "$tmp" "$ver"; then
            if moved=$(initramfs_backup_move "$img"); then
                if mv -f "$SYSROOT$tmp" "$SYSROOT$img"; then
                    log "Previous image kept at $moved"
                    return 0
                fi
                mv -f "$SYSROOT$moved" "$SYSROOT$img"
            fi
            rm -f "$SYSROOT$tmp"
            INITRAMFS_FAIL_REASON="could not replace $img"
            return 1
        fi
        rm -f "$SYSROOT$tmp"
        # Retry in place only when /boot cannot hold a second image; a
        # builder that fails for another reason would fail again.
        avail_kb=$(df -Pk "$SYSROOT$(dirname "$img")" 2>/dev/null | awk 'NR == 2 {print $4}')
        size_kb=$(du -k "$SYSROOT$img" 2>/dev/null | cut -f1)
        if [ -z "$avail_kb" ] || [ -z "$size_kb" ] \
                || [ "$avail_kb" -ge $((size_kb * 2)) ]; then
            INITRAMFS_FAIL_REASON="$builder could not build a valid initramfs for $ver"
            return 1
        fi
        log "Not enough space in /boot for a second image (${avail_kb}K free); retrying in place"
    fi

    # Move the current image (missing, invalid, or valid but in the way)
    # out of /boot, then build in place.
    if [ -e "$SYSROOT$img" ]; then
        moved=$(initramfs_backup_move "$img") || {
            INITRAMFS_FAIL_REASON="could not back up $img"
            return 1
        }
        log "Current image moved to $moved"
    fi
    if initramfs_build "$builder" "$img" "$ver" && initramfs_validate "$img" "$ver"; then
        return 0
    fi
    # Put the disk back the way it was.
    rm -f "$SYSROOT$img"
    if [ -n "$moved" ] && ! mv -f "$SYSROOT$moved" "$SYSROOT$img"; then
        log "ERROR: could not move $moved back to $img"
    fi
    INITRAMFS_FAIL_REASON="$builder could not build a valid initramfs for $ver"
    return 1
}

# Prints the installed kernels other than the target that have their
# modules, a kernel binary of the right release and a valid initramfs,
# newest first.
initramfs_find_fallback() {
    local target="$1" family="$2" ver img found=1
    for ver in $(initramfs_list_kernels | sort -rV); do
        [ "$ver" = "$target" ] && continue
        [ -n "$(initramfs_modules_dir "$ver")" ] || continue
        if ! initramfs_kernel_image_ok "$ver"; then
            log "Skipping kernel $ver: /boot/vmlinuz-$ver contains kernel $INITRAMFS_IMAGE_RELEASE"
            continue
        fi
        img=$(initramfs_image_path "$ver" "$family")
        if initramfs_validate "$img" "$ver"; then
            echo "$ver"
            found=0
        fi
    done
    return $found
}

# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

# Whether a block device visible from the rescue system carries the given
# blkid tag (UUID, PARTUUID, LABEL, PARTLABEL) with the given value.
initramfs_device_with_tag() {
    local tag="$1" value="$2"
    # -c /dev/null probes the devices instead of trusting the blkid cache.
    if [ -n "$(blkid -c /dev/null -o device -t "$tag=$value" 2>/dev/null)" ]; then
        return 0
    fi
    # Second opinion from lsblk, case-insensitive (UUIDs are sometimes
    # written in upper case on the kernel command line).
    lsblk -rno "$tag" 2>/dev/null | grep -qixF -- "$value"
}

# Checks that the root= device of the failed boot (GCE_BOOT_ROOT, taken from
# the serial console by the orchestrator) exists. When the boot
# configuration names a UUID/LABEL that no attached disk has (disk restored
# from another VM's snapshot, filesystem recreated, typo in
# GRUB_CMDLINE_LINUX) a rebuild cannot help, so this returns 1 with
# INITRAMFS_FAIL_REASON set and the repair stops BEFORE changing anything.
# Only by-tag references are checked: /dev/sdX, /dev/nvme* and
# /dev/mapper/* names look different from the rescue system. When LVM or
# LUKS devices exist the tag may live inside a container the rescue system
# has not opened, so a missing tag is only logged.
initramfs_check_boot_root() {
    local spec="${GCE_BOOT_ROOT:-}" tag value
    [ -n "$spec" ] || return 0
    case "$spec" in
        *[!A-Za-z0-9_.:/=+-]*) log "Ignoring unexpected root= value"; return 0 ;;
    esac
    case "$spec" in
        UUID=*|uuid=*)            tag=UUID;      value="${spec#*=}" ;;
        PARTUUID=*|partuuid=*)    tag=PARTUUID;  value="${spec#*=}" ;;
        LABEL=*|label=*)          tag=LABEL;     value="${spec#*=}" ;;
        PARTLABEL=*|partlabel=*)  tag=PARTLABEL; value="${spec#*=}" ;;
        /dev/disk/by-uuid/*)      tag=UUID;      value="${spec#/dev/disk/by-uuid/}" ;;
        /dev/disk/by-partuuid/*)  tag=PARTUUID;  value="${spec#/dev/disk/by-partuuid/}" ;;
        /dev/disk/by-label/*)     tag=LABEL;     value="${spec#/dev/disk/by-label/}" ;;
        /dev/disk/by-partlabel/*) tag=PARTLABEL; value="${spec#/dev/disk/by-partlabel/}" ;;
        *) log "Failed boot used root=$spec (not checked)"; return 0 ;;
    esac
    [ -n "$value" ] || return 0
    if initramfs_device_with_tag "$tag" "$value"; then
        log "Failed boot used root=$spec: device exists"
        return 0
    fi
    # Filesystems inside LVM, LUKS or md RAID containers are not visible
    # here (the containers are not activated in the rescue VM), so their
    # tags cannot be checked.
    if blkid -c /dev/null -o value -s TYPE 2>/dev/null | grep -qE '^(LVM2_member|crypto_LUKS|linux_raid_member)$'; then
        log "WARNING: no device with $tag=$value is visible, but LVM/LUKS/RAID devices exist; continuing"
        return 0
    fi
    INITRAMFS_FAIL_REASON="the failed boot used root=$spec, but no attached disk has that $tag. Rebuilding the initramfs cannot fix this: correct root= in the GRUB configuration (or attach the right disk) and try again"
    return 1
}

# Reports configuration that removes storage drivers from a NEW initramfs.
# The rebuild still runs (the configuration belongs to the customer), but
# if the VM does not boot afterwards these lines are the first suspects.
initramfs_warn_config() {
    local f line
    for f in "$SYSROOT"/etc/dracut.conf "$SYSROOT"/etc/dracut.conf.d/*.conf; do
        [ -f "$f" ] || continue
        while IFS= read -r line; do
            repair_line "[INFO] initramfs: ${f#"$SYSROOT"} removes modules from new initramfs images: $line"
        done < <(grep -E '^[[:space:]]*(omit_drivers|omit_dracutmodules)[[:space:]]*\+?=' "$f" 2>/dev/null | head -5)
    done
    for f in "$SYSROOT"/etc/modprobe.d/*.conf; do
        [ -f "$f" ] || continue
        while IFS= read -r line; do
            repair_line "[INFO] initramfs: ${f#"$SYSROOT"} blocks a storage driver: $line"
        done < <(grep -E '^[[:space:]]*(install|blacklist)[[:space:]]+(virtio|nvme|sd_mod|scsi_mod|ext4|xfs|vfat)' "$f" 2>/dev/null | head -5)
    done
    f="$SYSROOT/etc/initramfs-tools/initramfs.conf"
    if [ -f "$f" ]; then
        line=$(grep -E '^[[:space:]]*MODULES[[:space:]]*=[[:space:]]*(dep|list)' "$f" 2>/dev/null | head -1)
        [ -n "$line" ] && repair_line "[INFO] initramfs: ${f#"$SYSROOT"} limits the drivers in new initramfs images: $line"
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

initramfs_repair() {
    local family kernels target img fixes=0 candidates fallback fallback_img dir f bls_changed bls_files

    INITRAMFS_TOUCHED=""
    INITRAMFS_GRUB_CFG=""
    INITRAMFS_FAIL_REASON=""

    if [ ! -d "$SYSROOT" ] || ! mountpoint -q "$SYSROOT"; then
        repair_result "FAILED:sysroot not mounted at $SYSROOT"
        return
    fi
    if ! touch "$SYSROOT/.gce-rescue-write-test" 2>/dev/null; then
        repair_result "FAILED:the root filesystem is mounted read-only"
        return
    fi
    rm -f "$SYSROOT/.gce-rescue-write-test"
    if [ ! -d "$SYSROOT/etc" ] || [ ! -d "$SYSROOT/boot" ] \
            || { [ ! -d "$SYSROOT/usr/lib/modules" ] && [ ! -d "$SYSROOT/lib/modules" ]; }; then
        repair_result "FAILED:the mounted partition is not a Linux root filesystem (LVM or encrypted roots are not supported)"
        return
    fi

    # Chroot support (the base script mounts proc, sys and dev).
    for dir in proc sys dev dev/pts run; do
        if [ -d "$SYSROOT/$dir" ] && ! mountpoint -q "$SYSROOT/$dir"; then
            mount -o bind "/$dir" "$SYSROOT/$dir" 2>/dev/null || true
        fi
    done

    # A separate /boot MUST be mounted: writing into the empty mount point
    # on the root filesystem would "repair" files GRUB never reads.
    if ! initramfs_mount_target /boot; then
        repair_result "FAILED:could not mount the separate /boot partition"
        return
    fi
    initramfs_mount_target /boot/efi || log "WARNING: /boot/efi is not mounted"

    family=$(initramfs_os_family)
    log "OS family: $family"
    if [ "$family" = "unknown" ]; then
        repair_result "FAILED:unsupported Linux distribution"
        return
    fi

    # A root= that names a missing UUID/LABEL cannot be fixed here; stop
    # before touching the disk.
    if ! initramfs_check_boot_root; then
        repair_result "FAILED:$INITRAMFS_FAIL_REASON"
        return
    fi

    kernels=$(initramfs_list_kernels)
    if [ -z "$kernels" ]; then
        repair_result "FAILED:no installed kernel found in /boot"
        return
    fi
    log "Installed kernels: ${kernels//$'\n'/ }"

    target=""
    if [ -n "${GCE_FAILING_KERNEL:-}" ] && initramfs_safe_version "$GCE_FAILING_KERNEL"; then
        if echo "$kernels" | grep -qxF "$GCE_FAILING_KERNEL"; then
            target="$GCE_FAILING_KERNEL"
            log "Target kernel (from diagnosis): $target"
        else
            log "Kernel $GCE_FAILING_KERNEL from diagnosis is not installed on this disk"
        fi
    fi
    if [ -z "$target" ]; then
        target=$(echo "$kernels" | tail -1)
        log "Target kernel (newest installed): $target"
    fi

    INITRAMFS_BACKUP_DIR="/var/backups/gce-rescue/initramfs-$(date '+%Y%m%d-%H%M%S')"
    if ! mkdir -p "$SYSROOT$INITRAMFS_BACKUP_DIR"; then
        repair_result "FAILED:could not create the backup directory $INITRAMFS_BACKUP_DIR"
        return
    fi
    log "Backups: $INITRAMFS_BACKUP_DIR"

    # A build interrupted by an earlier, aborted repair can leave a partial
    # image next to the real one. It is never referenced by any boot entry.
    rm -f "$SYSROOT"/boot/*.gce-rescue-new 2>/dev/null

    initramfs_warn_config

    img=$(initramfs_image_path "$target" "$family")
    if ! initramfs_kernel_image_ok "$target"; then
        # vmlinuz-<target> holds another kernel (e.g. a file copied over
        # it): its initramfs and modules can never match. Nothing is
        # changed for this kernel; the fallback below boots another one.
        INITRAMFS_FAIL_REASON="/boot/vmlinuz-$target contains kernel $INITRAMFS_IMAGE_RELEASE; reinstall the package of kernel $target to use it again"
        log "Not rebuilding: $INITRAMFS_FAIL_REASON"
    elif initramfs_rebuild "$target" "$family" "$img"; then
        [ "$family" = "debian" ] && initramfs_post_update_hooks "$target" "$img"
        initramfs_touched "$img"
        fixes=$((fixes + 1))
        repair_line "[FIXED] initramfs: Rebuilt $img for kernel $target"

        if initramfs_restore_hmac "$target"; then
            fixes=$((fixes + 1))
            repair_line "[FIXED] initramfs: Restored the missing FIPS checksum file $INITRAMFS_HMAC_RESTORED from the kernel package"
        fi

        initramfs_fix_bls_initrd "$target" "$img"
        if [ "$INITRAMFS_BLS_CHANGED" -gt 0 ]; then
            fixes=$((fixes + 1))
            repair_line "[FIXED] initramfs: Added the missing initrd line to the boot entry of kernel $target"
        fi

        # Only grub-mkconfig menus list early images; BLS entries name
        # their initrd files themselves and are left alone.
        INITRAMFS_EARLY_MOVED=""
        [ -z "$(initramfs_bls_entries "$target")" ] && initramfs_fix_early_images
        if initramfs_update_grub "$family"; then
            fixes=$((fixes + 1))
            repair_line "[FIXED] initramfs: Regenerated the GRUB configuration ($INITRAMFS_GRUB_CFG)"
            for f in $INITRAMFS_EARLY_MOVED; do
                fixes=$((fixes + 1))
                repair_line "[FIXED] initramfs: Moved the damaged early microcode image ${f%%:*} to ${f#*:} (GRUB loaded it in front of the initramfs)"
            done
        else
            # The old GRUB config still names the early images: put them back.
            initramfs_undo_early_images
            log "WARNING: GRUB configuration was not regenerated"
        fi

        if initramfs_boot_entry_ok "$target" "$img" "$INITRAMFS_GRUB_CFG"; then
            initramfs_finish
            log "=== initramfs repair completed: $fixes fixes applied ==="
            repair_result "SUCCESS:$fixes"
            return
        fi
        log "The boot entry of kernel $target still does not reference $img"
        INITRAMFS_FAIL_REASON="the boot entry of kernel $target does not reference its initramfs"
    else
        log "Rebuild failed: $INITRAMFS_FAIL_REASON"
    fi

    # Fallback: boot the previous working kernel. Candidates are tried newest
    # first; initramfs_set_default undoes its own changes when it fails, so
    # moving on to the next candidate is safe.
    candidates=$(initramfs_find_fallback "$target" "$family")
    if [ -z "$candidates" ]; then
        initramfs_finish
        repair_result "FAILED:$INITRAMFS_FAIL_REASON and no other installed kernel has a valid initramfs"
        return
    fi
    for fallback in $candidates; do
        log "Fallback kernel: $fallback"
        fallback_img=$(initramfs_image_path "$fallback" "$family")
        initramfs_fix_bls_initrd "$fallback" "$fallback_img"
        bls_changed="$INITRAMFS_BLS_CHANGED"
        bls_files="$INITRAMFS_BLS_FILES"
        if initramfs_set_default "$fallback" "$family"; then
            if [ "$bls_changed" -gt 0 ]; then
                fixes=$((fixes + 1))
                repair_line "[FIXED] initramfs: Added the missing initrd line to the boot entry of kernel $fallback"
            fi
            fixes=$((fixes + 1))
            repair_line "[FIXED] initramfs: Could not repair kernel $target ($INITRAMFS_FAIL_REASON); set the previous kernel $fallback as the default boot entry"
            if [ "$family" = "debian" ]; then
                repair_line "[INFO] initramfs: To boot kernel $target again after fixing it, remove $INITRAMFS_DROPIN and run update-grub"
            fi
            initramfs_finish
            log "=== initramfs repair completed with kernel fallback: $fixes fixes applied ==="
            repair_result "SUCCESS:$fixes"
            return
        fi
        # Leave this kernel's boot entries as they were and try the next one.
        for f in $bls_files; do
            initramfs_restore_copy "$f"
        done
        log "Kernel $fallback could not be made the default"
    done
    initramfs_finish
    repair_result "FAILED:$INITRAMFS_FAIL_REASON and no previous kernel could be set as the default"
}

# Final steps on every path that changed or may have changed files.
initramfs_finish() {
    initramfs_selinux_relabel
    # Drop the markers of files that did not exist, then the backup
    # directory itself when nothing needed backing up.
    rm -f "$SYSROOT$INITRAMFS_BACKUP_DIR"/*.absent 2>/dev/null
    rmdir "$SYSROOT$INITRAMFS_BACKUP_DIR" 2>/dev/null \
        && rmdir "$SYSROOT/var/backups/gce-rescue" 2>/dev/null
    sync
}

log "=== initramfs repair started ==="
if [ "${GCE_INITRAMFS_FIX_LIB_ONLY:-0}" != "1" ]; then
    initramfs_repair
fi

# Copy full log to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null || true
