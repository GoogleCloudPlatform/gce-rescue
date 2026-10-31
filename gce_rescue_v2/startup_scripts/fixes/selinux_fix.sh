#!/bin/bash
# GCE Repair - SELinux fix script
#
# Runs after the affected disk is mounted at /mnt/sysroot. Repairs the
# SELinux boot failures diagnose reports (core/diagnose_rules/selinux.yaml):
#
#   1. The policy named by SELINUXTYPE= in /etc/selinux/config is missing
#      or unreadable (no valid /etc/selinux/<type>/policy/policy.NN), so init
#      cannot load it and the boot fails. Permissive mode does NOT help here:
#      with no policy loaded, systemd (252 and later) cannot label its
#      sockets, udev never starts and the boot ends in emergency mode.
#      a. Another policy type is installed (e.g. SELINUXTYPE=mls but only
#         targeted exists): SELINUXTYPE= is switched to it (SELINUX= stays
#         as configured) and a full relabel is scheduled, because file
#         labels differ between policy types.
#      b. No policy is installed: the policy package is reinstalled with the
#         target's package manager inside a chroot (the rescue VM's DNS
#         settings are lent to the chroot for the download), then a full
#         relabel is scheduled.
#      c. The reinstall is not possible (no network, no subscription, no
#         package manager): selinux=0 is added to the kernel command line so
#         the VM boots with SELinux disabled. The repair line tells the user
#         how to reinstall the policy and turn SELinux back on.
#   2. The kernel command line sets autorelabel and the relabel banner was
#      seen: every boot relabels and reboots, so the argument is removed.
#   3. The policy is installed and /.autorelabel is ALREADY on the disk: a
#      relabel ran without the boot finishing (or the policy still failed to
#      load), so another relabel will not help. The policy loads, so this is
#      a labelling problem, and SELINUX=permissive lets the VM boot.
#   4. The policy is installed: a full relabel is scheduled by creating an
#      empty /.autorelabel, so the next boot relabels every file.
#
# Inputs:
#   GCE_SELINUX_FINDINGS  Names of the diagnosed selinux patterns, space
#                         separated (injected by the repair orchestrator;
#                         may be empty).
#
# Safety rules:
#   - Every file is backed up to /var/backups/gce-rescue/ on the root
#     filesystem before it is changed, and written through so that it keeps
#     its inode and its own SELinux label. A failed write restores the
#     backup.
#   - Nothing is deleted. SELinux is never disabled in /etc/selinux/config;
#     only the kernel command line disables it, and only when the policy
#     cannot be reinstalled.
#   - Nothing runs unless the mounted partition is a writable root
#     filesystem that carries an SELinux configuration.
#   - Mounts made for the chroot are undone in reverse order, and the
#     target's /etc/resolv.conf is put back, on every path.
#
# All logic lives in functions prefixed selinux_ (fix scripts share one
# shell, so names must not collide). The script never calls 'exit': later
# fix scripts and the completion signal must still run.

SYSROOT="/mnt/sysroot"
LOGFILE="${LOGFILE:-/var/log/gce-rescue.log}"

# Logs go to the log file and stderr (serial console). Never stdout: helpers
# print their result on stdout and are called as $(helper).
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

SELINUX_CONFIG="/etc/selinux/config"
SELINUX_AUTORELABEL="/.autorelabel"
# DNS settings of the rescue VM, lent to the chroot for the package download.
SELINUX_HOST_RESOLV="${SELINUX_HOST_RESOLV:-/etc/resolv.conf}"
# Time limit (seconds) for one package manager run inside the chroot.
SELINUX_PKG_TIMEOUT=300
# Directories mounted by this script, relative to $SYSROOT, in mount order.
SELINUX_MOUNTS=""
SELINUX_CHROOT_READY=0
SELINUX_BOOT_MOUNTED=0
SELINUX_RESOLV_SAVED=""

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

# Sets SELINUX_CONFIG_FILE to the config file as seen from the rescue system.
# A symlink is resolved inside the target (one level): an absolute link
# target would otherwise point into the RESCUE system's /etc. Returns 1 when
# there is no config file.
selinux_locate_config() {
    local link
    SELINUX_CONFIG_FILE="$SYSROOT$SELINUX_CONFIG"
    if [ -L "$SELINUX_CONFIG_FILE" ]; then
        link=$(readlink "$SELINUX_CONFIG_FILE")
        case "$link" in
            /*) SELINUX_CONFIG_FILE="$SYSROOT$link" ;;
            *) SELINUX_CONFIG_FILE="$SYSROOT/etc/selinux/$link" ;;
        esac
        log "$SELINUX_CONFIG is a symlink to $link"
    fi
    [ -f "$SELINUX_CONFIG_FILE" ] && [ ! -L "$SELINUX_CONFIG_FILE" ]
}

# Prints the value of a KEY=value setting in the target's SELinux config
# (last assignment wins; quotes, blanks and trailing comments stripped).
selinux_config_value() {
    awk -v key="$1" '
        /^[[:space:]]*#/ { next }
        {
            i = index($0, "=")
            if (i == 0) next
            k = substr($0, 1, i - 1); gsub(/[[:space:]]/, "", k)
            if (k != key) next
            v = substr($0, i + 1); sub(/#.*/, "", v)
        }
        END { print v }' "$SELINUX_CONFIG_FILE" 2>/dev/null \
        | tr -d "\"'[:space:]"
}

