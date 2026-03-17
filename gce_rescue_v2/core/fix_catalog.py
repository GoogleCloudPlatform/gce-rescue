"""Fix metadata catalog for boot error categories.

Loads fix metadata from YAML files in the diagnose_rules/ directory.
Each YAML file contains both detection patterns and fix info in one place.

Exports:
    CATEGORY_FIX_GUIDANCE: Dict mapping category -> manual fix command
    SUPPORTED_FIX_CATEGORIES: Set of categories with auto_repair: true
    get_fixes_for_pattern(): Look up suggested fixes by category + pattern name
"""

from pathlib import Path
from typing import Dict, List, Set

import logging
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
        Tuple of (category_fix_guidance, supported_fix_categories, pattern_fixes_map)
    """
    guidance: Dict[str, str] = {}
    supported: Set[str] = set()
    pattern_fixes: Dict[str, Dict[str, List[str]]] = {}

    for category, data in fix_data.items():
        guidance[category] = data['fix_guidance']

        if data.get('auto_repair', False):
            supported.add(category)

        pattern_fixes[category] = {}
        for pattern in data.get('patterns', []):
            pattern_name = pattern.get('name', '')
            fixes = pattern.get('fixes', [])
            if pattern_name and fixes:
                pattern_fixes[category][pattern_name] = list(fixes)

    return guidance, supported, pattern_fixes


# Load at module level (fail fast if fix files are broken)
_FIX_DATA = _load_fix_files()
CATEGORY_FIX_GUIDANCE, SUPPORTED_FIX_CATEGORIES, _PATTERN_FIXES = _build_exports(_FIX_DATA)


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
