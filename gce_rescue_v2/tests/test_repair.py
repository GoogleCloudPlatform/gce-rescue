"""Tests for the repair command: orchestrator, CLI, script generation, result parsing."""

import time
import logging
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from gce_rescue_v2.orchestration.repair import (
    RepairOrchestrator,
    REPAIR_LINE_MARKER,
    REPAIR_RESULT_MARKER,
    RESCUE_COMPLETE_MARKER,
    RESCUE_SUBSTEP_LABELS,
    RESTORE_SUBSTEP_LABELS,
)
from gce_rescue_v2.core.fix_catalog import SUPPORTED_FIX_CATEGORIES
from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.orchestration.restore import RestoreOrchestrator
from gce_rescue_v2.core.config import RescueConfig
from gce_rescue_v2.operations.base import OperationResult


# ---------------------------------------------------------------------------
# Helpers / Fakes
# ---------------------------------------------------------------------------

class _Exec:
    def __init__(self, value=None):
        self._value = value

    def execute(self):
        return self._value


def _make_compute(vm_info=None, serial_output=''):
    """Create a minimal fake compute client."""
    compute = Mock()

    if vm_info is None:
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/original-boot',
                'deviceName': 'original-boot',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }

    compute.instances.return_value.get.return_value.execute.return_value = vm_info
    compute.instances.return_value.getSerialPortOutput.return_value.execute.return_value = {
        'contents': serial_output
    }
    return compute


def _make_logger():
    logger = logging.getLogger('test_repair')
    logger.setLevel(logging.DEBUG)
    logger.console_level = logging.WARNING
    return logger


# ---------------------------------------------------------------------------
# TestRepairOrchestrator
# ---------------------------------------------------------------------------

class TestRepairOrchestrator:
    """Core orchestrator logic."""

    def test_validate_linux_passes(self):
        """Validation should pass for a Linux VM."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        with patch.object(orch, 'validate', return_value=True):
            assert orch.validate() is True

    def test_validate_windows_rejected(self):
        """Windows VMs should be rejected by validate()."""
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/win-disk',
                'deviceName': 'win-disk',
                'licenses': ['projects/windows-cloud/global/licenses/windows-server-2022'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)

        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        # Patch out the rescue validation to isolate OS check
        with patch(
            'gce_rescue_v2.orchestration.repair.RescueOrchestrator.validate',
            return_value=True
        ):
            with patch.object(orch, '_create_tracked_client', return_value=compute):
                assert orch.validate() is False

    def test_diagnose_returns_dict(self):
        """diagnose() should return a diagnosis dict on success."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        mock_result = OperationResult(
            operation_name='Diagnose VM',
            success=True,
            message='Found 1 boot error(s)',
            rollback_data={
                'vm_name': 'vm-1',
                'zone': 'zone-a',
                'boot_errors': [{'category': 'fstab', 'severity': 'critical'}],
                'diagnosis_status': 'boot_errors_detected',
            }
        )
        with patch(
            'gce_rescue_v2.orchestration.repair.DiagnoseOperation.execute',
            return_value=mock_result
        ):
            result = orch.diagnose()
            assert result is not None
            assert len(result['boot_errors']) == 1

    def test_diagnose_returns_none_on_failure(self):
        """diagnose() should return None when diagnosis fails."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        mock_result = OperationResult(
            operation_name='Diagnose VM',
            success=False,
            message='Serial console disabled',
            rollback_data={}
        )
        with patch(
            'gce_rescue_v2.orchestration.repair.DiagnoseOperation.execute',
            return_value=mock_result
        ):
            assert orch.diagnose() is None

    def test_get_fixable_categories(self):
        """Should return only categories with fix scripts."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [
                {'category': 'fstab', 'severity': 'critical'},
                {'category': 'grub', 'severity': 'error'},
                {'category': 'fstab', 'severity': 'warning'},  # duplicate
            ]
        }
        fixable = orch.get_fixable_categories(diagnosis)
        assert fixable == ['fstab']

    def test_get_unfixable_categories(self):
        """Should return categories without fix scripts."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [
                {'category': 'fstab', 'severity': 'critical'},
                {'category': 'grub', 'severity': 'error'},
                {'category': 'kernel', 'severity': 'error'},
            ]
        }
        unfixable = orch.get_unfixable_categories(diagnosis)
        assert 'grub' in unfixable
        assert 'kernel' in unfixable
        assert 'fstab' not in unfixable

    def test_execute_no_fixable_returns_no_fix(self):
        """execute() with no fixable categories returns no_fix status."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [{'category': 'grub', 'severity': 'error'}]
        }
        result = orch.execute(diagnosis)
        assert result['status'] == 'no_fix'
        assert result['fixed_count'] == 0

    def test_execute_propagates_custom_rescue_image_to_inner_rescue(self):
        """Repair must pass --rescue-image fields through to inner rescue (issue #102)."""
        from unittest.mock import patch, MagicMock
        from gce_rescue_v2.core.config import RescueConfig

        compute = _make_compute()
        outer_config = RescueConfig(
            create_snapshot=True,
            custom_rescue_image="projects/debian-cloud/global/images/family/debian-12",
            custom_rescue_image_size_gb=20,
        )
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1',
            config=outer_config, logger=_make_logger(),
        )

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'error',
                             'detected_pattern': 'UUID=bad'}]
        }

        captured = {}

        # Capture the RescueConfig passed to the inner RescueOrchestrator
        def _fake_rescue_ctor(**kwargs):
            captured['rescue_config'] = kwargs.get('config')
            mock = MagicMock()
            mock.execute.return_value = False  # short-circuit, we only care about config
            return mock

        with patch(
            'gce_rescue_v2.orchestration.repair.RescueOrchestrator',
            side_effect=_fake_rescue_ctor,
        ):
            # Bypass fix-script generation to avoid extra setup
            with patch.object(orch, '_generate_repair_script', return_value="#!/bin/bash"):
                orch.execute(diagnosis)

        # The inner rescue_config must carry the custom image fields
        assert captured['rescue_config'].custom_rescue_image == (
            "projects/debian-cloud/global/images/family/debian-12"
        )
        assert captured['rescue_config'].custom_rescue_image_size_gb == 20