# Whether the diagnosis named a pattern (GCE_SELINUX_FINDINGS).
selinux_finding_seen() {
    local name
    for name in ${GCE_SELINUX_FINDINGS:-}; do
        [ "$name" = "$1" ] && return 0
    done
    return 1
}

# A policy type is only used in paths if it is a plain name.
selinux_safe_type() {
    case "$1" in
        ''|*[!A-Za-z0-9_-]*) return 1 ;;
    esac
    return 0
}

# Whether the binary policy of a policy type is installed and readable:
# libselinux loads /etc/selinux/<type>/policy/policy.<version>, a file that
# starts with the SELinux policy magic 0xf97cff8c (little-endian). An empty
# or truncated file fails to load exactly like a missing one.
selinux_policy_installed() {
    local f magic
    for f in "$SYSROOT/etc/selinux/$1/policy"/policy.[0-9]*; do
        [ -f "$f" ] && [ -s "$f" ] || continue
        magic=$(od -An -tx1 -N 4 "$f" 2>/dev/null | tr -d ' \n')
        [ "$magic" = "8cff7cf9" ] && return 0
        log "Ignoring ${f#"$SYSROOT"}: not an SELinux binary policy"
    done
    return 1
}

# Prints a policy type other than $1 whose binary policy is installed:
# targeted first, then any other.
selinux_other_installed_type() {
    local d t
    if [ "$1" != "targeted" ] && selinux_policy_installed targeted; then
        echo targeted
        return 0
    fi
    for d in "$SYSROOT"/etc/selinux/*/; do
        [ -d "$d" ] || continue
        t=$(basename "$d")
        [ "$t" = "$1" ] && continue
        selinux_safe_type "$t" || continue
        if selinux_policy_installed "$t"; then
            echo "$t"
            return 0
        fi
    done
    return 1
}

# Runs a command inside the target system. A fixed PATH and C locale make
# the result independent of the startup-script environment.
selinux_in_chroot() {
    chroot "$SYSROOT" /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive "$@"
}

# Runs a command inside the target with a time limit ($1 seconds); output
# goes to the log file only. Returns the command's exit status (124 when it
# timed out).
selinux_chroot_run() {
    local limit="$1"
    shift
    log "chroot: $*"
    timeout "$limit" chroot "$SYSROOT" /usr/bin/env -i \
        PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
        LC_ALL=C HOME=/root TERM=dumb DEBIAN_FRONTEND=noninteractive \
        "$@" >> "$LOGFILE" 2>&1
}

# Whether a command is installed in the target. Checked on the filesystem
# (relative symlinks such as dnf -> dnf-3 count), so no chroot is needed.
selinux_has_cmd() {
    local d
    for d in /usr/bin /usr/sbin /bin /sbin; do
        if [ -x "$SYSROOT$d/$1" ] || [ -L "$SYSROOT$d/$1" ]; then
            return 0
        fi
    done
    return 1
}

# Prints the target's package manager (dnf, yum, zypper or apt-get).
selinux_package_manager() {
    local pm
    for pm in dnf yum zypper apt-get; do
        if selinux_has_cmd "$pm"; then
            echo "$pm"
            return 0
        fi
    done
    return 1
}

# Prints the command a user runs inside the VM to reinstall the policy.
selinux_reinstall_hint() {
    case "${2:-dnf}" in
        apt-get) echo "sudo apt-get install -y --reinstall selinux-policy-$1" ;;
        zypper) echo "sudo zypper --non-interactive install --force selinux-policy-$1" ;;
        *) echo "sudo ${2:-dnf} -y reinstall selinux-policy-$1" ;;
    esac
}

# ---------------------------------------------------------------------------
# Kernel command line
# ---------------------------------------------------------------------------

# Prints (one per line) the files of the target that carry the kernel
# command line. A separate /boot that is not mounted simply has none of the
# /boot files.
selinux_cmdline_files() {
    local f
    for f in "$SYSROOT/etc/default/grub" "$SYSROOT/boot/grub2/grubenv" \
            "$SYSROOT/boot/grub2/grub.cfg" "$SYSROOT/boot/grub/grub.cfg" \
            "$SYSROOT"/boot/efi/EFI/*/grub.cfg \
            "$SYSROOT"/boot/loader/entries/*.conf; do
        [ -f "$f" ] && echo "$f"
    done
}

# Prints the kernel arguments of a file that turn SELinux off, make it
# permissive or request a relabel, space separated (comment lines dropped;
# quotes split words too).
selinux_cmdline_hits() {
    grep -vE '^[[:space:]]*#' "$1" 2>/dev/null \
        | tr "\"'\t " '\n\n\n\n' \
        | grep -xE 'selinux=0|enforcing=0|autorelabel(=[0-9]+)?' \
        | sort -u | tr '\n' ' '
}

# Reports (never changes) kernel command lines that turn SELinux off or make
# it permissive. Sets SELINUX_CMDLINE_AUTORELABEL=1 and
# SELINUX_CMDLINE_AUTORELABEL_FILES when 'autorelabel' is set; whether that
# is reported or removed is decided by selinux_repair.
selinux_report_cmdline() {
    local f hits
    SELINUX_CMDLINE_AUTORELABEL=0
    SELINUX_CMDLINE_AUTORELABEL_FILES=""
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        hits=$(selinux_cmdline_hits "$f")
        [ -n "$hits" ] || continue
        case " $hits" in
            *" selinux=0 "*|*" enforcing=0 "*)
                repair_line "[INFO] selinux: ${f#"$SYSROOT"} sets selinux=0 or enforcing=0 on the kernel command line; left unchanged (SELinux is not enforced while it is set, and a scheduled relabel only runs once selinux=0 is removed)" ;;
        esac
        case " $hits" in
            *" autorelabel "*|*" autorelabel="*)
                SELINUX_CMDLINE_AUTORELABEL=1
                SELINUX_CMDLINE_AUTORELABEL_FILES="$SELINUX_CMDLINE_AUTORELABEL_FILES ${f#"$SYSROOT"}" ;;
        esac
    done <<EOF
$(selinux_cmdline_files)
EOF
    return 0
}

# Prints the words of $2 with kernel argument $3 added ($1 = add; only when
# missing) or removed ($1 = remove; every "$3" and "$3=..." word).
selinux_edit_words() {
    local mode="$1" arg="$3" w out="" found=0
    local -a words
    read -ra words <<< "$2"
    for w in "${words[@]}"; do
        if [ "$mode" = "remove" ]; then
            case "$w" in "$arg"|"$arg"=*) continue ;; esac
        elif [ "$w" = "$arg" ]; then
            found=1
        fi
        out="${out:+$out }$w"
    done
    if [ "$mode" = "add" ] && [ "$found" = "0" ]; then
        out="${out:+$out }$arg"
    fi
    printf '%s' "$out"
}

# Edits the kernel command line in one file ($1, as seen from the rescue
# system): GRUB_CMDLINE_LINUX (and, when removing, _DEFAULT) in
# /etc/default/grub, the 'options' line of a BLS entry, or the linux lines
# of a grub.cfg. $2 = add|remove, $3 = argument. The file must already be
# backed up; it is written through, and restored from the backup when the
# write fails. Returns 1 only on a failed write.
selinux_edit_cmdline_file() {
    local f="$1" mode="$2" arg="$3" kind tmp line new pre q val changed=0
    local re_default="^([[:space:]]*GRUB_CMDLINE_LINUX(_DEFAULT)?=)([\"'])(.*)[\"'][[:space:]]*\$"
    local re_bls='^(options[[:space:]]+)(.*)$'
    local re_cfg='^([[:space:]]*linux(16|efi)?[[:space:]]+)(.*)$'
    case "$f" in
        */etc/default/grub) kind=default ;;
        */boot/loader/entries/*.conf) kind=bls ;;
        */grub.cfg) kind=cfg ;;
        *) return 0 ;;
    esac
    tmp="$SYSROOT$SELINUX_BACKUP_DIR/cmdline.tmp"
    while IFS= read -r line || [ -n "$line" ]; do
        new="$line"
        case "$kind" in
            default)
                if [[ $line =~ $re_default ]]; then
                    pre="${BASH_REMATCH[1]}"; q="${BASH_REMATCH[3]}"
                    val="${BASH_REMATCH[4]}"
                    if [ "$mode" = "remove" ] || [ -z "${BASH_REMATCH[2]}" ]; then
                        new="$pre$q$(selinux_edit_words "$mode" "$val" "$arg")$q"
                    fi
                fi ;;
            bls)
                if [[ $line =~ $re_bls ]]; then
                    new="${BASH_REMATCH[1]}$(selinux_edit_words "$mode" "${BASH_REMATCH[2]}" "$arg")"
                fi ;;
            cfg)
                if [[ $line =~ $re_cfg ]]; then
                    new="${BASH_REMATCH[1]}$(selinux_edit_words "$mode" "${BASH_REMATCH[3]}" "$arg")"
                fi ;;
        esac
        [ "$new" != "$line" ] && changed=1
        printf '%s\n' "$new"
    done < "$f" > "$tmp"
    if [ "$changed" = "0" ]; then
        rm -f "$tmp"
        return 0
    fi
    if [ -s "$tmp" ] && cat "$tmp" > "$f"; then
        rm -f "$tmp"
        log "Edited the kernel command line in ${f#"$SYSROOT"} ($mode $arg)"
        return 0
    fi
    rm -f "$tmp"
    log "Writing ${f#"$SYSROOT"} failed; restoring the backup"
    selinux_restore_file "$f"
    return 1
}

