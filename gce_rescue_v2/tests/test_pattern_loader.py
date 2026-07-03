"""Tests for YAML pattern loader and validator."""

import pytest
import tempfile
from pathlib import Path

from gce_rescue_v2.core.diagnosis import (
    _load_patterns_from_yaml,
    _validate_pattern_file,
    BootErrorPattern,
    BOOT_ERROR_PATTERNS,
)

VALID_YAML = """\
category: test_category

patterns:
  - name: test_pattern
    severity: critical
    description: "Test pattern description"
    regex:
      - 'test regex .*'
"""

MULTI_PATTERN_YAML = """\
category: multi

patterns:
  - name: multi_first
    severity: critical
    description: "First pattern"
    regex:
      - 'first.*match'
  - name: multi_second
    severity: warning
    description: "Second pattern"
    regex:
      - 'second.*match'
      - 'alternate.*second'
"""


def _write_yaml(tmp_dir: Path, filename: str, content: str) -> Path:
    """Write a YAML file to a temp directory."""
    filepath = tmp_dir / filename
    filepath.write_text(content, encoding='utf-8')
    return filepath


class TestLoadPatternsFromYaml:
    """Tests for _load_patterns_from_yaml."""

    def test_valid_yaml_produces_correct_patterns(self, tmp_path):
        _write_yaml(tmp_path, 'test.yaml', VALID_YAML)
        patterns = _load_patterns_from_yaml(tmp_path)

        assert len(patterns) == 1
        assert isinstance(patterns[0], BootErrorPattern)
        assert patterns[0].name == 'test_pattern'
        assert patterns[0].category == 'test_category'
        assert patterns[0].severity == 'critical'
        assert patterns[0].description == 'Test pattern description'
        assert patterns[0].patterns == ['test regex .*']

    def test_multiple_patterns_in_one_file(self, tmp_path):
        _write_yaml(tmp_path, 'multi.yaml', MULTI_PATTERN_YAML)
        patterns = _load_patterns_from_yaml(tmp_path)

        assert len(patterns) == 2
        assert patterns[0].name == 'multi_first'
        assert patterns[0].severity == 'critical'
        assert patterns[1].name == 'multi_second'
        assert patterns[1].severity == 'warning'
        assert len(patterns[1].patterns) == 2

    def test_multiple_yaml_files(self, tmp_path):
        _write_yaml(tmp_path, 'a_first.yaml', VALID_YAML)
        _write_yaml(tmp_path, 'b_second.yaml', MULTI_PATTERN_YAML)
        patterns = _load_patterns_from_yaml(tmp_path)

        assert len(patterns) == 3

    def test_empty_directory_raises_error(self, tmp_path):
        with pytest.raises(ValueError, match="No pattern YAML files found"):
            _load_patterns_from_yaml(tmp_path)

    def test_inline_fixes_loaded(self, tmp_path):
        yaml_with_fixes = """\
category: test_cat
patterns:
  - name: test_fix
    severity: critical
    description: "Test with fixes"
    regex:
      - 'some error'
    fixes:
      - "Fix suggestion 1"
      - "Fix suggestion 2"
"""
        _write_yaml(tmp_path, 'test.yaml', yaml_with_fixes)
        patterns = _load_patterns_from_yaml(tmp_path)
        assert patterns[0].fixes == ["Fix suggestion 1", "Fix suggestion 2"]

    def test_missing_fixes_defaults_to_empty(self, tmp_path):
        _write_yaml(tmp_path, 'test.yaml', VALID_YAML)
        patterns = _load_patterns_from_yaml(tmp_path)
        assert patterns[0].fixes == []