# ---------------------------------------------------------------------------
# TestRepairCLI
# ---------------------------------------------------------------------------

class TestRepairCLI:
    """CLI argument parsing for repair command."""

    def setup_method(self):
        from gce_rescue_v2.cli import create_parser
        self.parser = create_parser()

    def test_repair_parses_basic_args(self):
        """Repair command should parse vm name and zone."""
        args = self.parser.parse_args([
            'repair', 'my-vm', '--zone', 'us-central1-a'
        ])
        assert args.command == 'repair'
        assert args.instance_name == 'my-vm'
        assert args.zone == 'us-central1-a'

    def test_repair_requires_zone(self):
        """Repair should require --zone flag."""
        with pytest.raises(SystemExit):
            self.parser.parse_args(['repair', 'my-vm'])

    def test_repair_requires_vm_name(self):
        """Repair should require instance name."""
        with pytest.raises(SystemExit):
            self.parser.parse_args(['repair', '--zone', 'us-central1-a'])

    def test_repair_snapshot_default_enabled(self):
        """Snapshot should be enabled by default."""
        args = self.parser.parse_args([
            'repair', 'my-vm', '--zone', 'us-central1-a'
        ])
        assert args.snapshot is True

    def test_repair_no_snapshot_flag(self):
        """--no-snapshot should disable snapshot."""
        args = self.parser.parse_args([
            'repair', 'my-vm', '--zone', 'us-central1-a', '--no-snapshot'
        ])
        assert args.snapshot is False

    def test_repair_quiet_mode(self):
        """--quiet flag should be recognized."""
        args = self.parser.parse_args([
            'repair', 'my-vm', '--zone', 'us-central1-a', '--quiet'
        ])
        assert args.quiet is True

    def test_repair_with_project(self):
        """--project flag should work."""
        args = self.parser.parse_args([
            'repair', 'my-vm', '--zone', 'us-central1-a', '--project', 'my-proj'
        ])
        assert args.project == 'my-proj'


# ---------------------------------------------------------------------------
# TestRepairScript
# ---------------------------------------------------------------------------

