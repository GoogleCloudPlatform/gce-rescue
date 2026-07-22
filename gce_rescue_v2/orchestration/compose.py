"""
GCE Rescue - Startup script composition.

Combines the base mount script with fix script(s) into a single startup
script, relocating the completion marker so verification (and any restore
that follows) only proceeds after all fixes have finished.

Shared by the repair orchestrator (diagnosis-driven fixes) and the rescue
orchestrator (custom fix scripts via --fix-script).
"""

from typing import List, Optional, Tuple

# Completion marker emitted by the base mount script to the serial console
RESCUE_COMPLETE_MARKER = 'GCE-RESCUE-COMPLETE'

# Delimiters for an optional pre-mount block inside a fix script. Code between
# these exact marker lines is lifted out of the fix script and injected into
# the base mount script BEFORE the mount attempt (see PREMOUNT_ANCHOR), so a
# fix can repair the filesystem itself (e.g. fsck a corrupt superblock) before
# the base script tries to mount it — a failed mount would otherwise abort the
# startup script and no fix would ever run.
PREMOUNT_BEGIN_MARKER = '# === GCE-REPAIR-PREMOUNT-BEGIN ==='
PREMOUNT_END_MARKER = '# === GCE-REPAIR-PREMOUNT-END ==='

# Injection point in the base mount script (rescue_mount.sh): the first line
# of the filesystem-detection/mount section. At this point the disk-wait loop
# has finished, so /dev/disk/by-id/google-${disk} exists and $disk is set, but
# nothing has been mounted yet.
PREMOUNT_ANCHOR = 'log "Detecting filesystem type..."'


def extract_premount_blocks(fix_script: str) -> Tuple[List[str], str]:
    """Split a fix script into its pre-mount blocks and post-mount remainder.

    Pre-mount blocks are delimited by PREMOUNT_BEGIN_MARKER and
    PREMOUNT_END_MARKER lines (matched ignoring surrounding whitespace); the
    marker lines themselves are not included in either part. A script may
    contain zero or more blocks.

    Args:
        fix_script: Fix script body (shebang already stripped).

    Returns:
        Tuple of (pre-mount blocks in order of appearance, remainder script
        with the blocks and marker lines removed).

    Raises:
        ValueError: If a begin marker has no matching end marker (or vice
            versa) — running half a pre-mount block would be unsafe.
    """
    blocks: List[str] = []
    remainder_lines: List[str] = []
    current_block: Optional[List[str]] = None

    for line in fix_script.split('\n'):
        stripped = line.strip()
        if stripped == PREMOUNT_BEGIN_MARKER:
            if current_block is not None:
                raise ValueError(
                    'Malformed fix script: nested '
                    f'{PREMOUNT_BEGIN_MARKER} marker (previous block '
                    'was never closed)'
                )
            current_block = []
        elif stripped == PREMOUNT_END_MARKER:
            if current_block is None:
                raise ValueError(
                    f'Malformed fix script: {PREMOUNT_END_MARKER} '
                    'without a preceding begin marker'
                )
            blocks.append('\n'.join(current_block))
            current_block = None
        elif current_block is not None:
            current_block.append(line)
        else:
            remainder_lines.append(line)

    if current_block is not None:
        raise ValueError(
            f'Malformed fix script: {PREMOUNT_BEGIN_MARKER} '
            'without a closing end marker'
        )

    return blocks, '\n'.join(remainder_lines)


def strip_shebang(script: str) -> str:
    """Remove a leading shebang line (already present in the base script)."""
    if script.startswith('#!'):
        parts = script.split('\n', 1)
        return parts[1] if len(parts) > 1 else ''
    return script