class TestValidatePatternFile:
    """Tests for _validate_pattern_file."""

    def test_missing_category(self):
        data = {'patterns': []}
        with pytest.raises(ValueError, match="missing required field 'category'"):
            _validate_pattern_file(data, 'test.yaml')

    def test_missing_patterns(self):
        data = {'category': 'x'}
        with pytest.raises(ValueError, match="missing required field 'patterns'"):
            _validate_pattern_file(data, 'test.yaml')

    def test_empty_patterns_list(self):
        data = {'category': 'x', 'patterns': []}
        with pytest.raises(ValueError, match="'patterns' must be a non-empty list"):
            _validate_pattern_file(data, 'test.yaml')

    def test_missing_pattern_field_name(self):
        data = {
            'category': 'x',
            'patterns': [{'severity': 'critical', 'description': 'd', 'regex': ['r']}],
        }
        with pytest.raises(ValueError, match="pattern #1 missing required field 'name'"):
            _validate_pattern_file(data, 'test.yaml')

    def test_missing_pattern_field_regex(self):
        data = {
            'category': 'x',
            'patterns': [{'name': 'n', 'severity': 'critical', 'description': 'd'}],
        }
        with pytest.raises(ValueError, match="pattern #1 missing required field 'regex'"):
            _validate_pattern_file(data, 'test.yaml')

    def test_invalid_severity(self):
        data = {
            'category': 'x',
            'patterns': [{'name': 'n', 'severity': 'fatal', 'description': 'd', 'regex': ['r']}],
        }
        with pytest.raises(ValueError, match="invalid severity 'fatal'"):
            _validate_pattern_file(data, 'test.yaml')

    def test_invalid_regex_syntax(self):
        data = {
            'category': 'x',
            'patterns': [{'name': 'n', 'severity': 'critical', 'description': 'd', 'regex': ['[invalid']}],
        }
        with pytest.raises(ValueError, match="invalid regex"):
            _validate_pattern_file(data, 'test.yaml')

    def test_empty_regex_list(self):
        data = {
            'category': 'x',
            'patterns': [{'name': 'n', 'severity': 'critical', 'description': 'd', 'regex': []}],
        }
        with pytest.raises(ValueError, match="must have at least one regex"):
            _validate_pattern_file(data, 'test.yaml')

    def test_all_valid_severities_accepted(self):
        for severity in ['critical', 'error', 'warning']:
            data = {
                'category': 'x',
                'patterns': [{'name': 'n', 'severity': severity, 'description': 'd', 'regex': ['r']}],
            }
            _validate_pattern_file(data, 'test.yaml')  # Should not raise


class TestInvalidYamlLoading:
    """Tests that invalid YAML files produce clear errors during loading."""

    def test_invalid_regex_in_yaml(self, tmp_path):
        bad_yaml = """\
category: bad

patterns:
  - name: bad_regex
    severity: critical
    description: "Bad regex"
    regex:
      - '[invalid'
"""
        _write_yaml(tmp_path, 'bad.yaml', bad_yaml)
        with pytest.raises(ValueError, match="invalid regex"):
            _load_patterns_from_yaml(tmp_path)

    def test_missing_field_in_yaml(self, tmp_path):
        bad_yaml = """\
category: bad

patterns:
  - name: no_severity
    description: "Missing severity"
    regex:
      - 'test'
"""
        _write_yaml(tmp_path, 'bad.yaml', bad_yaml)
        with pytest.raises(ValueError, match="missing required field 'severity'"):
            _load_patterns_from_yaml(tmp_path)

    def test_invalid_severity_in_yaml(self, tmp_path):
        bad_yaml = """\
category: bad

patterns:
  - name: bad_sev
    severity: fatal
    description: "Bad severity"
    regex:
      - 'test'
"""
        _write_yaml(tmp_path, 'bad.yaml', bad_yaml)
        with pytest.raises(ValueError, match="invalid severity 'fatal'"):
            _load_patterns_from_yaml(tmp_path)