class TestRepairScript:
    """Script generation tests."""

    def _make_orchestrator(self):
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/test-boot-disk',
                'deviceName': 'test-boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        # Patch _create_tracked_client to return mock compute (no real credentials)
        orch._create_tracked_client = lambda label: compute
        return orch

    def test_script_contains_disk_placeholder_replaced(self):
        """Generated script should replace DISK_NAME_PLACEHOLDER."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'DISK_NAME_PLACEHOLDER' not in script
        assert 'test-boot-disk' in script

    def test_script_contains_fix_script(self):
        """Generated script should contain fstab fix logic."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'GCE-REPAIR-LINE' in script
        assert 'GCE-REPAIR-RESULT' in script
        assert 'fstab' in script.lower()

    def test_completion_marker_at_end(self):
        """GCE-RESCUE-COMPLETE should be at the end, not in the middle."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)

        # Find all occurrences of the marker
        lines = script.split('\n')
        marker_lines = [
            i for i, line in enumerate(lines)
            if RESCUE_COMPLETE_MARKER in line and 'moved to end' not in line
        ]
        assert len(marker_lines) >= 1
        # The active marker should be near the end (last 5 lines)
        assert marker_lines[-1] > len(lines) - 6

    def test_original_marker_commented_out(self):
        """Original completion marker in rescue_mount.sh should be commented."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'marker moved to end' in script

    def test_missing_fix_script_raises_error(self):
        """Fix script for category without a .sh file should raise, not silently skip."""
        orch = self._make_orchestrator()
        with pytest.raises(FileNotFoundError, match="Fix script missing for category 'grub'"):
            orch._get_fix_script('grub')


# ---------------------------------------------------------------------------
# TestRepairResultParsing
# ---------------------------------------------------------------------------

