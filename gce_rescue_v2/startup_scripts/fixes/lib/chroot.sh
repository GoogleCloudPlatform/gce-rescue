#!/bin/bash
# GCE Repair - shared chroot helper for fix scripts (library, not a fix)
#
# Fix scripts that run commands inside the affected system declare
#
#     # gce-rescue-requires: chroot
#
# and the startup script composer inlines this file ONCE, before the fix
# scripts, so every fix uses the same bind mounts, DNS handling and clean
# command environment.
#
#   chroot_enter [--no-dns]   Bind-mount /proc, /sys, /dev, /dev/pts and /run
#                             into the target (only what is not mounted yet)
#                             and, unless --no-dns, give the target the DNS
#                             configuration of the running rescue OS so package
#                             managers can reach their repositories. Returns 1
#                             when the target root is not mounted; a bind that
#                             fails is logged, not fatal.
#   chroot_exec CMD...        Run CMD inside the target with a clean
#                             environment; stdout/stderr pass through. Returns
#                             CMD's exit code.
#   chroot_run SECS CMD...    Same, with a time limit; output goes to the log
#                             file only. Returns CMD's exit code (124 on
#                             timeout).
#   chroot_has_cmd NAME       Whether NAME runs inside the target (needs a
#                             working /bin/sh in the target).
#   chroot_pkg_mgr            Print the target's package manager (apt-get,
#                             dnf, yum or zypper), picked from OS_FLAVOR and
#                             confirmed by looking for it on the target disk
#                             (no chroot, so it works even when the target's
#                             libc is broken); returns 1 if none.
#   chroot_leave              Undo chroot_enter: put the target's own
#                             resolv.conf back exactly as it was and unmount,
#                             in reverse order, only what chroot_enter
#                             mounted. Safe to call more than once, or without
#                             chroot_enter.
#
# Not nestable: all fixes in one startup script share this state. A
# chroot_leave undoes everything every earlier chroot_enter set up, so each
# fix should pair its own chroot_enter with a chroot_leave on every path.
# When DNS was set up, chroot_enter also registers chroot_leave as the EXIT
# trap (unless the script already has one), so the target's resolv.conf is
# restored if the startup script is stopped mid-fix (for example by a VM
# shutdown).
#
# The target root is $SYSROOT (default /mnt/sysroot), read at call time.
# All names are prefixed chroot_ / CHROOT_ because fix scripts share one
# shell. Never calls 'exit': later fix scripts and the completion signal must
# still run.

CHROOT_PATH='/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
CHROOT_DNS_BACKUP_SUFFIX='.gce-rescue-chroot-backup'
# First line of the temporary resolv.conf. Also how an interrupted session's
# copy is recognised later, so changing it orphans copies left by older runs.
CHROOT_DNS_HEADER='# gce-rescue: temporary DNS settings used during repair; the original is restored afterwards'
CHROOT_MOUNTED=''
CHROOT_DNS_STATE=''

chroot_log() {
    if declare -F log >/dev/null 2>&1; then
        log "$1"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] [REPAIR] $1" >&2
    fi
}

chroot_target() {
    echo "${SYSROOT:-/mnt/sysroot}"
}

chroot_enter() {
    local root dir with_dns=1
    root=$(chroot_target)
    [ "${1:-}" = "--no-dns" ] && with_dns=0
    if ! mountpoint -q "$root"; then
        chroot_log "chroot: $root is not mounted"
        return 1
    fi
    for dir in proc sys dev dev/pts run; do
        if [ -d "$root/$dir" ] && ! mountpoint -q "$root/$dir"; then
            if mount -o bind "/$dir" "$root/$dir" 2>/dev/null; then
                # Prepend, so chroot_leave unmounts in reverse order.
                CHROOT_MOUNTED="$dir${CHROOT_MOUNTED:+ $CHROOT_MOUNTED}"
            else
                chroot_log "WARNING: chroot: could not bind-mount /$dir into $root"
            fi
        fi
    done
    if [ "$with_dns" = 1 ]; then
        chroot_dns_setup
        # Restore the target's resolv.conf even if the script is stopped
        # before the fix calls chroot_leave. Never replace an EXIT trap a
        # custom --fix-script set for itself: that script then owns cleanup
        # and must call chroot_leave from its own trap.
        if [ -z "$(trap -p EXIT)" ]; then
            trap chroot_leave EXIT
        else
            chroot_log "chroot: an EXIT trap is already set; not adding chroot_leave to it"
        fi
    fi
    local dns_note='unchanged'
    [ -n "$CHROOT_DNS_STATE" ] && dns_note='rescue OS'
    chroot_log "chroot: entered $root (added mounts: ${CHROOT_MOUNTED:-none}; DNS: $dns_note)"
    return 0
}

