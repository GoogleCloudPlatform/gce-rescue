#!/bin/bash
# GCE Repair - SELinux fix script
#
# Runs after the affected disk is mounted at /mnt/sysroot. Repairs the
# SELinux boot failures diagnose reports (core/diagnose_rules/selinux.yaml):
#
#   1. The policy named by SELINUXTYPE= in /etc/selinux/config is missing
#      or unreadable (no valid /etc/selinux/<type>/policy/policy.NN), so init
#      cannot load it and freezes the boot: the config is switched to
#      SELINUX=permissive so the VM boots. The user then reinstalls the
#      policy package and sets SELINUX=enforcing again.
#   2. The policy is installed: a full relabel is scheduled by creating an
#      empty /.autorelabel, so the next boot relabels every file.
#   3. /.autorelabel is ALREADY on the disk and the serial console shows the
#      relabel ran without the boot finishing (or the policy still failed to
#      load): another relabel will not help, so the fallback is
#      SELINUX=permissive.
#
# selinux=0, enforcing=0 and autorelabel on the kernel command line are
# reported, never changed: the command line is the customer's choice.
#
# Inputs:
#   GCE_SELINUX_FINDINGS  Names of the diagnosed selinux patterns, space
#                         separated (injected by the repair orchestrator;
#                         may be empty).
#
# Safety rules:
#   - /etc/selinux/config is backed up to /var/backups/gce-rescue/ on the
#     root filesystem before it is changed, and written through so that the
#     file keeps its inode and its own SELinux label. A failed write
#     restores the backup.
#   - Nothing is deleted. SELinux is never disabled, only made permissive.
#   - Nothing runs unless the mounted partition is a writable root
#     filesystem that carries an SELinux configuration.
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

