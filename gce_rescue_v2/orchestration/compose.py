"""
GCE Rescue - Startup script composition.

Combines the base mount script with fix script(s) into a single startup
script, relocating the completion marker so verification (and any restore
that follows) only proceeds after all fixes have finished.

Shared by the repair orchestrator (diagnosis-driven fixes) and the rescue
orchestrator (custom fix scripts via --fix-script).
"""

from typing import List, Optional

# Completion marker emitted by the base mount script to the serial console
RESCUE_COMPLETE_MARKER = 'GCE-RESCUE-COMPLETE'


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

    Args:
        base_script: The mount script, with disk placeholder already resolved.
        fix_scripts: Fix script bodies to append (shebangs already stripped).
        repair_targets: Identifiers extracted from diagnosis for targeted
            fixing. Pass a list (possibly empty) to inject the REPAIR_TARGETS
            variable for diagnosis-driven fixes; pass None for self-contained
            (custom) fix scripts that need no targets.

    Returns:
        The combined startup script.
    """
    # Remove the completion marker line (re-added at the very end)
    combined = base_script.replace(
        f'echo "{RESCUE_COMPLETE_MARKER}" >&2',
        f'# GCE-RESCUE-COMPLETE marker moved to end (repair mode)'
    )

    combined += '\n'
    combined += '\n# === GCE Repair Fix Scripts ===\n'
    combined += 'log "=== Starting repair fixes ==="\n\n'

    # Inject REPAIR_TARGETS variable for targeted fstab fixing (diagnosis mode)
    if repair_targets is not None:
        if repair_targets:
            targets_str = '\n'.join(repair_targets)
            combined += '# Repair targets extracted from diagnosis\n'
            combined += f'REPAIR_TARGETS="{targets_str}"\n\n'
        else:
            combined += '# No specific repair targets extracted from diagnosis\n'
            combined += 'REPAIR_TARGETS=""\n\n'

    for fix_script in fix_scripts:
        combined += fix_script + '\n\n'

    combined += 'log "=== Repair fixes completed ==="\n'
    combined += f'echo "{RESCUE_COMPLETE_MARKER}" >&2\n'
    combined += 'log "=== Startup script completed successfully ==="\n'

    return combined