# RHEL 8 style BLS entries read their options from kernelopts= in grubenv,
# a fixed-size file that only grub2-editenv may rewrite. Removes $1 there.
selinux_remove_grubenv_arg() {
    local env="/boot/grub2/grubenv" val
    [ -f "$SYSROOT$env" ] || return 0
    case " $(selinux_cmdline_hits "$SYSROOT$env")" in
        *" $1 "*|*" $1="*) ;;
        *) return 0 ;;
    esac
    if ! selinux_has_cmd grub2-editenv; then
        log "$env sets $1 but grub2-editenv is not installed"
        return 1
    fi
    val=$(selinux_in_chroot grub2-editenv "$env" list 2>/dev/null \
        | sed -n 's/^kernelopts=//p')
    [ -n "$val" ] || return 0
    selinux_chroot_run 60 grub2-editenv "$env" set \
        "kernelopts=$(selinux_edit_words remove "$val" "$1")"
}

# Whether the boot entries now carry the change: no file sets $2 any more
# (remove), or every BLS entry - or, without BLS entries, every linux line
# of a grub.cfg - has $2 (add).
selinux_cmdline_verified() {
    local mode="$1" arg="$2" f hits checked=0
    if [ "$mode" = "remove" ]; then
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            hits=$(selinux_cmdline_hits "$f")
            case " $hits" in
                *" $arg "*|*" $arg="*)
                    log "${f#"$SYSROOT"} still sets $arg"
                    return 1 ;;
            esac
        done <<EOF