# Reports (never changes) kernel command lines that turn SELinux off, make
# it permissive or request a relabel on every boot. Checked wherever the
# command line can be configured; a separate /boot that is not mounted
# simply has none of the /boot files. Sets SELINUX_CMDLINE_AUTORELABEL=1
# when 'autorelabel' is set.
selinux_report_cmdline() {
    local f hits
    SELINUX_CMDLINE_AUTORELABEL=0
    for f in "$SYSROOT/etc/default/grub" "$SYSROOT/boot/grub2/grubenv" \
            "$SYSROOT/boot/grub2/grub.cfg" "$SYSROOT/boot/grub/grub.cfg" \
            "$SYSROOT"/boot/loader/entries/*.conf; do
        [ -f "$f" ] || continue
        # One word per line (quotes split words too), comment lines dropped.
        hits=$(grep -vE '^[[:space:]]*#' "$f" 2>/dev/null \
            | tr "\"'\t " '\n\n\n\n' \
            | grep -xE 'selinux=0|enforcing=0|autorelabel(=[0-9]+)?' \
            | sort -u | tr '\n' ' ')
        [ -n "$hits" ] || continue
        case " $hits" in
            *" selinux=0 "*|*" enforcing=0 "*)
                repair_line "[INFO] selinux: ${f#"$SYSROOT"} sets selinux=0 or enforcing=0 on the kernel command line; left unchanged (SELinux is not enforced while it is set, and a scheduled relabel only runs once selinux=0 is removed)" ;;
        esac
        case " $hits" in
            *" autorelabel "*|*" autorelabel="*)
                SELINUX_CMDLINE_AUTORELABEL=1
                repair_line "[INFO] selinux: ${f#"$SYSROOT"} sets autorelabel on the kernel command line; left unchanged (every boot relabels until it is removed: grubby --update-kernel=ALL --remove-args=autorelabel)" ;;
        esac
    done
    return 0
}

# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

# Saves the current SELinux config in the backup directory, once per run.
# Returns non-zero when the copy fails: the config must not be changed then.
selinux_backup_config() {
    local dest="$SYSROOT$SELINUX_BACKUP_DIR/etc_selinux_config"
    [ -f "$dest" ] && return 0
    if cp -p "$SELINUX_CONFIG_FILE" "$dest" 2>/dev/null; then
        log "Backed up $SELINUX_CONFIG to $SELINUX_BACKUP_DIR/etc_selinux_config"
        return 0
    fi
    rm -f "$dest" 2>/dev/null
    log "Could not back up $SELINUX_CONFIG"
    return 1
}

# Puts the saved config back, writing through the existing file.
selinux_restore_config() {
    local src="$SYSROOT$SELINUX_BACKUP_DIR/etc_selinux_config"
    [ -f "$src" ] && cat "$src" > "$SELINUX_CONFIG_FILE"
}

# ---------------------------------------------------------------------------
# Fixes
# ---------------------------------------------------------------------------

# Sets SELINUX=permissive in the target's config (backup first; restored on
# failure). Only the SELINUX= line changes: SELINUXTYPE= and comments stay.
selinux_set_permissive() {
    local tmp
    selinux_backup_config || return 1
    tmp="$SYSROOT$SELINUX_BACKUP_DIR/config.tmp"
    if grep -qE '^[[:space:]]*SELINUX[[:space:]]*=' "$SELINUX_CONFIG_FILE"; then
        sed -E 's/^[[:space:]]*SELINUX[[:space:]]*=.*/SELINUX=permissive/' \
            "$SELINUX_CONFIG_FILE" > "$tmp"
    else
        { cat "$SELINUX_CONFIG_FILE"; echo "SELINUX=permissive"; } > "$tmp"
    fi
    if [ -s "$tmp" ] && cat "$tmp" > "$SELINUX_CONFIG_FILE" \
            && [ "$(selinux_config_value SELINUX)" = "permissive" ]; then
        rm -f "$tmp"
        log "Set SELINUX=permissive in $SELINUX_CONFIG"
        return 0
    fi
    rm -f "$tmp"
    log "Writing $SELINUX_CONFIG failed; restoring the backup"
    selinux_restore_config
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

# Final steps: drop the backup directories when nothing was backed up
# (/var/backups itself only when this run created it).
selinux_finish() {
    rm -f "$SYSROOT$SELINUX_BACKUP_DIR"/config.tmp 2>/dev/null
    if rmdir "$SYSROOT$SELINUX_BACKUP_DIR" 2>/dev/null \
            && rmdir "$SYSROOT/var/backups/gce-rescue" 2>/dev/null \
            && [ "$SELINUX_BACKUPS_CREATED" = "1" ]; then
        rmdir "$SYSROOT/var/backups" 2>/dev/null
    fi
    sync
}

# Applies the permissive fallback and reports the outcome. $1 is the
# [FIXED] message.
selinux_permissive_fix() {
    if selinux_set_permissive; then
        repair_line "[FIXED] selinux: $1"
        selinux_finish
        log "=== selinux repair completed: 1 fix applied ==="
        repair_result "SUCCESS:1"
        return
    fi
    selinux_finish
    repair_result "FAILED:could not set SELINUX=permissive in $SELINUX_CONFIG"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

selinux_repair() {
    local mode type reinstall why

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

    if selinux_safe_type "$type"; then
        reinstall="dnf reinstall selinux-policy-$type"
    else
        log "SELINUXTYPE='$type' is not a valid policy name"
        reinstall="set SELINUXTYPE= to an installed policy and reinstall it with dnf"
    fi

    SELINUX_BACKUP_DIR="/var/backups/gce-rescue/selinux-$(date '+%Y%m%d-%H%M%S')"
    SELINUX_BACKUPS_CREATED=0
    [ -d "$SYSROOT/var/backups" ] || SELINUX_BACKUPS_CREATED=1
    if ! mkdir -p "$SYSROOT$SELINUX_BACKUP_DIR"; then
        repair_result "FAILED:could not create the backup directory $SELINUX_BACKUP_DIR"
        return
    fi

    # Case 1: the configured policy is not installed. No relabel can load a
    # policy that is not there; permissive mode lets the VM boot so the
    # package can be reinstalled from inside.
    if ! selinux_safe_type "$type" || ! selinux_policy_installed "$type"; then
        if [ "$mode" = "permissive" ]; then
            repair_line "[INFO] selinux: the $type policy named in $SELINUX_CONFIG is not installed (no valid /etc/selinux/$type/policy/policy.*), but SELINUX=permissive already lets the VM boot; reinstall it from inside the VM: $reinstall"
            selinux_finish
            repair_result "NO_ISSUES:0"
            return
        fi
        selinux_permissive_fix "$SELINUX_CONFIG names the $type policy, which is not installed (no valid /etc/selinux/$type/policy/policy.*), so init could not load it; set SELINUX=permissive so the VM boots. Then reinstall the policy ($reinstall), set SELINUX=enforcing and run: touch /.autorelabel; reboot"
        return
    fi

    # Case 2: the policy is installed but a relabel was already scheduled
    # and the VM still failed. Another relabel will not help; permissive
    # mode lets it boot so the policy store can be inspected.
    if [ -e "$SYSROOT$SELINUX_AUTORELABEL" ]; then
        if selinux_finding_seen selinux_relabel_required; then
            why="the relabel started but the VM never finished booting"
        else
            why="the $type policy failed to load even though it is installed"
        fi
        if [ "$mode" = "permissive" ]; then
            selinux_finish
            repair_result "FAILED:/.autorelabel is already present and SELINUX=permissive, yet $why; the policy store /etc/selinux/$type may be corrupt - reinstall it from rescue mode: sudo chroot /mnt/sysroot $reinstall"
            return
        fi
        selinux_permissive_fix "/.autorelabel was already present and $why; set SELINUX=permissive so the VM boots (the relabel still runs). Check the policy store ($reinstall), then set SELINUX=enforcing"
        return
    fi

    # A relabel requested on the kernel command line runs on every boot:
    # one more scheduled relabel cannot end that loop.
    if [ "$SELINUX_CMDLINE_AUTORELABEL" = "1" ] \
            && selinux_finding_seen selinux_relabel_required; then
        selinux_finish
        repair_result "FAILED:the kernel command line sets autorelabel, so every boot relabels and reboots; remove it from rescue mode: sudo chroot /mnt/sysroot grubby --update-kernel=ALL --remove-args=autorelabel"
        return
    fi

    # Case 3: policy installed, no relabel scheduled yet. Wrong or missing
    # labels (disk restored or edited from another system, policy
    # changed) are fixed by a full relabel on the next boot.
    if selinux_schedule_relabel; then
        repair_line "[FIXED] selinux: scheduled a full filesystem relabel on the next boot (/.autorelabel); that boot takes several minutes longer and reboots once when the relabel is done"
        selinux_finish
        log "=== selinux repair completed: 1 fix applied ==="
        repair_result "SUCCESS:1"
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