class TestShippedPatterns:
    """Integration tests for the actual shipped YAML pattern files."""

    def test_patterns_loaded_successfully(self):
        assert len(BOOT_ERROR_PATTERNS) > 0

    def test_all_patterns_are_boot_error_pattern(self):
        for pattern in BOOT_ERROR_PATTERNS:
            assert isinstance(pattern, BootErrorPattern)

    def test_fstab_patterns_present(self):
        fstab_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'fstab']
        assert len(fstab_patterns) == 7

    def test_all_patterns_have_valid_severity(self):
        for pattern in BOOT_ERROR_PATTERNS:
            assert pattern.severity in {'critical', 'error', 'warning'}

    def test_all_patterns_have_compilable_regex(self):
        import re
        for pattern in BOOT_ERROR_PATTERNS:
            for regex in pattern.patterns:
                re.compile(regex)  # Should not raise

    def test_fstab_patterns_have_inline_fixes(self):
        """All fstab patterns should have inline fixes from merged YAML."""
        fstab_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'fstab']
        for pattern in fstab_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_kernel_patterns_present(self):
        kernel_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'kernel']
        assert len(kernel_patterns) == 5

    def test_kernel_patterns_have_inline_fixes(self):
        """All kernel patterns should have inline fixes from merged YAML."""
        kernel_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'kernel']
        for pattern in kernel_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_initramfs_patterns_present(self):
        initramfs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'initramfs']
        assert len(initramfs_patterns) == 2

    def test_initramfs_patterns_have_inline_fixes(self):
        """All initramfs patterns should have inline fixes from merged YAML."""
        initramfs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'initramfs']
        for pattern in initramfs_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_all_kernel_pattern_names_prefixed(self):
        """Kernel pattern names should follow the category-prefix convention."""
        kernel_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'kernel']
        for pattern in kernel_patterns:
            assert pattern.name.startswith('kernel_'), (
                f"Pattern '{pattern.name}' missing 'kernel_' prefix"
            )

    def test_all_initramfs_pattern_names_prefixed(self):
        """Initramfs pattern names should follow the category-prefix convention."""
        initramfs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'initramfs']
        for pattern in initramfs_patterns:
            assert pattern.name.startswith('initramfs_'), (
                f"Pattern '{pattern.name}' missing 'initramfs_' prefix"
            )

    def test_disk_full_patterns_present(self):
        disk_full_patterns = [
            p for p in BOOT_ERROR_PATTERNS if p.category == 'disk_full'
        ]
        assert len(disk_full_patterns) == 2

    def test_disk_full_patterns_have_category_prefix(self):
        """disk_full pattern names should be prefixed with the category."""
        disk_full_patterns = [
            p for p in BOOT_ERROR_PATTERNS if p.category == 'disk_full'
        ]
        for pattern in disk_full_patterns:
            assert pattern.name.startswith('disk_full_')

    def test_disk_full_patterns_have_inline_fixes(self):
        """All disk_full patterns should have inline fixes from merged YAML."""
        disk_full_patterns = [
            p for p in BOOT_ERROR_PATTERNS if p.category == 'disk_full'
        ]
        for pattern in disk_full_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_cpu_lockup_patterns_present(self):
        cpu_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'cpu_lockup']
        assert len(cpu_patterns) == 4

    def test_cpu_lockup_patterns_have_inline_fixes(self):
        """All cpu_lockup patterns should have inline fixes from merged YAML."""
        cpu_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'cpu_lockup']
        for pattern in cpu_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_ssh_patterns_present(self):
        ssh_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'ssh']
        assert len(ssh_patterns) == 3

    def test_ssh_patterns_have_inline_fixes(self):
        """All ssh patterns should have inline fixes from merged YAML."""
        ssh_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'ssh']
        for pattern in ssh_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_ssh_pattern_names_have_category_prefix(self):
        """All ssh pattern names should carry the ssh_ prefix convention."""
        ssh_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'ssh']
        for pattern in ssh_patterns:
            assert pattern.name.startswith('ssh_'), (
                f"Pattern '{pattern.name}' missing 'ssh_' prefix"
            )

    def test_filesystem_patterns_present(self):
        fs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'filesystem']
        assert len(fs_patterns) == 2

    def test_filesystem_pattern_names_prefixed(self):
        """All filesystem pattern names should carry the category prefix."""
        fs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'filesystem']
        for pattern in fs_patterns:
            assert pattern.name.startswith('filesystem_')

    def test_filesystem_patterns_have_inline_fixes(self):
        """All filesystem patterns should have inline fixes from merged YAML."""
        fs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'filesystem']
        for pattern in fs_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_cpu_lockup_pattern_names_prefixed(self):
        """cpu_lockup pattern names should carry the category prefix."""
        cpu_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'cpu_lockup']
        for pattern in cpu_patterns:
            assert pattern.name.startswith('cpu_lockup_'), (
                f"Pattern '{pattern.name}' missing 'cpu_lockup_' prefix"
            )

    def test_survives_boot_success_flags(self):
        """ssh/filesystem/disk_full failures are not resolved by a completed
        boot (a full disk stays full after 'Startup finished'), so their YAML
        declares survives_boot_success; boot-blocking categories must not
        declare it."""
        from gce_rescue_v2.core.diagnosis import (
            SURVIVES_BOOT_SUCCESS_CATEGORIES,
        )
        assert 'ssh' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'filesystem' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'disk_full' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        for cat in ('fstab', 'kernel', 'initramfs'):
            assert cat not in SURVIVES_BOOT_SUCCESS_CATEGORIES

    def test_detect_only_flags(self):
        """cpu_lockup and kernel are detect-only (manual investigation, never
        a rescue/restore fix); detect_only does NOT imply
        survives_boot_success, and rescue-workflow categories must not
        declare it."""
        from gce_rescue_v2.core.diagnosis import (
            DETECT_ONLY_CATEGORIES,
            SURVIVES_BOOT_SUCCESS_CATEGORIES,
        )
        assert 'cpu_lockup' in DETECT_ONLY_CATEGORIES
        assert 'kernel' in DETECT_ONLY_CATEGORIES
        assert 'cpu_lockup' not in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'kernel' not in SURVIVES_BOOT_SUCCESS_CATEGORIES
        for cat in ('fstab', 'initramfs', 'disk_full', 'ssh', 'filesystem'):
            assert cat not in DETECT_ONLY_CATEGORIES

    def test_detect_only_sets_agree(self):
        """A detect_only category must declare fix_guidance (its prose is what
        the formatter renders), keeping engine and formatter sets identical."""
        from gce_rescue_v2.core import fix_catalog, diagnosis
        assert (fix_catalog.DETECT_ONLY_CATEGORIES
                == set(diagnosis.DETECT_ONLY_CATEGORIES))