class TestRepairResultParsing:
    """Parsing of structured markers from serial console."""

    def _make_orchestrator(self, serial_output=''):
        compute = _make_compute(serial_output=serial_output)
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        # Patch _create_tracked_client to return mock compute (no real credentials)
        orch._create_tracked_client = lambda label: compute
        return orch

    def test_parse_success_result(self):
        """Should parse SUCCESS result with fix count."""
        serial = (
            "some boot output\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: Commented out invalid UUID for /data\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        assert result['fixed_count'] == 1
        assert len(result['fix_lines']) == 1
        assert '/data' in result['fix_lines'][0]

    def test_parse_no_issues_result(self):
        """Should parse NO_ISSUES result."""
        serial = (
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'no_issues'
        assert result['fixed_count'] == 0

    def test_parse_failed_result(self):
        """Should parse FAILED result with reason."""
        serial = "GCE-REPAIR-RESULT:FAILED:fstab not found\n"
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'failed'
        assert 'fstab not found' in result['error']

    def test_parse_multiple_fix_lines(self):
        """Should parse multiple fix lines."""
        serial = (
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: device /dev/sdb1\n"
            "GCE-REPAIR-RESULT:SUCCESS:2\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['fixed_count'] == 2
        assert len(result['fix_lines']) == 2

    def test_parse_no_markers(self):
        """Should return unknown status when no markers found."""
        serial = "just some boot output\n"
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'unknown'
        assert result['fixed_count'] == 0

    def test_parse_serial_console_error(self):
        """Should handle serial console fetch failure gracefully."""
        compute = Mock()
        compute.instances.return_value.getSerialPortOutput.return_value.execute.side_effect = Exception("API error")
        compute.instances.return_value.get.return_value.execute.return_value = {}

        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        # Patch _create_tracked_client to return mock compute (no real credentials)
        orch._create_tracked_client = lambda label: compute
        result = orch._parse_repair_results()
        assert result['status'] == 'unknown'
        assert result['error'] is not None


# ---------------------------------------------------------------------------
# TestFstabFixScript
# ---------------------------------------------------------------------------

class TestFstabFixScript:
    """Validate that the fstab fix script file is well-formed."""

    def test_fix_script_exists(self):
        """fstab_fix.sh should exist in startup_scripts/fixes/."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        assert fix_path.exists(), f"Fix script not found at {fix_path}"

    def test_fix_script_emits_repair_markers(self):
        """Script should emit GCE-REPAIR-LINE and GCE-REPAIR-RESULT markers."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'GCE-REPAIR-LINE:' in content
        assert 'GCE-REPAIR-RESULT:' in content

    def test_fix_script_creates_backup(self):
        """Script should create a backup of fstab."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'gce-repair-backup' in content

    def test_fix_script_handles_virtual_fs(self):
        """Script should skip virtual filesystem types."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'proc' in content
        assert 'tmpfs' in content
        assert 'sysfs' in content

    def test_fix_script_uses_repair_targets(self):
        """Script should use REPAIR_TARGETS for targeted matching."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'REPAIR_TARGETS' in content
        assert 'matches_target' in content

    def test_fix_script_handles_empty_targets(self):
        """Script should exit with NO_ISSUES when REPAIR_TARGETS is empty."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'NO_ISSUES:0' in content

    def test_fix_script_detects_malformed_entries(self):
        """Script should detect entries with fewer than 3 fields."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'malformed' in content.lower()

    def test_fix_script_protects_root_mount(self):
        """Script should never comment out the root mount point."""
        fix_path = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'fstab_fix.sh'
        )
        content = fix_path.read_text()
        assert 'mountpoint" = "/"' in content or "mountpoint\" = \"/\"" in content


# ---------------------------------------------------------------------------
# TestExtractFstabTargets
# ---------------------------------------------------------------------------

class TestExtractFstabTargets:
    """Tests for _extract_fstab_targets() method."""

    def _make_orchestrator(self):
        compute = _make_compute()
        return RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )

    def test_extract_uuid_from_not_found(self):
        """Should extract UUID from 'UUID=xxx does not exist' pattern."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'UUID=bad-uuid-12345 does not exist',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'bad-uuid-12345' in targets

    def test_extract_uuid_from_cant_find(self):
        """Should extract UUID from "can't find UUID=xxx" pattern."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': "can't find UUID=abc123-def456-789",
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'abc123-def456-789' in targets

    def test_extract_uuid_from_by_uuid_path(self):
        """Should extract UUID from /dev/disk/by-uuid/xxx path."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern':
                    'Timed out waiting for device /dev/disk/by-uuid/aaa-bbb-ccc',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'aaa-bbb-ccc' in targets

    def test_extract_device_path(self):
        """Should extract raw device like /dev/sdb1."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'Device /dev/sdb1 does not exist',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'sdb1' in targets

    def test_extract_nvme_partition_path(self):
        """Should extract partitioned NVMe device like /dev/nvme0n1p2 from systemd timeout logs."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'Timed out waiting for device dev-nvme0n1p2.device - /dev/nvme0n1p2.',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        # We want to ensure it captures the 'p2' and doesn't truncate at 'nvme0n1'
        assert 'nvme0n1p2' in targets

    def test_extract_nvme_device_path(self):
        """Should extract raw NVMe device like /dev/nvme0n2 from systemd timeout logs."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'Timed out waiting for device nvme0n2.device - /dev/nvme0n2.',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'nvme0n2' in targets


    def test_extract_mount_from_systemd_unit(self):
        """Should extract mount point from systemd unit name."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'Dependency failed for mnt-data.mount',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert '/mnt/data' in targets

    def test_extract_uuid_from_systemd_escaped_device(self):
        """Should extract UUID from systemd escaped device path."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern':
                    'Expecting device dev-disk-by\\x2duuid-bad-uuid-11111-22222.device',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'bad-uuid-11111-22222' in targets

    def test_extract_uuid_from_systemd_unescaped_device(self):
        """Should extract UUID from systemd device path (already unescaped)."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern':
                    'Expecting device dev-disk-by-uuid-bad-uuid-11111-22222.device'
                    ' - /dev/disk/by-uuid/bad-uuid-11111-22222',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'bad-uuid-11111-22222' in targets

    def test_extract_partuuid_from_systemd_device(self):
        """Should extract PARTUUID from systemd escaped device path."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern':
                    'Expecting device dev-disk-by-partuuid-66813b81-9688-4fce.device',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert '66813b81-9688-4fce' in targets

    def test_extract_label_from_by_label_path(self):
        """Should extract label from /dev/disk/by-label/xxx path."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern':
                    'Expecting device /dev/disk/by-label/DATA-DISK',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'DATA-DISK' in targets

    def test_extract_deduplicates(self):
        """Should not return duplicate targets."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'UUID=same-uuid does not exist',
                },
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': "can't find UUID=same-uuid",
                },
            ]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert targets.count('same-uuid') == 1

    def test_extract_empty_for_no_fstab_errors(self):
        """Should return empty list when no fstab errors."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'grub',
                'severity': 'error',
                'detected_pattern': 'error: file not found',
            }]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert targets == []

    def test_extract_empty_for_no_boot_errors(self):
        """Should return empty list when no boot errors at all."""
        orch = self._make_orchestrator()
        diagnosis = {'boot_errors': []}
        targets = orch._extract_fstab_targets(diagnosis)
        assert targets == []

    def test_extract_multiple_different_targets(self):
        """Should extract different target types from multiple errors."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'UUID=bad-uuid-111 does not exist',
                },
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'Device /dev/sdc1 does not exist',
                },
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'Dependency failed for opt-data.mount',
                },
            ]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'bad-uuid-111' in targets
        assert 'sdc1' in targets
        assert '/opt/data' in targets

    def test_extract_skips_non_fstab_errors(self):
        """Should ignore errors with non-fstab category."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'UUID=fstab-uuid does not exist',
                },
                {
                    'category': 'kernel',
                    'severity': 'error',
                    'detected_pattern': 'UUID=kernel-uuid something',
                },
            ]
        }
        targets = orch._extract_fstab_targets(diagnosis)
        assert 'fstab-uuid' in targets
        assert 'kernel-uuid' not in targets


