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

    def test_os_scope_loaded_from_yaml(self, tmp_path):
        yaml_with_os = """\
category: win_boot
os: windows
patterns:
  - name: win_pattern
    severity: critical
    description: "Windows-scoped pattern"
    regex:
      - 'Windows failed to start'
"""
        _write_yaml(tmp_path, 'test.yaml', yaml_with_os)
        patterns = _load_patterns_from_yaml(tmp_path)
        assert patterns[0].os == 'windows'

    def test_os_scope_defaults_to_any(self, tmp_path):
        """Absent 'os' key means 'any' — the 21 pre-scoping Linux YAMLs
        load unmodified and keep running against every buffer."""
        _write_yaml(tmp_path, 'test.yaml', VALID_YAML)
        patterns = _load_patterns_from_yaml(tmp_path)
        assert patterns[0].os == 'any'

    def test_os_scope_copied_onto_every_pattern(self, tmp_path):
        """Like survives_boot_success/detect_only, 'os' is a category-level
        flag copied onto each pattern in the file."""
        yaml_multi_os = """\
category: linux_only
os: linux
patterns:
  - name: linux_first
    severity: critical
    description: "First"
    regex:
      - 'first.*match'
  - name: linux_second
    severity: warning
    description: "Second"
    regex:
      - 'second.*match'
"""
        _write_yaml(tmp_path, 'test.yaml', yaml_multi_os)
        patterns = _load_patterns_from_yaml(tmp_path)
        assert [p.os for p in patterns] == ['linux', 'linux']


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

    def test_invalid_os_scope_rejected(self):
        data = {
            'category': 'x',
            'os': 'darwin',
            'patterns': [{'name': 'n', 'severity': 'critical', 'description': 'd', 'regex': ['r']}],
        }
        with pytest.raises(ValueError, match="'os' must be one of"):
            _validate_pattern_file(data, 'test.yaml')

    def test_all_valid_os_scopes_accepted(self):
        for os_scope in ['linux', 'windows', 'any']:
            data = {
                'category': 'x',
                'os': os_scope,
                'patterns': [{'name': 'n', 'severity': 'critical', 'description': 'd', 'regex': ['r']}],
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

    def test_invalid_os_scope_in_yaml(self, tmp_path):
        bad_yaml = """\
category: bad
os: solaris

patterns:
  - name: bad_os
    severity: critical
    description: "Bad os scope"
    regex:
      - 'test'
"""
        _write_yaml(tmp_path, 'bad.yaml', bad_yaml)
        with pytest.raises(ValueError, match="'os' must be one of"):
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
        assert len(kernel_patterns) == 8

    def test_kernel_patterns_have_inline_fixes(self):
        """All kernel patterns should have inline fixes from merged YAML."""
        kernel_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'kernel']
        for pattern in kernel_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_initramfs_patterns_present(self):
        initramfs_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'initramfs']
        assert len(initramfs_patterns) == 8

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
        # 4 = 3 original + ssh_serial_getty_failed (Wave 5: the serial
        # console is the second operator access path next to SSH).
        ssh_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'ssh']
        assert len(ssh_patterns) == 4

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
        assert len(fs_patterns) == 6

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

    def test_grub_patterns_present(self):
        grub_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'grub']
        assert len(grub_patterns) == 12

    def test_grub_pattern_names_prefixed(self):
        """grub pattern names should carry the category prefix."""
        grub_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'grub']
        for pattern in grub_patterns:
            assert pattern.name.startswith('grub_'), (
                f"Pattern '{pattern.name}' missing 'grub_' prefix"
            )

    def test_grub_patterns_have_inline_fixes(self):
        """All grub patterns should have inline fixes from merged YAML."""
        grub_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'grub']
        for pattern in grub_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_firmware_patterns_present(self):
        fw_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'firmware']
        assert len(fw_patterns) == 4

    def test_firmware_pattern_names_prefixed(self):
        """firmware pattern names should carry the category prefix."""
        fw_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'firmware']
        for pattern in fw_patterns:
            assert pattern.name.startswith('firmware_'), (
                f"Pattern '{pattern.name}' missing 'firmware_' prefix"
            )

    def test_firmware_patterns_have_inline_fixes(self):
        """All firmware patterns should have inline fixes from merged YAML."""
        fw_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'firmware']
        for pattern in fw_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_lvm_patterns_present(self):
        lvm_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'lvm']
        assert len(lvm_patterns) == 3

    def test_lvm_pattern_names_prefixed(self):
        """lvm pattern names should carry the category prefix."""
        lvm_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'lvm']
        for pattern in lvm_patterns:
            assert pattern.name.startswith('lvm_'), (
                f"Pattern '{pattern.name}' missing 'lvm_' prefix"
            )

    def test_lvm_patterns_have_inline_fixes(self):
        """All lvm patterns should have inline fixes from merged YAML."""
        lvm_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'lvm']
        for pattern in lvm_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_crypt_patterns_present(self):
        crypt_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'crypt']
        assert len(crypt_patterns) == 3

    def test_crypt_pattern_names_prefixed(self):
        """crypt pattern names should carry the category prefix."""
        crypt_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'crypt']
        for pattern in crypt_patterns:
            assert pattern.name.startswith('crypt_'), (
                f"Pattern '{pattern.name}' missing 'crypt_' prefix"
            )

    def test_crypt_patterns_have_inline_fixes(self):
        """All crypt patterns should have inline fixes from merged YAML."""
        crypt_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'crypt']
        for pattern in crypt_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_raid_patterns_present(self):
        raid_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'raid']
        assert len(raid_patterns) == 2

    def test_raid_pattern_names_prefixed(self):
        """raid pattern names should carry the category prefix."""
        raid_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'raid']
        for pattern in raid_patterns:
            assert pattern.name.startswith('raid_'), (
                f"Pattern '{pattern.name}' missing 'raid_' prefix"
            )

    def test_raid_patterns_have_inline_fixes(self):
        """All raid patterns should have inline fixes from merged YAML."""
        raid_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'raid']
        for pattern in raid_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_machine_id_patterns_present(self):
        mid_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'machine_id']
        assert len(mid_patterns) == 1

    def test_machine_id_pattern_names_prefixed(self):
        """machine_id pattern names should carry the category prefix."""
        mid_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'machine_id']
        for pattern in mid_patterns:
            assert pattern.name.startswith('machine_id_'), (
                f"Pattern '{pattern.name}' missing 'machine_id_' prefix"
            )

    def test_machine_id_patterns_have_inline_fixes(self):
        """All machine_id patterns should have inline fixes."""
        mid_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'machine_id']
        for pattern in mid_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_switchroot_patterns_present(self):
        sr_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'switchroot']
        assert len(sr_patterns) == 3

    def test_switchroot_pattern_names_prefixed(self):
        """switchroot pattern names should carry the category prefix."""
        sr_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'switchroot']
        for pattern in sr_patterns:
            assert pattern.name.startswith('switchroot_'), (
                f"Pattern '{pattern.name}' missing 'switchroot_' prefix"
            )

    def test_switchroot_patterns_have_inline_fixes(self):
        """All switchroot patterns should have inline fixes."""
        sr_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'switchroot']
        for pattern in sr_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_switchroot_patterns_all_critical(self):
        """Stage-6 failures always leave the VM unbootable - all critical."""
        sr_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'switchroot']
        for pattern in sr_patterns:
            assert pattern.severity == 'critical'

    def test_systemd_early_patterns_present(self):
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'systemd_early']
        assert len(se_patterns) == 3

    def test_systemd_early_pattern_names_prefixed(self):
        """systemd_early pattern names should carry the category prefix."""
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'systemd_early']
        for pattern in se_patterns:
            assert pattern.name.startswith('systemd_'), (
                f"Pattern '{pattern.name}' missing 'systemd_' prefix"
            )

    def test_systemd_early_patterns_have_inline_fixes(self):
        """All systemd_early patterns should have inline fixes."""
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'systemd_early']
        for pattern in se_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    # -- Wave 4/5 categories ------------------------------------------------

    def test_readonly_patterns_present(self):
        ro_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'readonly']
        assert len(ro_patterns) == 2

    def test_readonly_pattern_names_prefixed(self):
        """readonly pattern names should carry the category prefix."""
        ro_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'readonly']
        for pattern in ro_patterns:
            assert pattern.name.startswith('readonly_'), (
                f"Pattern '{pattern.name}' missing 'readonly_' prefix"
            )

    def test_readonly_patterns_have_inline_fixes(self):
        """All readonly patterns should have inline fixes."""
        ro_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'readonly']
        for pattern in ro_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_oom_patterns_present(self):
        """One pattern only: 'invoked oom-killer' and 'Out of memory:
        Killed process' are two halves of the same kill event and must
        produce a single finding."""
        oom_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'oom']
        assert len(oom_patterns) == 1

    def test_oom_pattern_is_warning(self):
        """A single OOM kill on a running VM is non-alarmist by design -
        severity warning, so it can never act as a dedupe suppressor
        (_is_boot_root_cause excludes warnings)."""
        oom_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'oom']
        for pattern in oom_patterns:
            assert pattern.severity == 'warning'

    def test_oom_pattern_names_prefixed(self):
        oom_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'oom']
        for pattern in oom_patterns:
            assert pattern.name.startswith('oom_'), (
                f"Pattern '{pattern.name}' missing 'oom_' prefix"
            )

    def test_oom_patterns_have_inline_fixes(self):
        oom_patterns = [p for p in BOOT_ERROR_PATTERNS if p.category == 'oom']
        for pattern in oom_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_selinux_patterns_present(self):
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'selinux']
        assert len(se_patterns) == 2

    def test_selinux_pattern_names_prefixed(self):
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'selinux']
        for pattern in se_patterns:
            assert pattern.name.startswith('selinux_'), (
                f"Pattern '{pattern.name}' missing 'selinux_' prefix"
            )

    def test_selinux_patterns_have_inline_fixes(self):
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'selinux']
        for pattern in se_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_startup_script_patterns_present(self):
        ss_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'startup_script']
        assert len(ss_patterns) == 1

    def test_startup_script_pattern_names_prefixed(self):
        ss_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'startup_script']
        for pattern in ss_patterns:
            assert pattern.name.startswith('startup_script_'), (
                f"Pattern '{pattern.name}' missing 'startup_script_' prefix"
            )

    def test_startup_script_patterns_have_inline_fixes(self):
        ss_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'startup_script']
        for pattern in ss_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_startup_script_regex_excludes_exit_status_zero(self):
        """The success line 'startup-script exit status 0' prints on every
        healthy boot - the exit-status regex must be structurally unable
        to match a zero status."""
        import re
        ss_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'startup_script']
        healthy = ("google_metadata_script_runner[712]: startup-script "
                   "exit status 0")
        for pattern in ss_patterns:
            for regex in pattern.patterns:
                assert not re.search(regex, healthy, re.IGNORECASE), (
                    f"Regex '{regex}' matches the healthy exit-status-0 line"
                )

    def test_cloud_init_patterns_present(self):
        ci_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'cloud_init']
        assert len(ci_patterns) == 2

    def test_cloud_init_pattern_names_prefixed(self):
        ci_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'cloud_init']
        for pattern in ci_patterns:
            assert pattern.name.startswith('cloud_init_'), (
                f"Pattern '{pattern.name}' missing 'cloud_init_' prefix"
            )

    def test_cloud_init_patterns_have_inline_fixes(self):
        ci_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'cloud_init']
        for pattern in ci_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_network_patterns_present(self):
        # 3 since the red-team C2 fix split the benign wait-online
        # timeout out of network_unit_failed into its own warning.
        net_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'network']
        assert len(net_patterns) == 3

    def test_network_pattern_names_prefixed(self):
        net_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'network']
        for pattern in net_patterns:
            assert pattern.name.startswith('network_'), (
                f"Pattern '{pattern.name}' missing 'network_' prefix"
            )

    def test_network_patterns_have_inline_fixes(self):
        net_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'network']
        for pattern in net_patterns:
            assert len(pattern.fixes) > 0, (
                f"Pattern '{pattern.name}' has no inline fixes"
            )

    def test_network_failed_to_start_regexes_are_unit_bound(self):
        """network may use 'Failed to start' ONLY when bound to a specific
        network unit name/description - a bare 'Failed to start .*' would
        fire on every benign unit failure (the systemd_early scoping
        rule)."""
        net_patterns = [p for p in BOOT_ERROR_PATTERNS
                        if p.category == 'network']
        for pattern in net_patterns:
            for regex in pattern.patterns:
                if 'Failed to start' in regex:
                    assert regex != 'Failed to start .*', (
                        "Unbound 'Failed to start' regex in network category"
                    )
                    assert any(tok in regex for tok in (
                        'networkd', 'Network ?Manager', 'networking',
                        'Raise network interfaces', 'Wait for Network',
                    )), (
                        f"'Failed to start' regex not bound to a network "
                        f"unit: {regex}"
                    )

    def test_systemd_early_no_generic_failed_to_start(self):
        """systemd_early must never ship a bare 'Failed to start' regex -
        it fires on every non-fatal unit failure on healthy boots (the
        scoping decision documented in systemd_early.yaml)."""
        se_patterns = [p for p in BOOT_ERROR_PATTERNS
                       if p.category == 'systemd_early']
        for pattern in se_patterns:
            for regex in pattern.patterns:
                assert 'Failed to start' not in regex, (
                    f"Pattern '{pattern.name}' carries the FP-prone "
                    f"'Failed to start' anchor: {regex}"
                )

    def test_survives_boot_success_flags(self):
        """ssh/filesystem/disk_full failures are not resolved by a completed
        boot (a full disk stays full after 'Startup finished'), so their YAML
        declares survives_boot_success; boot-blocking categories must not
        declare it.

        Wave 4/5 additions: readonly (errors=remount-ro is a RUNNING-VM
        condition), oom (runtime kills persist), startup_script/cloud_init
        (the boot-success marker always appears around provisioning
        failures). selinux and network deliberately do NOT survive: a later
        completed boot proves the policy loaded / the transient DHCP flap
        recovered, and a post-boot NIC death sits after the last success
        marker so ordering already protects it from suppression."""
        from gce_rescue_v2.core.diagnosis import (
            SURVIVES_BOOT_SUCCESS_CATEGORIES,
        )
        assert 'ssh' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'filesystem' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'disk_full' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'readonly' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'oom' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'startup_script' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'cloud_init' in SURVIVES_BOOT_SUCCESS_CATEGORIES
        for cat in ('fstab', 'kernel', 'initramfs', 'grub', 'firmware',
                    'lvm', 'crypt', 'raid', 'machine_id', 'switchroot',
                    'systemd_early', 'selinux', 'network'):
            assert cat not in SURVIVES_BOOT_SUCCESS_CATEGORIES

    def test_detect_only_flags(self):
        """cpu_lockup/kernel/crypt are detect-only (manual investigation or,
        for crypt, an encrypted disk that rescue mode cannot unlock);
        detect_only does NOT imply survives_boot_success, and
        rescue-workflow categories must not declare it.

        Wave 4/5 additions: oom (workload sizing, nothing on disk to edit),
        startup_script (user code in VM metadata) and cloud_init (user-data/
        datasource config) are detect-only; readonly, selinux and network
        are NOT - they are fixable from rescue mode (offline fsck,
        autorelabel/selinux=0, on-disk network config)."""
        from gce_rescue_v2.core.diagnosis import (
            DETECT_ONLY_CATEGORIES,
            SURVIVES_BOOT_SUCCESS_CATEGORIES,
        )
        assert 'cpu_lockup' in DETECT_ONLY_CATEGORIES
        assert 'kernel' in DETECT_ONLY_CATEGORIES
        assert 'crypt' in DETECT_ONLY_CATEGORIES
        assert 'oom' in DETECT_ONLY_CATEGORIES
        assert 'startup_script' in DETECT_ONLY_CATEGORIES
        assert 'cloud_init' in DETECT_ONLY_CATEGORIES
        assert 'cpu_lockup' not in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'kernel' not in SURVIVES_BOOT_SUCCESS_CATEGORIES
        assert 'crypt' not in SURVIVES_BOOT_SUCCESS_CATEGORIES
        for cat in ('fstab', 'initramfs', 'disk_full', 'ssh', 'filesystem',
                    'grub', 'firmware', 'lvm', 'raid', 'machine_id',
                    'switchroot', 'systemd_early', 'readonly', 'selinux',
                    'network'):
            assert cat not in DETECT_ONLY_CATEGORIES

    def test_detect_only_sets_agree(self):
        """A detect_only category must declare fix_guidance (its prose is what
        the formatter renders), keeping engine and formatter sets identical."""
        from gce_rescue_v2.core import fix_catalog, diagnosis
        assert (fix_catalog.DETECT_ONLY_CATEGORIES
                == set(diagnosis.DETECT_ONLY_CATEGORIES))
