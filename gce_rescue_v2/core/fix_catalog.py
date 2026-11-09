"""Fix metadata catalog for boot error categories.

Loads fix metadata from YAML files in the diagnose_rules/ directory.
Each YAML file contains both detection patterns and fix info in one place.

Exports:
    CATEGORY_FIX_GUIDANCE: Dict mapping category -> manual fix command
    SUPPORTED_FIX_CATEGORIES: Set of categories with auto_repair: true
    DETECT_ONLY_CATEGORIES: Set of categories with detect_only: true
        (runtime conditions — rescue mode does not apply)
    get_fixes_for_pattern(): Look up suggested fixes by category + pattern name
    grub_findings_only_missing_initrd(): Whether the GRUB findings are an
        initramfs problem (repaired by rebuilding the image, not GRUB)
"""

from pathlib import Path
from typing import Any, Dict, List, Set

import logging
import re
import yaml

logger = logging.getLogger(__name__)


def _load_fix_files(
    fixes_dir: Path = None,
) -> Dict[str, dict]:
    """Load fix info from YAML files in the diagnose_rules/ directory.

    Args:
        fixes_dir: Directory containing YAML files with fix metadata.
            Defaults to the diagnose_rules/ directory next to this module.

    Returns:
        Dict mapping category name to parsed fix data.

    Raises:
        ValueError: If a fix file has invalid structure.
    """
    if fixes_dir is None:
        fixes_dir = Path(__file__).parent / 'diagnose_rules'

    if not fixes_dir.exists():
        logger.debug(f"No diagnose_rules directory found at {fixes_dir}")
        return {}

    yaml_files = sorted(fixes_dir.glob('*.yaml'))
    if not yaml_files:
        logger.debug(f"No YAML files found in {fixes_dir}")
        return {}

    result: Dict[str, dict] = {}

    for yaml_file in yaml_files:
        data = yaml.safe_load(yaml_file.read_text(encoding='utf-8'))

        # Skip files without fix metadata (detection-only categories)
        if 'fix_guidance' not in data:
            continue

        _validate_fix_file(data, yaml_file.name)
        category = data['category']
        result[category] = data

    return result


def _validate_fix_file(data: dict, filename: str) -> None:
    """Validate fix metadata in a merged YAML file.

    Args:
        data: Parsed YAML data.
        filename: File name for error messages.

    Raises:
        ValueError: If required fields are missing or invalid.
    """
    if 'category' not in data:
        raise ValueError(f"{filename}: missing required field 'category'")
    if 'fix_guidance' not in data:
        raise ValueError(f"{filename}: missing required field 'fix_guidance'")
    if 'patterns' not in data:
        raise ValueError(f"{filename}: missing required field 'patterns'")
    if not isinstance(data['patterns'], list):
        raise ValueError(f"{filename}: 'patterns' must be a list")


def _build_exports(
    fix_data: Dict[str, dict],
) -> tuple:
    """Build module-level exports from loaded fix data.

    Returns:
        Tuple of (category_fix_guidance, supported_fix_categories,
        detect_only_categories, pattern_fixes_map)
    """
    guidance: Dict[str, str] = {}
    supported: Set[str] = set()
    detect_only: Set[str] = set()
    pattern_fixes: Dict[str, Dict[str, List[str]]] = {}

    for category, data in fix_data.items():
        guidance[category] = data['fix_guidance']

        if data.get('auto_repair', False):
            supported.add(category)

        if data.get('detect_only', False):
            detect_only.add(category)

        pattern_fixes[category] = {}
        for pattern in data.get('patterns', []):
            pattern_name = pattern.get('name', '')
            fixes = pattern.get('fixes', [])
            if pattern_name and fixes:
                pattern_fixes[category][pattern_name] = list(fixes)

    return guidance, supported, detect_only, pattern_fixes


# Load at module level (fail fast if fix files are broken)
_FIX_DATA = _load_fix_files()
(CATEGORY_FIX_GUIDANCE, SUPPORTED_FIX_CATEGORIES, DETECT_ONLY_CATEGORIES,
 _PATTERN_FIXES) = _build_exports(_FIX_DATA)

# initramfs findings that an initramfs rebuild can fix: a missing, corrupt
# or driver-less image. The other initramfs patterns describe symptoms a
# rebuild cannot change (root= names a device that no longer exists, the
# root filesystem itself fails to mount, or only the emergency shell was
# seen without the warning that names the cause), so a diagnosis made ONLY
# of those gets manual guidance instead of an automated repair.
INITRAMFS_REBUILD_PATTERNS = frozenset({
    'initramfs_no_root_fs',
    'initramfs_load_failure',
    'initramfs_dracut_timeout',
    'initramfs_dracut_fatal',
    'initramfs_busybox_shell',
})

# GRUB 'file not found' for an initramfs image (initrd.img-*, initrd-*,
# initramfs-*.img), with or without the /boot prefix. GRUB wraps long lines
# on the serial console, so a line break may follow 'file' and split the file
# name once (see grub.yaml).
GRUB_INITRD_NOT_FOUND_RE = re.compile(
    r"file\s*.?(?:/boot)?/init(?:rd|ramfs)[^/\s]*(?:\n[^/\s]+)? not found")


def grub_findings_only_missing_initrd(boot_errors: List[Dict[str, Any]]) -> bool:
    """Whether every grub finding only reports a missing initramfs image.

    GRUB prints "error: file `/boot/initrd.img-X' not found" before the
    kernel panics without its initramfs. That is a symptom of the initramfs
    problem, not GRUB damage: the initramfs repair rebuilds the image and
    regenerates the GRUB config, so GRUB is not reinstalled.
    """
    grub_errors = [err for err in boot_errors if err.get('category') == 'grub']
    return bool(grub_errors) and all(
        err.get('name') == 'grub_kernel_not_found'
        and GRUB_INITRD_NOT_FOUND_RE.search(err.get('detected_pattern', '') or '')
        for err in grub_errors
    )


SELINUX_RELABEL_PATTERN = 'selinux_relabel_required'

# Shown by diagnose and repair when the only selinux finding is the relabel
# banner: the relabel finishes and reboots on its own, and rescue mode would
# interrupt it.
SELINUX_RELABEL_IN_PROGRESS_MESSAGE = (
    "A relabel is in progress and reboots the VM when done. Wait for it; "
    "run diagnose again if the banner repeats across reboots."
)


def selinux_findings_only_relabel(boot_errors: List[Dict[str, Any]]) -> bool:
    """Whether the selinux findings are only the relabel banner.

    The banner marks a normal relabel that reboots the VM when done, so the
    selinux repair does not act on it: repairing would stop the VM
    mid-relabel. Any other selinux finding (the policy failed to load) is
    repaired.
    """
    selinux = [err for err in boot_errors if err.get('category') == 'selinux']
    return bool(selinux) and all(
        err.get('name') == SELINUX_RELABEL_PATTERN for err in selinux)


def get_fixes_for_pattern(category: str, pattern_name: str) -> List[str]:
    """Look up suggested fixes for a specific pattern.

    Args:
        category: Error category (e.g. 'fstab').
        pattern_name: Pattern name (e.g. 'fstab_uuid_not_found').

    Returns:
        List of fix suggestion strings, empty if not found.
    """
    category_fixes = _PATTERN_FIXES.get(category, {})
    return list(category_fixes.get(pattern_name, []))
