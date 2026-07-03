"""Tests for fix catalog loader (core/fix_catalog.py)."""

import pytest
from pathlib import Path

from gce_rescue_v2.core.fix_catalog import (
    _load_fix_files,
    _validate_fix_file,
    _build_exports,
    CATEGORY_FIX_GUIDANCE,
    SUPPORTED_FIX_CATEGORIES,
    get_fixes_for_pattern,
)


def _write_yaml(tmp_dir: Path, filename: str, content: str) -> Path:
    """Write a YAML file to a temp directory."""
    filepath = tmp_dir / filename
    filepath.write_text(content, encoding='utf-8')
    return filepath


VALID_FIX_YAML = """\
category: test_cat
fix_guidance: "sudo nano /mnt/sysroot/etc/test"
auto_repair: true

patterns:
  - name: test_pattern_a
    severity: critical
    description: "Test A"
    regex:
      - 'test_a'
    fixes:
      - "Fix suggestion A1"
      - "Fix suggestion A2"
  - name: test_pattern_b
    severity: warning
    description: "Test B"
    regex:
      - 'test_b'
    fixes:
      - "Fix suggestion B1"
"""

NO_AUTO_REPAIR_YAML = """\
category: manual_cat
fix_guidance: "Check the docs"
auto_repair: false

patterns:
  - name: manual_pattern
    severity: error
    description: "Manual fix needed"
    regex:
      - 'manual_error'
    fixes:
      - "Manual fix"
"""


class TestLoadFixFiles:
    """Tests for _load_fix_files."""

    def test_loads_valid_yaml(self, tmp_path):
        _write_yaml(tmp_path, 'test.yaml', VALID_FIX_YAML)
        result = _load_fix_files(tmp_path)

        assert 'test_cat' in result
        assert result['test_cat']['fix_guidance'] == "sudo nano /mnt/sysroot/etc/test"
        assert result['test_cat']['auto_repair'] is True

    def test_empty_directory_returns_empty(self, tmp_path):
        result = _load_fix_files(tmp_path)
        assert result == {}

    def test_nonexistent_directory_returns_empty(self, tmp_path):
        result = _load_fix_files(tmp_path / 'nonexistent')
        assert result == {}

    def test_multiple_files(self, tmp_path):
        _write_yaml(tmp_path, 'a.yaml', VALID_FIX_YAML)
        _write_yaml(tmp_path, 'b.yaml', NO_AUTO_REPAIR_YAML)
        result = _load_fix_files(tmp_path)

        assert len(result) == 2
        assert 'test_cat' in result
        assert 'manual_cat' in result

    def test_skips_files_without_fix_guidance(self, tmp_path):
        """YAML files without fix_guidance should be skipped (detection-only)."""
        detection_only = """\
category: detect_only
patterns:
  - name: detect_pattern
    severity: warning
    description: "Detection only"
    regex:
      - 'detect_this'
"""
        _write_yaml(tmp_path, 'a.yaml', VALID_FIX_YAML)
        _write_yaml(tmp_path, 'b.yaml', detection_only)
        result = _load_fix_files(tmp_path)

        assert len(result) == 1
        assert 'test_cat' in result
        assert 'detect_only' not in result


class TestValidateFixFile:
    """Tests for _validate_fix_file."""

    def test_missing_category(self):
        data = {'fix_guidance': 'x', 'patterns': []}
        with pytest.raises(ValueError, match="missing required field 'category'"):
            _validate_fix_file(data, 'test.yaml')

    def test_missing_fix_guidance(self):
        data = {'category': 'x', 'patterns': []}
        with pytest.raises(ValueError, match="missing required field 'fix_guidance'"):
            _validate_fix_file(data, 'test.yaml')

    def test_missing_patterns(self):
        data = {'category': 'x', 'fix_guidance': 'y'}
        with pytest.raises(ValueError, match="missing required field 'patterns'"):
            _validate_fix_file(data, 'test.yaml')

    def test_patterns_must_be_list(self):
        data = {'category': 'x', 'fix_guidance': 'y', 'patterns': 'not a list'}
        with pytest.raises(ValueError, match="'patterns' must be a list"):
            _validate_fix_file(data, 'test.yaml')

    def test_valid_data_passes(self):
        data = {'category': 'x', 'fix_guidance': 'y', 'patterns': [{'name': 'p', 'fixes': ['fix']}]}
        _validate_fix_file(data, 'test.yaml')  # Should not raise