def compose_startup_script(base_script: str, fix_scripts: List[str],
                           repair_targets: Optional[List[str]] = None) -> str:
    """Combine the base mount script with fix script(s) into one startup script.

    The base script's GCE-RESCUE-COMPLETE marker is relocated to the very end
    so the orchestrator's verification (and any restore that follows) only
    proceeds after ALL fix scripts have finished — never mid-fix.

    Fix scripts may carry an optional pre-mount block (see
    extract_premount_blocks). Those blocks are lifted out and injected into
    the base script right before the PREMOUNT_ANCHOR line — i.e. after the
    disk-wait loop but before any mount attempt — so filesystem-level repairs
    (fsck) can run even when the disk would fail to mount. The remainder of
    each fix script is appended at the end as usual. Scripts without a
    pre-mount block are composed exactly as before.

    Args:
        base_script: The mount script, with disk placeholder already resolved.
        fix_scripts: Fix script bodies to append (shebangs already stripped).
        repair_targets: Identifiers extracted from diagnosis for targeted
            fixing. Pass a list (possibly empty) to inject the REPAIR_TARGETS
            variable for diagnosis-driven fixes; pass None for self-contained
            (custom) fix scripts that need no targets.

    Returns:
        The combined startup script.

    Raises:
        ValueError: If a fix script has a malformed pre-mount block, or if a
            pre-mount block is present but the base script lacks the
            PREMOUNT_ANCHOR line (the block would silently never run).
    """
    # Lift pre-mount blocks out of the fix scripts (order preserved)
    premount_blocks: List[str] = []
    fix_bodies: List[str] = []
    for fix_script in fix_scripts:
        blocks, remainder = extract_premount_blocks(fix_script)
        premount_blocks.extend(blocks)
        fix_bodies.append(remainder)

    # Move the completion signal (serial marker + guest attribute) to the very
    # end so verification only proceeds after all fixes finish. The base script
    # calls signal_complete; relocate that call (the echo lives inside the
    # function definition, which must stay intact).
    # Match the bare call ("signal_complete\n") which only appears at the call
    # site; the definition line is "signal_complete() {" so it is not matched.
    combined = base_script.replace(
        'signal_complete\n',
        '# completion signal moved to end (repair mode)\n',
        1,
    )
    # The base script's trailing success log must move with the signal:
    # left in place it prints BEFORE the fixes run, and the on-disk repair
    # log then claims completion ahead of the actual fix output.
    combined = combined.replace(
        'log "=== Startup script completed successfully ==="\n',
        '# completion log moved to end (repair mode)\n',
        1,
    )

    # Inject pre-mount blocks before the base script's mount section
    if premount_blocks:
        if PREMOUNT_ANCHOR not in combined:
            raise ValueError(
                'Cannot inject pre-mount fix blocks: anchor line '
                f'{PREMOUNT_ANCHOR!r} not found in the base mount script. '
                'The base script and the pre-mount hook are out of sync.'
            )
        injection = '# === GCE Repair Pre-Mount Fixes ===\n'
        injection += 'log "=== Running pre-mount repair fixes ==="\n'
        for block in premount_blocks:
            injection += block + '\n'
        injection += 'log "=== Pre-mount repair fixes completed ==="\n\n'
        combined = combined.replace(
            PREMOUNT_ANCHOR, injection + PREMOUNT_ANCHOR, 1
        )

    combined += '\n'
    combined += '\n# === GCE Repair Fix Scripts ===\n'
    combined += 'log "=== Starting repair fixes ==="\n\n'

    # Preserve the previous session's on-disk repair log: this session's fix
    # scripts overwrite gce-repair.log, which would erase the audit trail of
    # earlier repairs on this disk. Runs once per session, before any fix
    # body; harmless when sysroot is unmounted or no earlier log exists.
    combined += (
        "# Preserve the previous session's on-disk repair log: this session's fix\n"
        '# scripts overwrite gce-repair.log, which would erase the audit trail of\n'
        '# earlier repairs on this disk.\n'
        'if mountpoint -q /mnt/sysroot 2>/dev/null'
        ' && [ -f /mnt/sysroot/var/log/gce-repair.log ]; then\n'
        '    { echo ""; echo "=== earlier gce-repair session'
        " (archived $(date '+%Y-%m-%d %H:%M:%S')) ===\"; \\\n"
        '        cat /mnt/sysroot/var/log/gce-repair.log; } \\\n'
        '        >> /mnt/sysroot/var/log/gce-repair-history.log 2>/dev/null || true\n'
        'fi\n\n'
    )

    # Inject REPAIR_TARGETS variable for targeted fstab fixing (diagnosis mode)
    if repair_targets is not None:
        if repair_targets:
            targets_str = '\n'.join(repair_targets)
            combined += '# Repair targets extracted from diagnosis\n'
            combined += f'REPAIR_TARGETS="{targets_str}"\n\n'
        else:
            combined += '# No specific repair targets extracted from diagnosis\n'
            combined += 'REPAIR_TARGETS=""\n\n'

    for fix_body in fix_bodies:
        combined += fix_body + '\n\n'

    combined += 'log "=== Repair fixes completed ==="\n'
    combined += 'signal_complete\n'
    combined += 'log "=== Startup script completed successfully ==="\n'

    return combined


def compose_startup_script_windows(base_script: str,
                                   fix_scripts: List[str]) -> str:
    """Combine the Windows mount script with fix script(s) (PowerShell).

    Unlike the Linux script, the Windows mount script has content AFTER its
    completion marker (RDP credentials, desktop instructions), so instead of
    relocating the marker to the end, the fix script(s) are INSERTED directly
    before the marker line. Verification still only succeeds after the fixes
    have run, and the post-marker content is preserved.

    Args:
        base_script: rescue_mount_windows.ps1 content, placeholders resolved.
        fix_scripts: PowerShell fix script bodies to insert.

    Returns:
        The combined startup script.
    """
    marker_line = f'Write-Log "{RESCUE_COMPLETE_MARKER}"'

    fix_block = '# === GCE Repair Fix Scripts ===\n'
    fix_block += 'Write-Log "=== Starting repair fixes ==="\n\n'
    for fix_script in fix_scripts:
        fix_block += fix_script + '\n\n'
    fix_block += 'Write-Log "=== Repair fixes completed ==="\n\n'

    if marker_line in base_script:
        return base_script.replace(marker_line, fix_block + marker_line, 1)

    # Fallback: marker not found in base script — append fixes + marker so
    # verification still gates on fix completion.
    return base_script + '\n' + fix_block + marker_line + '\n'