$(selinux_cmdline_files)
EOF
        return 0
    fi
    for f in "$SYSROOT"/boot/loader/entries/*.conf; do
        [ -f "$f" ] || continue
        checked=1
        grep -E '^options[[:space:]]' "$f" | tr '\t ' '\n\n' | grep -qx "$arg" \
            || { log "${f#"$SYSROOT"} does not set $arg"; return 1; }
    done
    [ "$checked" = "1" ] && return 0
    for f in "$SYSROOT/boot/grub2/grub.cfg" "$SYSROOT/boot/grub/grub.cfg" \
            "$SYSROOT"/boot/efi/EFI/*/grub.cfg; do
        [ -f "$f" ] || continue
        if grep -qE '^[[:space:]]*linux(16|efi)?[[:space:]]' "$f"; then
            checked=1
            if grep -E '^[[:space:]]*linux(16|efi)?[[:space:]]' "$f" \
                    | grep -vE "[[:space:]]$arg([[:space:]]|\$)" | grep -q .; then
                log "${f#"$SYSROOT"} has a boot entry without $arg"
                return 1
            fi
        fi
    done
    [ "$checked" = "1" ] || log "No boot entries found to carry $arg"
    [ "$checked" = "1" ]
}

# Adds ($1 = add) or removes ($1 = remove) kernel argument $2 for every
# kernel: grubby inside the chroot when the target has it (it also handles
# grubenv), then a direct edit of every command-line file it left
# unchanged. Every file is backed up first; backups of files that did not
# change are dropped again. Sets SELINUX_CMDLINE_CHANGED to the files that
# changed. Returns 0 when the boot entries carry the change.
selinux_edit_cmdline() {
    local mode="$1" arg="$2" f files ok=0
    SELINUX_CMDLINE_CHANGED=""
    selinux_prepare_chroot || return 1
    files=$(selinux_cmdline_files)
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        selinux_backup_file "$f" || return 1
    done <<EOF
$files
EOF
    if selinux_has_cmd grubby; then
        if [ "$mode" = "add" ]; then
            selinux_chroot_run 60 grubby --update-kernel=ALL "--args=$arg"
        else
            selinux_chroot_run 60 grubby --update-kernel=ALL "--remove-args=$arg"
        fi || log "grubby could not update the boot entries; editing them directly"
    fi
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        selinux_edit_cmdline_file "$f" "$mode" "$arg" || return 1
    done <<EOF
$files
EOF
    if [ "$mode" = "remove" ]; then
        selinux_remove_grubenv_arg "$arg" || log "Could not update kernelopts in /boot/grub2/grubenv"
    fi
    selinux_cmdline_verified "$mode" "$arg" && ok=1
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        if selinux_drop_unchanged_backup "$f"; then
            SELINUX_CMDLINE_CHANGED="${SELINUX_CMDLINE_CHANGED:+$SELINUX_CMDLINE_CHANGED, }${f#"$SYSROOT"}"
        fi
    done <<EOF
$files
EOF
    [ "$ok" = "1" ]
}

# ---------------------------------------------------------------------------
# Chroot (bind mounts, /boot, DNS)
# ---------------------------------------------------------------------------