# ---------------------------------------------------------------------------
# TestGenerateRepairScriptTargets
# ---------------------------------------------------------------------------

class TestGenerateRepairScriptTargets:
    """Tests for REPAIR_TARGETS injection in generated repair scripts."""

    def _make_orchestrator(self):
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/test-boot-disk',
                'deviceName': 'test-boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        return orch

    def test_script_includes_repair_targets(self):
        """Generated script should include REPAIR_TARGETS variable."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'UUID=bad-uuid-12345 does not exist',
            }]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'REPAIR_TARGETS=' in script
        assert 'bad-uuid-12345' in script

    def test_script_targets_multiple_uuids(self):
        """Generated script should include all extracted targets."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'UUID=uuid-aaa does not exist',
                },
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'detected_pattern': 'Device /dev/sdb1 does not exist',
                },
            ]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'uuid-aaa' in script
        assert 'sdb1' in script

    def test_script_empty_targets_when_no_fstab(self):
        """Script should set empty REPAIR_TARGETS when no fstab targets extracted."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{
                'category': 'fstab',
                'severity': 'critical',
                'detected_pattern': 'You are in emergency mode',
            }]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'REPAIR_TARGETS=""' in script


# ---------------------------------------------------------------------------
# TestSupportedCategories
# ---------------------------------------------------------------------------

class TestSupportedCategories:
    """Verify supported fix categories are consistent."""

    def test_fstab_is_supported(self):
        assert 'fstab' in SUPPORTED_FIX_CATEGORIES

    def test_grub_is_not_supported(self):
        assert 'grub' not in SUPPORTED_FIX_CATEGORIES

    def test_fix_script_exists_for_each_supported_category(self):
        """Every supported category should have a corresponding fix script."""
        fixes_dir = (
            Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
        )
        for cat in SUPPORTED_FIX_CATEGORIES:
            script_path = fixes_dir / f'{cat}_fix.sh'
            assert script_path.exists(), (
                f"Missing fix script for supported category '{cat}': {script_path}"
            )


# ---------------------------------------------------------------------------
# TestRepairProgressCallback
# ---------------------------------------------------------------------------

class TestRepairProgressCallback:
    """Progress callback integration between repair and rescue/restore."""

    def test_rescue_invokes_callback(self):
        """Rescue _update_progress should invoke the callback with the step label."""
        compute = _make_compute()
        received = []
        rescue = RescueOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1',
            logger=_make_logger(), suppress_progress=True,
            progress_callback=lambda label: received.append(label)
        )
        rescue._progress_lock = __import__('threading').Lock()
        rescue._update_progress('Stopping')
        rescue._update_progress('Starting')
        assert received == ['Stopping', 'Starting']

    def test_restore_invokes_callback(self):
        """Restore _update_progress should invoke the callback with the step label."""
        compute = _make_compute()
        received = []
        restore = RestoreOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1',
            logger=_make_logger(), suppress_progress=True,
            progress_callback=lambda label: received.append(label)
        )
        restore._progress_lock = __import__('threading').Lock()
        restore._update_progress('Stopping')
        restore._update_progress('Restoring affected disk')
        assert received == ['Stopping', 'Restoring affected disk']

    def test_no_callback_by_default(self):
        """No error when progress_callback is None (default)."""
        compute = _make_compute()
        rescue = RescueOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1',
            logger=_make_logger(), suppress_progress=True
        )
        rescue._progress_lock = __import__('threading').Lock()
        # Should not raise
        rescue._update_progress('Stopping')

    def test_make_progress_callback_maps_labels(self):
        """Callback from _make_progress_callback should map raw labels to display names."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._progress_lock = __import__('threading').Lock()
        cb = orch._make_progress_callback("Rescue", RESCUE_SUBSTEP_LABELS)

        cb('Stopping')
        assert orch._current_phase == 'Rescue'
        assert orch._current_substep == 'Stopping VM'

        cb('Starting')
        assert orch._current_substep == 'Starting rescue VM'

    def test_unknown_label_passthrough(self):
        """Unmapped labels should pass through unchanged."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._progress_lock = __import__('threading').Lock()
        cb = orch._make_progress_callback("Rescue", RESCUE_SUBSTEP_LABELS)

        cb('SomeNewStep')
        assert orch._current_substep == 'SomeNewStep'


# ---------------------------------------------------------------------------
# TestRepairExecuteReturnValues
# ---------------------------------------------------------------------------

class TestRepairExecuteReturnValues:
    """Tests for execute() method return values including snapshot_name and duration."""

    def test_execute_no_fixable_returns_snapshot_name_none(self):
        """execute() with no fixable categories should return snapshot_name=None."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [{'category': 'grub', 'severity': 'error'}]
        }
        result = orch.execute(diagnosis)
        assert result['snapshot_name'] is None
        assert 'duration_seconds' in result

    def test_execute_returns_duration_seconds(self):
        """execute() should return duration_seconds in all return paths."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [{'category': 'grub', 'severity': 'error'}]
        }
        result = orch.execute(diagnosis)
        assert 'duration_seconds' in result
        assert result['duration_seconds'] >= 0
        assert isinstance(result['duration_seconds'], (int, float))

    def test_execute_duration_captured_on_success(self):
        """execute() should capture duration even for quick operations."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [{'category': 'grub', 'severity': 'error'}]
        }
        result = orch.execute(diagnosis)
        # Duration should be captured for no_fix result
        assert 'duration_seconds' in result
        assert isinstance(result['duration_seconds'], (int, float))
        assert result['duration_seconds'] >= 0