# Replace the target's resolv.conf with the running rescue OS's, keeping the original
# (regular file or symlink) as resolv.conf<CHROOT_DNS_BACKUP_SUFFIX>.
chroot_dns_setup() {
    local root conf backup
    [ -n "$CHROOT_DNS_STATE" ] && return 0
    root=$(chroot_target)
    conf="$root/etc/resolv.conf"
    backup="$conf$CHROOT_DNS_BACKUP_SUFFIX"
    [ -d "$root/etc" ] || return 0

    if [ -e "$backup" ] || [ -L "$backup" ]; then
        if { [ ! -e "$conf" ] && [ ! -L "$conf" ]; } \
                || grep -qF "$CHROOT_DNS_HEADER" "$conf" 2>/dev/null; then
            # An interrupted session left its copy (or nothing) in place:
            # the backup is still the system's own file.
            chroot_log "chroot: found the resolv.conf backup of an interrupted repair; keeping it as the original"
            CHROOT_DNS_STATE='backup'
        else
            # The system changed resolv.conf after that session; the old
            # backup is stale. Keep it (renamed) rather than delete it, and
            # if that is impossible leave DNS alone: the next mv would
            # overwrite the old backup, which may be the only copy left.
            if ! mv -f "$backup" "$backup.stale" 2>/dev/null; then
                chroot_log "WARNING: chroot: could not move the old resolv.conf backup aside; leaving DNS unchanged"
                return 0
            fi
            chroot_log "chroot: renamed a stale resolv.conf backup to $(basename "$backup").stale"
        fi
    fi
    if [ -z "$CHROOT_DNS_STATE" ]; then
        if [ -e "$conf" ] || [ -L "$conf" ]; then
            # mv keeps a symlink as a symlink (systemd-resolved stub), so
            # writing the copy below never follows it into the rescue OS's
            # /run, which chroot_enter has already bind-mounted.
            if ! mv "$conf" "$backup" 2>/dev/null; then
                chroot_log "WARNING: chroot: could not move resolv.conf aside; DNS inside the chroot may not work"
                return 0
            fi
            CHROOT_DNS_STATE='backup'
        else
            CHROOT_DNS_STATE='absent'
        fi
    fi
    rm -f "$conf" 2>/dev/null
    # CHROOT_HOST_RESOLV only exists so tests can supply the source file.
    if { echo "$CHROOT_DNS_HEADER"; cat "${CHROOT_HOST_RESOLV:-/etc/resolv.conf}"; } > "$conf" 2>/dev/null; then
        chroot_log "chroot: using the rescue OS's DNS configuration"
    else
        chroot_log "WARNING: chroot: could not write resolv.conf; DNS inside the chroot may not work"
    fi
    return 0
}

chroot_dns_restore() {
    local root conf backup
    root=$(chroot_target)
    conf="$root/etc/resolv.conf"
    backup="$conf$CHROOT_DNS_BACKUP_SUFFIX"
    case "$CHROOT_DNS_STATE" in
        backup)
            rm -f "$conf" 2>/dev/null
            if mv "$backup" "$conf" 2>/dev/null; then
                chroot_log "chroot: restored the original resolv.conf"
            else
                chroot_log "WARNING: chroot: could not restore resolv.conf; the original is $(basename "$backup")"
            fi
            ;;
        absent)
            rm -f "$conf" 2>/dev/null
            ;;
    esac
    CHROOT_DNS_STATE=''
}

chroot_leave() {
    local root dir
    root=$(chroot_target)
    chroot_dns_restore
    for dir in $CHROOT_MOUNTED; do
        umount "$root/$dir" 2>/dev/null \
            || umount -l "$root/$dir" 2>/dev/null \
            || chroot_log "WARNING: chroot: could not unmount $root/$dir"
    done
    CHROOT_MOUNTED=''
    return 0
}

# A fixed PATH and C locale make results independent of the startup-script
# environment.
chroot_exec() {
    chroot "$(chroot_target)" /usr/bin/env -i \
        PATH="$CHROOT_PATH" \
        LC_ALL=C HOME=/root TERM=dumb "$@"
}

chroot_run() {
    local limit="$1"
    shift
    chroot_log "chroot: $*"
    timeout "$limit" chroot "$(chroot_target)" /usr/bin/env -i \
        PATH="$CHROOT_PATH" \
        LC_ALL=C HOME=/root TERM=dumb "$@" >> "${LOGFILE:-/dev/null}" 2>&1
}

chroot_has_cmd() {
    chroot_exec /bin/sh -c 'command -v "$1"' sh "$1" >/dev/null 2>&1
}

# Whether a package manager is installed on the target disk. A plain file
# check, not chroot_has_cmd: it must work when the target's /bin/sh or libc
# is broken (#201), which is when a reinstall is needed most. /bin and /sbin
# are only searched when they are real directories, because an absolute
# merged-/usr symlink would resolve against the rescue OS instead.
chroot_pkg_mgr_installed() {
    local root dir
    root=$(chroot_target)
    for dir in usr/bin usr/sbin bin sbin; do
        [ -L "$root/$dir" ] && continue
        if [ -f "$root/$dir/$1" ] || [ -L "$root/$dir/$1" ]; then
            return 0
        fi
    done
    return 1
}

chroot_pkg_mgr() {
    local flavor guess pm
    flavor=$(printf '%s' "${OS_FLAVOR:-}" | tr 'A-Z' 'a-z')
    case "$flavor" in
        debian*|ubuntu*) guess='apt-get' ;;
        sles*|suse*|opensuse*) guess='zypper' ;;
        rhel-7*|centos-7*|oracle-linux-7*|ol-7*) guess='yum' ;;
        rhel*|centos*|rocky*|alma*|fedora*|oracle*|ol-*) guess='dnf' ;;
        *) guess='' ;;
    esac
    if [ -n "$guess" ] && chroot_pkg_mgr_installed "$guess"; then
        echo "$guess"
        return 0
    fi
    # Unknown flavor (custom image, disk name) or the guess is not installed:
    # look at the target disk itself.
    for pm in dnf yum zypper apt-get; do
        if chroot_pkg_mgr_installed "$pm"; then
            echo "$pm"
            return 0
        fi
    done
    return 1
}