# Prints the partition of the AFFECTED disk that an fstab spec names, or
# nothing. Only partitions of /dev/disk/by-id/google-$disk are considered:
# the rescue disk can carry the same UUIDs/labels when both come from the
# same image.
selinux_resolve_spec() {
    local spec="$1" key="" val="" part num="" got
    case "$spec" in
        UUID=*) key=UUID; val="${spec#UUID=}" ;;
        LABEL=*) key=LABEL; val="${spec#LABEL=}" ;;
        PARTUUID=*) key=PARTUUID; val="${spec#PARTUUID=}" ;;
        /dev/disk/by-uuid/*) key=UUID; val="${spec##*/}" ;;
        /dev/disk/by-label/*) key=LABEL; val="${spec##*/}" ;;
        /dev/disk/by-partuuid/*) key=PARTUUID; val="${spec##*/}" ;;
        /dev/*) key=NUM; num=$(echo "$spec" | grep -oE '[0-9]+$') ;;
        *) return 1 ;;
    esac
    val=$(echo "$val" | tr -d '"' | tr 'A-Z' 'a-z')
    [ -n "${disk:-}" ] && [ -e "/dev/disk/by-id/google-$disk" ] || return 1
    for part in $(lsblk -lnpo NAME,TYPE "/dev/disk/by-id/google-$disk" 2>/dev/null \
            | awk '$2 == "part" {print $1}'); do
        if [ "$key" = "NUM" ]; then
            [ -n "$num" ] && echo "$part" | grep -qE "[^0-9]${num}\$" \
                && { echo "$part"; return 0; }
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

# Mounts /boot or /boot/efi ($1) when the target's fstab lists it as a
# separate filesystem. Returns 0 when it is mounted or not listed.
selinux_mount_target() {
    local mp="$1" spec fstype dev
    mountpoint -q "$SYSROOT$mp" && return 0
    read -r spec fstype <<EOF
$(awk -v mp="$mp" '$1 !~ /^#/ && $2 == mp {print $1, $3; exit}' "$SYSROOT/etc/fstab" 2>/dev/null)
EOF
    [ -z "$spec" ] && return 0
    dev=$(selinux_resolve_spec "$spec")
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
        SELINUX_MOUNTS="$SELINUX_MOUNTS ${mp#/}"
        log "Mounted $dev ($spec) at $SYSROOT$mp"
        return 0
    fi
    log "Failed to mount $dev ($spec) at $SYSROOT$mp"
    return 1
}

# Bind-mounts /dev, /proc, /sys and /run of the rescue system into the
# target (those not mounted yet) and mounts a separate /boot and /boot/efi.
# Returns 1 when a separate /boot cannot be mounted: the boot entries would
# be edited on the empty mount point otherwise.
selinux_prepare_chroot() {
    local d
    if [ "$SELINUX_CHROOT_READY" != "1" ]; then
        for d in dev proc sys run; do
            [ -d "$SYSROOT/$d" ] || continue
            mountpoint -q "$SYSROOT/$d" && continue
            if mount -o bind "/$d" "$SYSROOT/$d" 2>/dev/null; then
                SELINUX_MOUNTS="$SELINUX_MOUNTS $d"
            else
                log "Could not bind-mount /$d into $SYSROOT"
            fi
        done
        SELINUX_CHROOT_READY=1
        SELINUX_BOOT_MOUNTED=0
        if selinux_mount_target /boot; then
            SELINUX_BOOT_MOUNTED=1
            selinux_mount_target /boot/efi || log "WARNING: /boot/efi is not mounted"
        else
            log "The separate /boot partition is not mounted"
        fi
    fi
    [ "$SELINUX_BOOT_MOUNTED" = "1" ]
}

# Unmounts what selinux_prepare_chroot mounted, in reverse order.
selinux_release_chroot() {
    local d rev=""
    for d in $SELINUX_MOUNTS; do
        rev="$d $rev"
    done
    for d in $rev; do
        umount "$SYSROOT/$d" 2>/dev/null || umount -l "$SYSROOT/$d" 2>/dev/null \
            || log "Could not unmount $SYSROOT/$d"
    done
    SELINUX_MOUNTS=""
    SELINUX_CHROOT_READY=0
    SELINUX_BOOT_MOUNTED=0
}

# Lends the rescue system's DNS settings to the chroot. A regular target
# resolv.conf is backed up and written through; a symlink (often into /run,
# which now shows the RESCUE system's /run) is moved aside. Sets
# SELINUX_RESOLV_SAVED to file, link or absent for selinux_restore_resolv.
selinux_use_host_resolv() {
    local target="$SYSROOT/etc/resolv.conf"
    SELINUX_RESOLV_SAVED=""
    [ -s "$SELINUX_HOST_RESOLV" ] || { log "The rescue system has no $SELINUX_HOST_RESOLV"; return 1; }
    if [ -L "$target" ]; then
        mv "$target" "$target.gce-rescue-orig" || return 1
        SELINUX_RESOLV_SAVED=link
        cat "$SELINUX_HOST_RESOLV" > "$target"
    elif [ -e "$target" ]; then
        selinux_backup_file "$target" || return 1
        SELINUX_RESOLV_SAVED=file
        cat "$SELINUX_HOST_RESOLV" > "$target"
    else
        SELINUX_RESOLV_SAVED=absent
        cat "$SELINUX_HOST_RESOLV" > "$target"
    fi
    log "Using the rescue system's DNS settings inside the chroot"
}

# Puts the target's resolv.conf back (no-op when it was not replaced).
selinux_restore_resolv() {
    local target="$SYSROOT/etc/resolv.conf"
    case "$SELINUX_RESOLV_SAVED" in
        link)
            rm -f "$target"
            mv "$target.gce-rescue-orig" "$target" ;;
        file)
            selinux_restore_file "$target" \
                && rm -f "$SYSROOT$SELINUX_BACKUP_DIR/$(selinux_backup_name "$target")" ;;
        absent)
            rm -f "$target" ;;
        *) return 0 ;;
    esac
    SELINUX_RESOLV_SAVED=""
    log "Restored the target's /etc/resolv.conf"
}

# Reinstalls the binary policy of type $1 with the target's package
# manager inside the chroot. Sets SELINUX_PM. Returns 0 when the policy is
# valid afterwards; the exit status of the package manager is not trusted
# ('install' of a package that is already installed succeeds without
# restoring missing files).
selinux_reinstall_policy() {
    local ptype="$1" pkg="selinux-policy-$1" step rc ok=1 cmd
    SELINUX_PM=$(selinux_package_manager) || {
        SELINUX_PM=""
        log "No supported package manager (dnf, yum, zypper, apt-get) in the target"
        return 1
    }
    # The package manager does not need /boot; only the bind mounts matter.
    selinux_prepare_chroot || log "Continuing without the separate /boot"
    selinux_use_host_resolv || log "Continuing without DNS settings for the chroot"
    log "Reinstalling $pkg with $SELINUX_PM inside the target (up to $SELINUX_PKG_TIMEOUT s per attempt)"
    if [ "$SELINUX_PM" = "apt-get" ]; then
        selinux_chroot_run "$SELINUX_PKG_TIMEOUT" apt-get update
    fi
    for step in 1 2 3; do
        case "$SELINUX_PM:$step" in
            dnf:1|yum:1) cmd="$SELINUX_PM -y reinstall $pkg" ;;
            # The installed version may be gone from the repositories, so
            # reinstall fails while an upgrade restores the files.
            dnf:2|yum:2) cmd="$SELINUX_PM -y upgrade $pkg" ;;
            dnf:3|yum:3) cmd="$SELINUX_PM -y install $pkg" ;;
            zypper:1) cmd="zypper --non-interactive install --force $pkg" ;;
            apt-get:1) cmd="apt-get -y install --reinstall $pkg" ;;
            *) break ;;
        esac
        # shellcheck disable=SC2086
        selinux_chroot_run "$SELINUX_PKG_TIMEOUT" $cmd
        rc=$?
        if selinux_policy_installed "$ptype"; then
            ok=0
            break
        fi
        log "'$cmd' did not restore the $ptype policy (exit status $rc)"
        [ "$rc" = "124" ] && break
    done
    selinux_restore_resolv
    return "$ok"
}

# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

# Name of the backup of a file: its path inside the target, '/' -> '_'.
selinux_backup_name() {
    local rel="${1#"$SYSROOT"}"
    rel="${rel#/}"
    echo "${rel//\//_}"
}

# Saves a file ($1, as seen from the rescue system) in the backup
# directory, once per run. Returns non-zero when the copy fails: the file
# must not be changed then.
selinux_backup_file() {
    local dest
    dest="$SYSROOT$SELINUX_BACKUP_DIR/$(selinux_backup_name "$1")"
    [ -f "$dest" ] && return 0
    if cp -p "$1" "$dest" 2>/dev/null; then
        log "Backed up ${1#"$SYSROOT"} to ${dest#"$SYSROOT"}"
        return 0
    fi
    rm -f "$dest" 2>/dev/null
    log "Could not back up ${1#"$SYSROOT"}"
    return 1
}

# Puts a saved file back, writing through the existing file.
selinux_restore_file() {
    local src
    src="$SYSROOT$SELINUX_BACKUP_DIR/$(selinux_backup_name "$1")"
    [ -f "$src" ] && cat "$src" > "$1"
}

# Drops the backup of a file that did not change. Returns 0 when the file
# changed (the backup is kept).
selinux_drop_unchanged_backup() {
    local src
    src="$SYSROOT$SELINUX_BACKUP_DIR/$(selinux_backup_name "$1")"
    [ -f "$src" ] || return 1
    if cmp -s "$src" "$1"; then
        rm -f "$src"
        return 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------

# Sets KEY=value ($1, $2) in the target's SELinux config (backup first;
# restored on failure). Only that line changes: other settings and comments
# stay.
selinux_set_value() {
    local key="$1" value="$2" tmp
    selinux_backup_file "$SELINUX_CONFIG_FILE" || return 1
    tmp="$SYSROOT$SELINUX_BACKUP_DIR/config.tmp"
    if grep -qE "^[[:space:]]*$key[[:space:]]*=" "$SELINUX_CONFIG_FILE"; then
        sed -E "s/^[[:space:]]*$key[[:space:]]*=.*/$key=$value/" \
            "$SELINUX_CONFIG_FILE" > "$tmp"
    else
        { cat "$SELINUX_CONFIG_FILE"; echo "$key=$value"; } > "$tmp"
    fi
    if [ -s "$tmp" ] && cat "$tmp" > "$SELINUX_CONFIG_FILE" \
            && [ "$(selinux_config_value "$key")" = "$value" ]; then
        rm -f "$tmp"
        log "Set $key=$value in $SELINUX_CONFIG"
        return 0
    fi
    rm -f "$tmp"
    log "Writing $SELINUX_CONFIG failed; restoring the backup"
    selinux_restore_file "$SELINUX_CONFIG_FILE"
    return 1
}