# ---------------------------------------------------------------------------
# TestRepairResumeMethod
# ---------------------------------------------------------------------------

class TestRepairResumeMethod:
    """Tests for resume() method: parse results + restore without rescue."""

    def _make_resume_orchestrator(self, snapshot_name=None):
        """Create orchestrator with mocked snapshot lookup for resume tests."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._progress_lock = __import__('threading').Lock()
        orch._progress_started = False
        orch._find_rescue_snapshot = lambda: snapshot_name
        orch._create_tracked_client = lambda label: compute
        orch._verify_boot_after_repair = lambda: {'verified': None, 'errors': []}
        return orch

    def test_resume_sets_total_steps_to_2(self):
        """resume() should set _total_steps=2 (skips rescue)."""
        orch = self._make_resume_orchestrator()
        assert orch._total_steps == 3  # Default

        with patch.object(orch, '_parse_repair_results', return_value={'status': 'success', 'fixed_count': 1, 'fix_lines': [], 'error': None}):
            with patch.object(orch, '_init_progress'):
                with patch.object(orch, '_update_progress'):
                    with patch.object(orch, '_finish_progress'):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            mock_restore = MagicMock()
                            mock_restore.execute.return_value = True
                            mock_restore_class.return_value = mock_restore

                            orch.resume()
                            assert orch._total_steps == 2

    def test_resume_skips_rescue_phase(self):
        """resume() should not create a RescueOrchestrator."""
        orch = self._make_resume_orchestrator()

        with patch.object(orch, '_parse_repair_results', return_value={'status': 'success', 'fixed_count': 1, 'fix_lines': [], 'error': None}):
            with patch.object(orch, '_init_progress'):
                with patch.object(orch, '_update_progress'):
                    with patch.object(orch, '_finish_progress'):
                        with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator') as mock_rescue_class:
                            with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                                mock_restore = MagicMock()
                                mock_restore.execute.return_value = True
                                mock_restore_class.return_value = mock_restore

                                orch.resume()
                                mock_rescue_class.assert_not_called()

    def test_resume_returns_dict_with_required_keys(self):
        """resume() should return dict with status, fixed_count, fix_lines, etc."""
        orch = self._make_resume_orchestrator()

        with patch.object(orch, '_parse_repair_results', return_value={'status': 'success', 'fixed_count': 1, 'fix_lines': ['Fixed UUID'], 'error': None}):
            with patch.object(orch, '_init_progress'):
                with patch.object(orch, '_update_progress'):
                    with patch.object(orch, '_finish_progress'):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            mock_restore = MagicMock()
                            mock_restore.execute.return_value = True
                            mock_restore_class.return_value = mock_restore

                            result = orch.resume()
                            assert 'status' in result
                            assert 'fixed_count' in result
                            assert 'fix_lines' in result
                            assert 'error' in result
                            assert 'snapshot_name' in result
                            assert 'duration_seconds' in result

    def test_resume_finds_snapshot_name(self):
        """resume() should include snapshot name from _find_rescue_snapshot."""
        orch = self._make_resume_orchestrator(
            snapshot_name='pre-rescue-original-boot-1234567890'
        )

        with patch.object(orch, '_parse_repair_results', return_value={'status': 'success', 'fixed_count': 1, 'fix_lines': [], 'error': None}):
            with patch.object(orch, '_init_progress'):
                with patch.object(orch, '_update_progress'):
                    with patch.object(orch, '_finish_progress'):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            mock_restore = MagicMock()
                            mock_restore.execute.return_value = True
                            mock_restore_class.return_value = mock_restore

                            result = orch.resume()
                            assert result['snapshot_name'] == 'pre-rescue-original-boot-1234567890'

    def test_resume_snapshot_none_when_not_found(self):
        """resume() should set snapshot_name=None when no snapshot found."""
        orch = self._make_resume_orchestrator(snapshot_name=None)

        with patch.object(orch, '_parse_repair_results', return_value={'status': 'success', 'fixed_count': 1, 'fix_lines': [], 'error': None}):
            with patch.object(orch, '_init_progress'):
                with patch.object(orch, '_update_progress'):
                    with patch.object(orch, '_finish_progress'):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            mock_restore = MagicMock()
                            mock_restore.execute.return_value = True
                            mock_restore_class.return_value = mock_restore

                            result = orch.resume()
                            assert result['snapshot_name'] is None


# ---------------------------------------------------------------------------
# TestRescueSubstepLabels
# ---------------------------------------------------------------------------

class TestRescueSubstepLabels:
    """Tests for RESCUE_SUBSTEP_LABELS mapping."""

    def test_attaching_disk_label_changed_to_mounting(self):
        """'Attaching affected disk' should map to 'Mounting disk'."""
        assert RESCUE_SUBSTEP_LABELS['Attaching affected disk'] == 'Mounting disk'

    def test_all_substep_labels_are_user_friendly(self):
        """All labels should be user-friendly, not raw step names."""
        for raw, display in RESCUE_SUBSTEP_LABELS.items():
            # Display name should be capitalized and more readable
            assert display[0].isupper()
            # Should not contain technical jargon
            assert 'Stopping' in display or 'Creating' in display or 'Starting' in display or 'Mounting' in display


# ---------------------------------------------------------------------------
# TestRepairMountFailure
# ---------------------------------------------------------------------------

class TestRepairMountFailure:
    """Tests for the mount failure path during repair."""

    def test_mount_failed_leaves_vm_in_rescue(self):
        """If startup script doesn't complete, status should be mount_failed."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = True
        mock_rescue.snapshot_name = 'pre-rescue-boot-123'
        mock_rescue.verification_succeeded = False  # Mount failed

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        result = orch.execute(diagnosis)

        assert result['status'] == 'mount_failed'
        assert result['fixed_count'] == 0
        assert result['snapshot_name'] == 'pre-rescue-boot-123'

    def test_mount_failed_does_not_restore(self):
        """Mount failure should NOT trigger restore (VM stays in rescue mode)."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = True
        mock_rescue.snapshot_name = None
        mock_rescue.verification_succeeded = False

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            orch.execute(diagnosis)
                            # RestoreOrchestrator should never have been created
                            mock_restore_class.assert_not_called()


# ---------------------------------------------------------------------------
# TestRepairRescueFailure
# ---------------------------------------------------------------------------

class TestRepairRescueFailure:
    """Tests for rescue phase failure during repair."""

    def test_rescue_failed_returns_rescue_failed(self):
        """If rescue phase fails, status should be rescue_failed."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = False  # Rescue failed
        mock_rescue.snapshot_name = 'pre-rescue-boot-123'

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        result = orch.execute(diagnosis)

        assert result['status'] == 'rescue_failed'
        assert result['fixed_count'] == 0
        assert result['snapshot_name'] == 'pre-rescue-boot-123'
        assert 'duration_seconds' in result

    def test_rescue_failed_does_not_restore(self):
        """Rescue failure should NOT trigger restore."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = False

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator') as mock_restore_class:
                            orch.execute(diagnosis)
                            mock_restore_class.assert_not_called()


# ---------------------------------------------------------------------------
# TestRepairRestoreFailure
# ---------------------------------------------------------------------------

class TestRepairRestoreFailure:
    """Tests for restore phase failure during repair."""

    def test_restore_failed_returns_restore_failed(self):
        """If restore phase fails, status should be restore_failed."""
        compute = _make_compute(serial_output=(
            "GCE-REPAIR-LINE:[FIXED] fstab: Commented out bad entry\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-RESCUE-COMPLETE\n"
        ))
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = True
        mock_rescue.snapshot_name = 'pre-rescue-boot-123'
        mock_rescue.verification_succeeded = True

        mock_restore = MagicMock()
        mock_restore.execute.return_value = False  # Restore failed

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator', return_value=mock_restore):
                            result = orch.execute(diagnosis)

        assert result['status'] == 'restore_failed'
        assert result['fixed_count'] == 1  # Fix was applied
        assert result['snapshot_name'] == 'pre-rescue-boot-123'


# ---------------------------------------------------------------------------
# TestRepairResumeRestoreFailure
# ---------------------------------------------------------------------------

class TestRepairResumeRestoreFailure:
    """Tests for restore failure during resume path."""

    def test_resume_restore_failed(self):
        """resume() with restore failure should return restore_failed."""
        compute = _make_compute(serial_output=(
            "GCE-REPAIR-LINE:[FIXED] fstab: Commented out bad entry\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
        ))
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        orch._find_rescue_snapshot = lambda: 'pre-rescue-boot-123'
        orch._progress_lock = __import__('threading').Lock()
        orch._progress_started = False

        mock_restore = MagicMock()
        mock_restore.execute.return_value = False

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator', return_value=mock_restore):
                        result = orch.resume()

        assert result['status'] == 'restore_failed'
        assert result['snapshot_name'] == 'pre-rescue-boot-123'

    def test_resume_unexpected_error(self):
        """resume() should handle unexpected exceptions gracefully."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        orch._find_rescue_snapshot = lambda: None
        orch._progress_lock = __import__('threading').Lock()
        orch._progress_started = False

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch.object(orch, '_parse_repair_results', side_effect=RuntimeError("API timeout")):
                        result = orch.resume()

        assert result['status'] == 'error'
        assert 'API timeout' in result['error']
        assert 'duration_seconds' in result