class TestBuildExports:
    """Tests for _build_exports."""

    def test_guidance_extracted(self, tmp_path):
        _write_yaml(tmp_path, 'test.yaml', VALID_FIX_YAML)
        fix_data = _load_fix_files(tmp_path)
        guidance, _, _ = _build_exports(fix_data)

        assert guidance == {'test_cat': "sudo nano /mnt/sysroot/etc/test"}

    def test_auto_repair_discovery(self, tmp_path):
        _write_yaml(tmp_path, 'a.yaml', VALID_FIX_YAML)
        _write_yaml(tmp_path, 'b.yaml', NO_AUTO_REPAIR_YAML)
        fix_data = _load_fix_files(tmp_path)
        _, supported, _ = _build_exports(fix_data)

        assert 'test_cat' in supported
        assert 'manual_cat' not in supported

    def test_pattern_fixes_extracted(self, tmp_path):
        _write_yaml(tmp_path, 'test.yaml', VALID_FIX_YAML)
        fix_data = _load_fix_files(tmp_path)
        _, _, pattern_fixes = _build_exports(fix_data)

        assert 'test_cat' in pattern_fixes
        assert pattern_fixes['test_cat']['test_pattern_a'] == [
            "Fix suggestion A1", "Fix suggestion A2"
        ]
        assert pattern_fixes['test_cat']['test_pattern_b'] == ["Fix suggestion B1"]


class TestGetFixesForPattern:
    """Tests for get_fixes_for_pattern."""

    def test_known_pattern_returns_fixes(self):
        fixes = get_fixes_for_pattern('fstab', 'fstab_uuid_not_found')
        assert len(fixes) > 0
        assert any('UUID' in f or 'Comment out' in f for f in fixes)

    def test_unknown_pattern_returns_empty(self):
        fixes = get_fixes_for_pattern('fstab', 'nonexistent_pattern')
        assert fixes == []

    def test_unknown_category_returns_empty(self):
        fixes = get_fixes_for_pattern('nonexistent_category', 'some_pattern')
        assert fixes == []

    def test_returns_list_copy(self):
        """Returned list should be a copy, not the internal reference."""
        fixes1 = get_fixes_for_pattern('fstab', 'fstab_uuid_not_found')
        fixes2 = get_fixes_for_pattern('fstab', 'fstab_uuid_not_found')
        assert fixes1 == fixes2
        assert fixes1 is not fixes2


class TestShippedFixInfo:
    """Integration tests for the actual shipped fix YAML files."""

    def test_fstab_fix_guidance_loaded(self):
        assert 'fstab' in CATEGORY_FIX_GUIDANCE
        assert 'fstab' in CATEGORY_FIX_GUIDANCE

    def test_fstab_is_auto_repairable(self):
        assert 'fstab' in SUPPORTED_FIX_CATEGORIES

    def test_all_fstab_patterns_have_fixes(self):
        """Every fstab pattern should have at least one fix suggestion."""
        from gce_rescue_v2.core.diagnosis import BOOT_ERROR_PATTERNS

        fstab_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'fstab']
        for pattern in fstab_patterns:
            fixes = get_fixes_for_pattern('fstab', pattern.name)
            assert len(fixes) > 0, (
                f"Pattern '{pattern.name}' has no fixes in core/diagnose_rules/fstab.yaml"
            )

    def test_cpu_lockup_fix_guidance_loaded(self):
        assert 'cpu_lockup' in CATEGORY_FIX_GUIDANCE

    def test_cpu_lockup_is_not_auto_repairable(self):
        """cpu_lockup is detect-only; auto_repair stays false until a
        startup_scripts/fixes/cpu_lockup_fix.sh lands (lockups are workload
        conditions, not on-disk boot configuration)."""
        assert 'cpu_lockup' not in SUPPORTED_FIX_CATEGORIES

    def test_cpu_lockup_patterns_have_fixes(self):
        """Every cpu_lockup pattern should have at least one fix suggestion."""
        from gce_rescue_v2.core.diagnosis import BOOT_ERROR_PATTERNS

        cpu_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'cpu_lockup']
        for pattern in cpu_patterns:
            fixes = get_fixes_for_pattern('cpu_lockup', pattern.name)
            assert len(fixes) > 0, (
                f"Pattern '{pattern.name}' has no fixes in "
                f"core/diagnose_rules/cpu_lockup.yaml"
            )

    def test_fix_script_exists_for_supported_categories(self):
        """Every auto-repairable category should have a fix script."""
        fixes_dir = Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
        for cat in SUPPORTED_FIX_CATEGORIES:
            script_path = fixes_dir / f'{cat}_fix.sh'
            assert script_path.exists(), (
                f"Missing fix script for supported category '{cat}': {script_path}"
            )