# Creates an empty, root-owned /.autorelabel so the next boot relabels
# every file. That boot consumes (deletes) the file, so it needs no backup.
selinux_schedule_relabel() {
    if : > "$SYSROOT$SELINUX_AUTORELABEL" 2>/dev/null; then
        chown 0:0 "$SYSROOT$SELINUX_AUTORELABEL" 2>/dev/null || true
        chmod 0644 "$SYSROOT$SELINUX_AUTORELABEL" 2>/dev/null || true
        log "Created $SELINUX_AUTORELABEL"
        return 0
    fi
    log "Could not create $SELINUX_AUTORELABEL"
    return 1
}

# Final steps on every path after the backup directory exists: undo the
# chroot mounts and DNS settings, then drop the backup directories when
# nothing was backed up (/var/backups itself only when this run created it).
selinux_finish() {
    selinux_restore_resolv
    selinux_release_chroot
    rm -f "$SYSROOT$SELINUX_BACKUP_DIR"/config.tmp \
        "$SYSROOT$SELINUX_BACKUP_DIR"/cmdline.tmp 2>/dev/null
    if rmdir "$SYSROOT$SELINUX_BACKUP_DIR" 2>/dev/null \
            && rmdir "$SYSROOT/var/backups/gce-rescue" 2>/dev/null \
            && [ "$SELINUX_BACKUPS_CREATED" = "1" ]; then
        rmdir "$SYSROOT/var/backups" 2>/dev/null
    fi
    sync
}