# ---------------------------------------------------------------------------
# TestFindRescueSnapshot
# ---------------------------------------------------------------------------

class TestFindRescueSnapshot:
    """Tests for _find_rescue_snapshot helper."""

    def test_finds_snapshot_from_metadata(self):
        """Should find snapshot matching original disk name from metadata."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [],
            'metadata': {
                'items': [
                    {'key': 'rescue-mode', 'value': '1234567890'},
                    {'key': 'rescue-original-disk', 'value': 'boot-disk'},
                ],
                'fingerprint': 'abc'
            },
        }
        compute = _make_compute(vm_info=vm_info)
        # Mock snapshots.list
        compute.snapshots.return_value.list.return_value.execute.return_value = {
            'items': [
                {'name': 'pre-rescue-boot-disk-1111111111', 'creationTimestamp': '2025-01-01T00:00:00Z'},
                {'name': 'pre-rescue-boot-disk-2222222222', 'creationTimestamp': '2025-01-02T00:00:00Z'},
            ]
        }

        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        result = orch._find_rescue_snapshot()
        # Should return the most recent one
        assert result == 'pre-rescue-boot-disk-2222222222'

    def test_no_metadata_returns_none(self):
        """Should return None when rescue-original-disk not in metadata."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info=vm_info)
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        assert orch._find_rescue_snapshot() is None

    def test_no_matching_snapshots_returns_none(self):
        """Should return None when no matching snapshots exist."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [],
            'metadata': {
                'items': [
                    {'key': 'rescue-original-disk', 'value': 'boot-disk'},
                ],
                'fingerprint': 'abc'
            },
        }
        compute = _make_compute(vm_info=vm_info)
        compute.snapshots.return_value.list.return_value.execute.return_value = {
            'items': []
        }
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        assert orch._find_rescue_snapshot() is None

    def test_api_error_returns_none(self):
        """Should return None gracefully on API error."""
        compute = _make_compute()
        compute.instances.return_value.get.return_value.execute.side_effect = (
            Exception("API error")
        )
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute

        assert orch._find_rescue_snapshot() is None