# Reports one applied fix: $1 is the [FIXED] message.
selinux_success() {
    repair_line "[FIXED] selinux: $1"
    selinux_finish
    log "=== selinux repair completed: 1 fix applied ==="
    repair_result "SUCCESS:1"
}

# Applies the permissive fallback and reports the outcome. $1 is the
# [FIXED] message.
selinux_permissive_fix() {
    if selinux_set_value SELINUX permissive; then
        selinux_success "$1"
        return
    fi
    selinux_finish
    repair_result "FAILED:could not set SELINUX=permissive in $SELINUX_CONFIG"
}

# Case 1: the configured policy ($1; $2 = the type to install when $1 is
# not a usable name) is not installed. Switches to another installed type,
# or reinstalls the policy, or - when neither is possible - disables SELinux
# on the kernel command line so the VM boots.
selinux_fix_missing_policy() {
    local type="$1" ptype="$2" other hint undo
    if other=$(selinux_other_installed_type "$type"); then
        if ! selinux_set_value SELINUXTYPE "$other"; then
            selinux_finish
            repair_result "FAILED:could not set SELINUXTYPE=$other in $SELINUX_CONFIG"
            return
        fi
        if ! selinux_schedule_relabel; then
            selinux_finish
            repair_result "FAILED:set SELINUXTYPE=$other but could not create /.autorelabel; files keep the labels of the $type policy"
            return
        fi
        selinux_success "SELINUXTYPE=$type has no installed policy; switched to $other (SELINUX= unchanged) and scheduled a full relabel (/.autorelabel) for the $other policy; the next boot takes several minutes longer and reboots once"
        return
    fi

    if selinux_reinstall_policy "$ptype"; then
        if [ "$ptype" != "$type" ] && ! selinux_set_value SELINUXTYPE "$ptype"; then
            selinux_finish
            repair_result "FAILED:reinstalled the $ptype policy but could not set SELINUXTYPE=$ptype in $SELINUX_CONFIG"
            return
        fi
        if ! selinux_schedule_relabel; then
            selinux_finish
            repair_result "FAILED:reinstalled the $ptype policy but could not create /.autorelabel"
            return
        fi
        selinux_success "the $ptype policy was missing (no valid /etc/selinux/$ptype/policy/policy.*); reinstalled selinux-policy-$ptype with $SELINUX_PM and scheduled a full relabel (/.autorelabel); the next boot takes several minutes longer and reboots once"
        return
    fi

    # Case 1c: no policy can be installed. Permissive mode does not boot
    # without a policy (systemd cannot label its sockets), selinux=0 does.
    hint=$(selinux_reinstall_hint "$ptype" "$SELINUX_PM")
    if selinux_edit_cmdline add selinux=0; then
        undo="$hint"
        if selinux_has_cmd grubby; then
            undo="$undo && sudo grubby --update-kernel=ALL --remove-args=selinux=0"
        fi
        case ", $SELINUX_CMDLINE_CHANGED" in
            *", /etc/default/grub"*)
                undo="$undo && sudo sed -i 's/ *selinux=0//' /etc/default/grub" ;;
        esac
        if ! selinux_has_cmd grubby; then
            undo="$undo && remove selinux=0 from the boot entries (regenerate the GRUB config)"
        fi
        undo="$undo && sudo touch /.autorelabel && sudo reboot"
        selinux_success "the $ptype policy is missing (no valid /etc/selinux/$ptype/policy/policy.*) and could not be reinstalled from rescue mode (see /var/log/gce-repair.log on the VM); added selinux=0 to the kernel command line (${SELINUX_CMDLINE_CHANGED:-boot entries}) so the VM boots. SELinux is DISABLED until the policy package is reinstalled. To turn it back on, run inside the VM: $undo"
        return
    fi
    selinux_finish
    repair_result "FAILED:the $ptype policy is missing and could not be reinstalled, and selinux=0 could not be added to the kernel command line; run gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT first, then reinstall it: sudo chroot /mnt/sysroot ${hint#sudo }"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

selinux_repair() {
    local mode type ptype reinstall why

    if [ ! -d "$SYSROOT" ] || ! mountpoint -q "$SYSROOT"; then
        repair_result "FAILED:sysroot not mounted at $SYSROOT"
        return
    fi
    if ! touch "$SYSROOT/.gce-rescue-write-test" 2>/dev/null; then
        repair_result "FAILED:the root filesystem is mounted read-only"
        return
    fi
    rm -f "$SYSROOT/.gce-rescue-write-test"
    if [ ! -d "$SYSROOT/etc" ]; then
        repair_result "FAILED:the mounted partition is not a Linux root filesystem"
        return
    fi
    if ! selinux_locate_config; then
        repair_line "[INFO] selinux: no $SELINUX_CONFIG on the disk; this system does not use SELinux"
        repair_result "NO_ISSUES:0"
        return
    fi

    mode=$(selinux_config_value SELINUX | tr 'A-Z' 'a-z')
    type=$(selinux_config_value SELINUXTYPE)
    type="${type:-targeted}"
    log "SELinux config: SELINUX=${mode:-<unset>} SELINUXTYPE=$type"
    log "Diagnosed selinux findings: ${GCE_SELINUX_FINDINGS:-<none>}"

    selinux_report_cmdline

    if [ "$mode" = "disabled" ]; then
        repair_line "[INFO] selinux: SELINUX=disabled in $SELINUX_CONFIG; the policy is not loaded at boot, nothing to repair"
        repair_result "NO_ISSUES:0"
        return
    fi

    ptype="$type"
    if ! selinux_safe_type "$type"; then
        log "SELINUXTYPE='$type' is not a valid policy name"
        ptype=targeted
    fi
    reinstall=$(selinux_reinstall_hint "$ptype" "$(selinux_package_manager)")

    SELINUX_BACKUP_DIR="/var/backups/gce-rescue/selinux-$(date '+%Y%m%d-%H%M%S')"
    SELINUX_BACKUPS_CREATED=0
    [ -d "$SYSROOT/var/backups" ] || SELINUX_BACKUPS_CREATED=1
    if ! mkdir -p "$SYSROOT$SELINUX_BACKUP_DIR"; then
        repair_result "FAILED:could not create the backup directory $SELINUX_BACKUP_DIR"
        return
    fi

    # Case 1: the configured policy is not installed. No relabel can load a
    # policy that is not there, and permissive mode does not boot without
    # one either.
    if ! selinux_safe_type "$type" || ! selinux_policy_installed "$type"; then
        selinux_fix_missing_policy "$type" "$ptype"
        return
    fi

    # Case 2: a relabel requested on the kernel command line runs on every
    # boot and reboots: remove the argument. Checked before /.autorelabel,
    # whose permissive fallback cannot end that loop.
    if [ "$SELINUX_CMDLINE_AUTORELABEL" = "1" ] \
            && selinux_finding_seen selinux_relabel_required; then
        if selinux_edit_cmdline remove autorelabel; then
            selinux_success "removed autorelabel from the kernel command line (${SELINUX_CMDLINE_CHANGED:-boot entries}); it made every boot relabel the whole filesystem and reboot, so the VM never finished booting"
            return
        fi
        selinux_finish
        repair_result "FAILED:the kernel command line sets autorelabel, so every boot relabels and reboots, and it could not be removed automatically; run gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT first, then: sudo chroot /mnt/sysroot grubby --update-kernel=ALL --remove-args=autorelabel (and remove it from GRUB_CMDLINE_LINUX in /mnt/sysroot/etc/default/grub), then gce-rescue restore VM_NAME --zone=ZONE --project=PROJECT"
        return
    fi
    if [ "$SELINUX_CMDLINE_AUTORELABEL" = "1" ]; then
        repair_line "[INFO] selinux:$SELINUX_CMDLINE_AUTORELABEL_FILES set autorelabel on the kernel command line; left unchanged (every boot relabels until it is removed: grubby --update-kernel=ALL --remove-args=autorelabel)"
    fi

    # Case 3: the policy is installed but a relabel was already scheduled
    # and the VM still failed. The policy loads, so this is a labelling
    # problem: another relabel will not help, permissive mode boots.
    if [ -e "$SYSROOT$SELINUX_AUTORELABEL" ]; then
        if selinux_finding_seen selinux_relabel_required; then
            why="the relabel started but the VM never finished booting"
        else
            why="the $type policy failed to load even though it is installed"
        fi
        if [ "$mode" = "permissive" ]; then
            selinux_finish
            repair_result "FAILED:/.autorelabel is already present and SELINUX=permissive, yet $why; the policy store /etc/selinux/$type may be corrupt - run gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT first, then reinstall it: sudo chroot /mnt/sysroot ${reinstall#sudo }"
            return
        fi
        selinux_permissive_fix "/.autorelabel was already present and $why; set SELINUX=permissive so the VM boots (the relabel still runs). Check the policy store from inside the VM ($reinstall), then set SELINUX=enforcing"
        return
    fi

    # Case 4: policy installed, no relabel scheduled yet. Wrong or missing
    # labels (disk restored or edited from another system, policy
    # changed) are fixed by a full relabel on the next boot.
    if selinux_schedule_relabel; then
        selinux_success "scheduled a full filesystem relabel on the next boot (/.autorelabel); that boot takes several minutes longer and reboots once when the relabel is done"
        return
    fi
    selinux_finish
    repair_result "FAILED:could not create /.autorelabel on the root filesystem"
}

log "=== selinux repair started ==="
if [ "${GCE_SELINUX_FIX_LIB_ONLY:-0}" != "1" ]; then
    selinux_repair
fi

# Copy full log to affected disk so it survives restore
cp "$LOGFILE" "$SYSROOT/var/log/gce-repair.log" 2>/dev/null || true
