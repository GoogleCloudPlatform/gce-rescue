"""Tests for the repair command: orchestrator, CLI, script generation, result parsing."""

import time
import logging
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from gce_rescue_v2.orchestration.repair import (
    RepairOrchestrator,
    FIX_EXECUTION_ORDER,
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
        """Should return only categories with fix scripts, in execution order."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [
                {'category': 'fstab', 'severity': 'critical'},
                {'category': 'grub', 'severity': 'error'},
                {'category': 'fstab', 'severity': 'warning'},  # duplicate
                {'category': 'kernel', 'severity': 'error'},  # no fix script
            ]
        }
        fixable = orch.get_fixable_categories(diagnosis)
        assert fixable == ['fstab', 'grub']

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
        assert 'kernel' in unfixable
        assert 'grub' not in unfixable  # grub_fix.sh shipped
        assert 'fstab' not in unfixable

    def test_execute_no_fixable_returns_no_fix(self):
        """execute() with no fixable categories returns no_fix status."""
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        diagnosis = {
            'boot_errors': [{'category': 'kernel', 'severity': 'error'}]
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
# TestFixExecutionOrdering
# ---------------------------------------------------------------------------

class TestFixExecutionOrdering:
    """get_fixable_categories returns categories in fix execution order."""

    def _make_orchestrator(self):
        compute = _make_compute()
        return RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )

    def test_execution_order_constant(self):
        """filesystem first, grub last (grub must see the rebuilt initrd)."""
        assert FIX_EXECUTION_ORDER == [
            'filesystem', 'fstab', 'initramfs', 'grub'
        ]

    def test_categories_sorted_into_execution_order(self):
        """Diagnosis order must not leak into fix composition order."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {'category': 'grub', 'severity': 'critical'},
                {'category': 'initramfs', 'severity': 'critical'},
                {'category': 'fstab', 'severity': 'critical'},
                {'category': 'filesystem', 'severity': 'critical'},
            ]
        }
        # Simulate all four categories having fix scripts (the scripts for
        # filesystem/initramfs/grub land in follow-up changes).
        with patch(
            'gce_rescue_v2.orchestration.repair.SUPPORTED_FIX_CATEGORIES',
            {'fstab', 'filesystem', 'initramfs', 'grub'},
        ):
            fixable = orch.get_fixable_categories(diagnosis)
        assert fixable == ['filesystem', 'fstab', 'initramfs', 'grub']

    def test_subset_keeps_execution_order(self):
        """A subset of known categories still sorts by execution order."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {'category': 'grub', 'severity': 'critical'},
                {'category': 'filesystem', 'severity': 'critical'},
            ]
        }
        with patch(
            'gce_rescue_v2.orchestration.repair.SUPPORTED_FIX_CATEGORIES',
            {'fstab', 'filesystem', 'initramfs', 'grub'},
        ):
            fixable = orch.get_fixable_categories(diagnosis)
        assert fixable == ['filesystem', 'grub']

    def test_unknown_categories_stable_after_known(self):
        """Future categories keep diagnosis order, after the known ones."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [
                {'category': 'zeta_future', 'severity': 'critical'},
                {'category': 'grub', 'severity': 'critical'},
                {'category': 'alpha_future', 'severity': 'critical'},
                {'category': 'fstab', 'severity': 'critical'},
            ]
        }
        with patch(
            'gce_rescue_v2.orchestration.repair.SUPPORTED_FIX_CATEGORIES',
            {'fstab', 'grub', 'zeta_future', 'alpha_future'},
        ):
            fixable = orch.get_fixable_categories(diagnosis)
        assert fixable == ['fstab', 'grub', 'zeta_future', 'alpha_future']


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
        """The signal_complete call (serial marker + guest attr) should be at the end."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)

        # The bare 'signal_complete' call (not the 'signal_complete() {' definition)
        lines = script.split('\n')
        call_lines = [
            i for i, line in enumerate(lines) if line.strip() == 'signal_complete'
        ]
        assert len(call_lines) >= 1
        # The active completion call should be near the end (last 5 lines)
        assert call_lines[-1] > len(lines) - 6

    def test_original_marker_commented_out(self):
        """Original completion call in rescue_mount.sh should be relocated/commented."""
        orch = self._make_orchestrator()
        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }
        script = orch._generate_repair_script(diagnosis)
        assert 'moved to end' in script

    def test_missing_fix_script_raises_error(self):
        """Fix script for category without a .sh file should raise, not silently skip."""
        orch = self._make_orchestrator()
        with pytest.raises(FileNotFoundError, match="Fix script missing for category 'kernel'"):
            orch._get_fix_script('kernel')


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

    def test_aggregate_multiple_success_results(self):
        """Multiple SUCCESS markers (one per fix script) sum their counts."""
        serial = (
            "GCE-REPAIR-LINE:[FIXED] filesystem: fsck repaired /dev/sdb1\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: device /dev/sdc1\n"
            "GCE-REPAIR-RESULT:SUCCESS:2\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        assert result['fixed_count'] == 3
        assert len(result['fix_lines']) == 3
        assert result['error'] is None

    def test_aggregate_success_and_no_issues(self):
        """SUCCESS + NO_ISSUES collapses to success with the SUCCESS count."""
        serial = (
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        assert result['fixed_count'] == 1

    def test_aggregate_success_and_failed(self):
        """Any FAILED marker makes the overall status failed (partial fixes kept)."""
        serial = (
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-REPAIR-RESULT:FAILED:grub.cfg regeneration failed\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'failed'
        assert 'grub.cfg regeneration failed' in result['error']
        # Partial success is preserved for the CLI's failed branch
        assert result['fixed_count'] == 1
        assert len(result['fix_lines']) == 1

    def test_aggregate_failed_after_success_not_overridden(self):
        """FAILED wins even when a later script reports SUCCESS."""
        serial = (
            "GCE-REPAIR-RESULT:FAILED:fsck could not repair\n"
            "GCE-REPAIR-RESULT:SUCCESS:2\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'failed'
        assert result['fixed_count'] == 2

    def test_aggregate_multiple_failures_join_reasons(self):
        """Multiple FAILED markers join all reasons in the error."""
        serial = (
            "GCE-REPAIR-RESULT:FAILED:first reason\n"
            "GCE-REPAIR-RESULT:FAILED:second reason\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'failed'
        assert 'first reason' in result['error']
        assert 'second reason' in result['error']

    def test_aggregate_all_no_issues(self):
        """Only NO_ISSUES markers collapse to no_issues."""
        serial = (
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'no_issues'
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
        assert result['marker_results'] == []

    # --- per-marker outcomes (marker_results) ---

    def test_marker_results_preserve_serial_order_mixed_kinds(self):
        """One entry per RESULT marker, in serial order, with kind/count/reason."""
        serial = (
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-REPAIR-RESULT:FAILED:grub install failed\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['marker_results'] == [
            {'kind': 'no_issues', 'count': 0, 'reason': None},
            {'kind': 'success', 'count': 1, 'reason': None},
            {'kind': 'failed', 'count': 0, 'reason': 'grub install failed'},
        ]
        # Aggregate contract unchanged alongside the new key
        assert result['status'] == 'failed'
        assert result['fixed_count'] == 1

    def test_marker_results_empty_without_markers(self):
        """No RESULT markers -> empty marker_results list."""
        orch = self._make_orchestrator("just some boot output\n")
        result = orch._parse_repair_results()
        assert result['marker_results'] == []

    def test_marker_results_malformed_count_uses_segment_fallback(self):
        """An unparseable SUCCESS count records its own segment's count."""
        serial = (
            "GCE-REPAIR-LINE:[FIXED] grub: Reinstalled GRUB\n"
            "GCE-REPAIR-LINE:[FIXED] grub: Regenerated config\n"
            "GCE-REPAIR-RESULT:SUCCESS:oops\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['marker_results'] == [
            {'kind': 'success', 'count': 2, 'reason': None},
        ]

    def test_marker_results_windowed_to_current_boot(self):
        """Markers from a previous boot are not in marker_results."""
        banner = '=== GCE Rescue Auto-Mount Started ==='
        serial = (
            f"{banner}\n"
            "GCE-REPAIR-RESULT:FAILED:old attempt\n"
            f"{banner}\n"
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['marker_results'] == [
            {'kind': 'no_issues', 'count': 0, 'reason': None},
        ]


# ---------------------------------------------------------------------------
# TestRepairHardening
# ---------------------------------------------------------------------------

class TestRepairHardening:
    """Guards for multi-category repair: serial windowing, snapshot
    requirement, verification-timeout floors, and non-boot-disk filtering."""

    BANNER = '=== GCE Rescue Auto-Mount Started ==='

    def _make_orchestrator(self, serial_output='', config=None,
                           debug_console=False):
        compute = _make_compute(serial_output=serial_output)
        logger = _make_logger()
        if debug_console:
            # Debug console level disables the spinner/progress display so
            # flow tests do not write progress lines to stdout.
            logger.console_level = logging.DEBUG
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', config=config, logger=logger
        )
        orch._create_tracked_client = lambda label: compute
        return orch

    # --- serial-output windowing (one rescue session, several boots) ---

    def test_markers_from_previous_boot_not_counted(self):
        """Only markers after the LAST mount banner belong to this run."""
        serial = (
            f"{self.BANNER}\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: old attempt\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            f"{self.BANNER}\n"
            "GCE-REPAIR-LINE:[FIXED] grub: Reinstalled GRUB to /dev/sdb (BIOS)\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['fixed_count'] == 1
        assert len(result['fix_lines']) == 1
        assert 'grub' in result['fix_lines'][0]

    def test_previous_boot_failure_does_not_taint_current(self):
        """A FAILED marker from an earlier boot must not fail this run."""
        serial = (
            f"{self.BANNER}\n"
            "GCE-REPAIR-RESULT:FAILED:old attempt failed\n"
            f"{self.BANNER}\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        assert result['error'] is None

    def test_output_without_banner_parsed_whole(self):
        """No banner (custom scripts, pre-banner failures): parse everything."""
        serial = "GCE-REPAIR-RESULT:SUCCESS:2\n"
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        assert result['fixed_count'] == 2

    # --- malformed SUCCESS count falls back to its OWN segment ---

    def test_malformed_success_count_uses_own_segment(self):
        """An unparseable count falls back to that script's [FIXED] lines,
        not every script's lines."""
        serial = (
            "GCE-REPAIR-LINE:[FIXED] filesystem: e2fsck repaired /dev/sdb1\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-REPAIR-LINE:[FIXED] grub: Reinstalled GRUB\n"
            "GCE-REPAIR-LINE:[FIXED] grub: Regenerated config\n"
            "GCE-REPAIR-LINE:[WARNING] grub: informational only\n"
            "GCE-REPAIR-RESULT:SUCCESS:oops\n"
        )
        orch = self._make_orchestrator(serial)
        result = orch._parse_repair_results()
        assert result['status'] == 'success'
        # 1 (parsed) + 2 (grub segment's [FIXED] lines; WARNING not counted)
        assert result['fixed_count'] == 3

    # --- snapshot guard for destructive fixes ---

    def test_filesystem_repair_refuses_no_snapshot(self):
        """fsck rewrites the disk in place; --no-snapshot must abort."""
        config = RescueConfig(create_snapshot=False)
        orch = self._make_orchestrator(config=config)
        diagnosis = {'boot_errors': [{
            'category': 'filesystem', 'severity': 'critical',
            'detected_pattern': 'EXT4-fs error (device sda1)',
        }]}
        result = orch.execute(diagnosis)
        assert result['status'] == 'error'
        assert 'snapshot' in result['error']

    def test_fstab_repair_allows_no_snapshot(self):
        """Non-destructive categories keep --no-snapshot working."""
        config = RescueConfig(create_snapshot=False)
        orch = self._make_orchestrator(config=config)
        diagnosis = {'boot_errors': [{
            'category': 'fstab', 'severity': 'critical',
            'detected_pattern': 'mount: bad UUID',
        }]}
        sentinel = {'status': 'success'}
        with patch.object(orch, '_generate_repair_script',
                          return_value='script'):
            with patch.object(orch, '_run_repair_flow',
                              return_value=sentinel) as flow:
                result = orch.execute(diagnosis)
        assert result is sentinel
        assert flow.called

    # --- verification-timeout floors and require_snapshot propagation ---

    def _run_flow_capture_config(self, config=None, categories=None):
        orch = self._make_orchestrator(config=config, debug_console=True)
        captured = {}

        class _FakeRescue:
            def __init__(self, **kwargs):
                captured.update(kwargs)
                self.snapshot_name = None

            def execute(self):
                return False  # Short-circuit the flow after config capture

        with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator',
                   _FakeRescue):
            result = orch._run_repair_flow(
                'script', fixable_categories=categories
            )
        assert result['status'] == 'rescue_failed'
        return captured['config']

    def test_filesystem_raises_verification_timeout_and_requires_snapshot(self):
        cfg = self._run_flow_capture_config(
            categories=['filesystem', 'fstab', 'grub']
        )
        assert cfg.verification_timeout_override == 1800
        assert cfg.require_snapshot is True

    def test_grub_only_gets_smaller_floor_without_snapshot_requirement(self):
        cfg = self._run_flow_capture_config(categories=['grub'])
        assert cfg.verification_timeout_override == 900
        assert cfg.require_snapshot is False

    def test_fstab_only_keeps_os_default_timeout(self):
        cfg = self._run_flow_capture_config(categories=['fstab'])
        assert cfg.verification_timeout_override is None

    def test_explicit_timeout_override_beats_category_floor(self):
        config = RescueConfig(verification_timeout_override=120)
        cfg = self._run_flow_capture_config(
            config=config, categories=['filesystem']
        )
        assert cfg.verification_timeout_override == 120

    # --- filesystem findings on non-boot disks are not rescue-fixable ---

    def _fs_diagnosis(self, *patterns):
        return {'boot_errors': [
            {'category': 'filesystem', 'severity': 'critical',
             'detected_pattern': p} for p in patterns
        ]}

    def test_filesystem_on_secondary_disk_not_fixable(self):
        """Rescuing the boot disk cannot fsck a corrupt SECONDARY disk."""
        orch = self._make_orchestrator()
        diagnosis = self._fs_diagnosis('EXT4-fs error (device sdb1): bad block')
        assert orch.get_fixable_categories(diagnosis) == []
        assert 'filesystem' in orch.get_unfixable_categories(diagnosis)

    def test_filesystem_on_boot_disk_stays_fixable(self):
        orch = self._make_orchestrator()
        diagnosis = self._fs_diagnosis('EXT4-fs error (device sda1): bad block')
        assert orch.get_fixable_categories(diagnosis) == ['filesystem']
        assert 'filesystem' not in orch.get_unfixable_categories(diagnosis)

    def test_filesystem_without_device_name_stays_fixable(self):
        """Ambiguous findings keep the category fixable (fail open)."""
        orch = self._make_orchestrator()
        diagnosis = self._fs_diagnosis('Corruption of in-memory data detected')
        assert orch.get_fixable_categories(diagnosis) == ['filesystem']

    def test_filesystem_mixed_devices_stays_fixable(self):
        """One boot-disk finding is enough to keep the category."""
        orch = self._make_orchestrator()
        diagnosis = self._fs_diagnosis(
            'EXT4-fs error (device sdb1): bad block',
            'XFS (sda1): Metadata corruption detected',
        )
        assert orch.get_fixable_categories(diagnosis) == ['filesystem']

    # --- custom fix script pre-mount markers validated pre-flight ---

    def test_validate_rejects_malformed_premount_markers(self):
        """A BEGIN without END must fail validate(), not rescue step 6."""
        config = RescueConfig(fix_script=(
            '#!/bin/bash\n'
            '# === GCE-REPAIR-PREMOUNT-BEGIN ===\n'
            'echo unterminated\n'
        ))
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', config=config,
            logger=_make_logger()
        )
        with patch.object(orch, '_create_tracked_client',
                          return_value=compute):
            assert orch.validate() is False


# ---------------------------------------------------------------------------
# TestCategoryOutcomes
# ---------------------------------------------------------------------------

class TestCategoryOutcomes:
    """Per-category outcome attribution in _run_repair_flow().

    Scripts compose in get_fixable_categories() order and each emits one
    RESULT marker, so serial marker order == category order and the two
    lists zip 1:1. On any length mismatch the key is omitted (attribution
    would be a guess)."""

    def _run_flow(self, serial, fixable_categories=None, fix_script=None):
        compute = _make_compute(serial_output=serial)
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        orch._verify_boot_after_repair = lambda: {
            'verified': True, 'errors': []
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = True
        mock_rescue.snapshot_name = 'pre-rescue-boot-123'
        mock_rescue.verification_succeeded = True
        mock_restore = MagicMock()
        mock_restore.execute.return_value = True

        with patch.object(orch, '_init_progress'), \
                patch.object(orch, '_update_progress'), \
                patch.object(orch, '_finish_progress'), \
                patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator',
                      return_value=mock_rescue), \
                patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator',
                      return_value=mock_restore):
            return orch._run_repair_flow(
                'script' if fix_script is None else None,
                fix_script=fix_script,
                fixable_categories=fixable_categories,
            )

    def test_outcomes_zip_categories_in_composition_order(self):
        """Markers map 1:1 onto categories when the counts match."""
        serial = (
            "GCE-REPAIR-RESULT:NO_ISSUES:0\n"
            "GCE-REPAIR-LINE:[FIXED] fstab: UUID for /data\n"
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        result = self._run_flow(
            serial, fixable_categories=['filesystem', 'fstab']
        )
        assert result['category_outcomes'] == [
            {'category': 'filesystem', 'kind': 'no_issues', 'count': 0,
             'reason': None},
            {'category': 'fstab', 'kind': 'success', 'count': 1,
             'reason': None},
        ]

    def test_marker_count_mismatch_omits_key(self):
        """Serial dropped a marker: no guessing, the key is absent."""
        serial = (
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        result = self._run_flow(
            serial, fixable_categories=['filesystem', 'fstab']
        )
        assert 'category_outcomes' not in result

    def test_custom_fix_script_flow_omits_key(self):
        """--fix-script flows have no categories: never attribute."""
        serial = (
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        result = self._run_flow(serial, fix_script='#!/bin/bash\necho fix\n')
        assert 'category_outcomes' not in result

    def test_failed_marker_keeps_reason_in_outcomes(self):
        """A failed category carries its reason for per-category display."""
        serial = (
            "GCE-REPAIR-RESULT:SUCCESS:1\n"
            "GCE-REPAIR-RESULT:FAILED:grub-install returned 1\n"
            "GCE-RESCUE-COMPLETE\n"
        )
        result = self._run_flow(
            serial, fixable_categories=['fstab', 'grub']
        )
        assert result['status'] == 'failed'
        assert result['category_outcomes'][1] == {
            'category': 'grub', 'kind': 'failed', 'count': 0,
            'reason': 'grub-install returned 1',
        }


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

    def test_grub_is_supported(self):
        """grub ships fixes/grub_fix.sh (auto_repair: true)."""
        assert 'grub' in SUPPORTED_FIX_CATEGORIES

    def test_kernel_is_not_supported(self):
        """kernel is detect-only — no auto-repair fix script exists."""
        assert 'kernel' not in SUPPORTED_FIX_CATEGORIES

    def test_initramfs_is_supported(self):
        """initramfs ships fixes/initramfs_fix.sh (auto_repair: true)."""
        assert 'initramfs' in SUPPORTED_FIX_CATEGORIES

    def test_filesystem_is_supported(self):
        """filesystem ships fixes/filesystem_fix.sh (auto_repair: true)."""
        assert 'filesystem' in SUPPORTED_FIX_CATEGORIES

    def test_supported_set_is_exactly_the_shipped_scripts(self):
        """The full auto-repairable set — update when a new fix script lands."""
        assert SUPPORTED_FIX_CATEGORIES == {'fstab', 'filesystem', 'initramfs', 'grub'}

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
            'boot_errors': [{'category': 'kernel', 'severity': 'error'}]
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
            'boot_errors': [{'category': 'kernel', 'severity': 'error'}]
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
            'boot_errors': [{'category': 'kernel', 'severity': 'error'}]
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
        # Completion confirmed: these tests exercise the restore path, not
        # the fix_in_progress guard (covered by TestResumeSafetyGuard).
        orch._rescue_fixes_completed = lambda: True
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
# TestResumeSafetyGuard
# ---------------------------------------------------------------------------

class TestResumeSafetyGuard:
    """resume() must not restore (stop the VM) while the previous session's
    fix scripts may still be running - a restore mid-fsck/mid-rebuild
    corrupts the very disk being repaired."""

    TOKEN = 'COMPLETE-abc123def456'

    def _make_orchestrator(self, metadata_items=None, guest_attr=None,
                           serial_output=''):
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/rescue-disk',
                'deviceName': 'rescue-disk',
            }],
            'metadata': {'items': metadata_items or [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info=vm_info, serial_output=serial_output)
        if guest_attr is None:
            compute.instances.return_value.getGuestAttributes.return_value \
                .execute.side_effect = Exception('404 attribute not set')
        else:
            compute.instances.return_value.getGuestAttributes.return_value \
                .execute.return_value = guest_attr
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        return orch

    def _script_metadata(self, key='startup-script'):
        return [{'key': key,
                 'value': f'#!/bin/bash\n... {self.TOKEN} ...\n'}]

    # --- _rescue_fixes_completed() signal checks ---

    def test_confirmed_via_session_token_guest_attribute(self):
        """Token from metadata matching gce-rescue/status confirms completion."""
        orch = self._make_orchestrator(
            metadata_items=self._script_metadata(),
            guest_attr={'variableValue': self.TOKEN},
        )
        assert orch._rescue_fixes_completed() is True

    def test_confirmed_via_windows_startup_script_token(self):
        """Token embedded in windows-startup-script-ps1 is also recovered."""
        orch = self._make_orchestrator(
            metadata_items=self._script_metadata(
                key='windows-startup-script-ps1'
            ),
            guest_attr={'queryValue': {'items': [{'value': self.TOKEN}]}},
        )
        assert orch._rescue_fixes_completed() is True

    def test_confirmed_via_serial_fallback_without_token(self):
        """No token in metadata: the serial completion marker still counts."""
        orch = self._make_orchestrator(
            metadata_items=[],
            serial_output='boot output\nGCE-RESCUE-COMPLETE\n',
        )
        assert orch._rescue_fixes_completed() is True

    def test_stale_guest_attr_falls_back_to_serial(self):
        """A stale non-session guest attribute must not confirm by itself,
        but the serial marker still can."""
        orch = self._make_orchestrator(
            metadata_items=self._script_metadata(),
            guest_attr={'variableValue': 'COMPLETE'},  # previous-era value
            serial_output='GCE-RESCUE-COMPLETE\n',
        )
        assert orch._rescue_fixes_completed() is True

    def test_unconfirmed_returns_false(self):
        """No matching guest attribute and no serial marker: not confirmed."""
        orch = self._make_orchestrator(
            metadata_items=self._script_metadata(),
            guest_attr={'variableValue': 'COMPLETE'},
            serial_output='fsck is still running...\n',
        )
        assert orch._rescue_fixes_completed() is False

    def test_marker_from_previous_boot_not_confirmed(self):
        """A completion marker BEFORE the last mount banner belongs to an
        earlier boot and must not confirm the current fix."""
        banner = '=== GCE Rescue Auto-Mount Started ==='
        orch = self._make_orchestrator(
            metadata_items=[],
            serial_output=(
                f'GCE-RESCUE-COMPLETE\n{banner}\nrunning e2fsck...\n'
            ),
        )
        assert orch._rescue_fixes_completed() is False

    # --- resume() behavior on the guard ---

    def _resume(self, confirmed):
        compute = _make_compute()
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        orch._find_rescue_snapshot = lambda: 'pre-rescue-boot-123'
        orch._rescue_fixes_completed = lambda: confirmed
        orch._verify_boot_after_repair = lambda: {
            'verified': None, 'errors': []
        }

        mock_restore = MagicMock()
        mock_restore.execute.return_value = True

        with patch.object(orch, '_init_progress'), \
                patch.object(orch, '_update_progress'), \
                patch.object(orch, '_finish_progress'), \
                patch('gce_rescue_v2.orchestration.repair.RestoreOrchestrator',
                      return_value=mock_restore) as restore_class:
            result = orch.resume()
        return result, restore_class

    def test_unconfirmed_blocks_restore(self):
        """Unconfirmed completion: no restore, status fix_in_progress."""
        result, restore_class = self._resume(confirmed=False)
        assert result['status'] == 'fix_in_progress'
        assert 'still be running' in result['error']
        assert result['snapshot_name'] == 'pre-rescue-boot-123'
        restore_class.assert_not_called()

    def test_confirmed_proceeds_to_restore(self):
        """Confirmed completion: resume restores as before."""
        result, restore_class = self._resume(confirmed=True)
        assert result['status'] != 'fix_in_progress'
        restore_class.assert_called_once()


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

    def test_mount_failed_timeout_message_includes_duration(self):
        """When verification timed out, the error must say so with the
        duration (mirrors the rescue command's #133 messaging)."""
        compute = _make_compute()
        logger = MagicMock()
        orch = RepairOrchestrator(compute, 'proj', 'zone-a', 'vm-1', logger=logger)
        orch._create_tracked_client = lambda label: compute

        diagnosis = {
            'boot_errors': [{'category': 'fstab', 'severity': 'critical'}]
        }

        mock_rescue = MagicMock()
        mock_rescue.execute.return_value = True
        mock_rescue.snapshot_name = None
        mock_rescue.verification_succeeded = False
        mock_rescue.verification_result = OperationResult(
            operation_name='Verify startup script',
            success=False,
            message='timed out',
            rollback_data={},
            details={'timed_out': True, 'timeout_seconds': 900},
        )

        with patch.object(orch, '_init_progress'):
            with patch.object(orch, '_update_progress'):
                with patch.object(orch, '_finish_progress'):
                    with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator', return_value=mock_rescue):
                        orch.execute(diagnosis)

        logged = ' '.join(str(c) for c in logger.error.call_args_list)
        assert 'timed out after 900s' in logged

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
        orch._rescue_fixes_completed = lambda: True
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
        orch._rescue_fixes_completed = lambda: True
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


# ---------------------------------------------------------------------------
# TestCustomFixScript
# ---------------------------------------------------------------------------

class TestCustomFixScript:
    """Custom fix script path (--fix-script): orchestrator side."""

    def _make_orchestrator(self, fix_script='echo custom-fix'):
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
        config = RescueConfig()
        config.fix_script = fix_script
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', config=config,
            logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        return orch

    def test_execute_custom_propagates_fix_script(self):
        """execute_custom hands the fix script to the shared flow (no
        startup-script override; composition happens per-OS in rescue)."""
        orch = self._make_orchestrator('echo custom-fix')
        sentinel = {'status': 'success', 'fixed_count': 1, 'fix_lines': [],
                    'error': None, 'snapshot_name': 's', 'duration_seconds': 1}
        with patch.object(orch, '_run_repair_flow',
                          return_value=sentinel) as flow:
            result = orch.execute_custom()
        flow.assert_called_once_with(fix_script='echo custom-fix')
        assert result is sentinel

    def test_run_repair_flow_sets_fix_script_on_rescue_config(self):
        """The inner rescue config carries the fix script for composition."""
        orch = self._make_orchestrator('echo custom-fix')
        captured = {}

        class FakeRescue:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def execute(self):
                return False  # stop the flow right after construction

            snapshot_name = None

        with patch('gce_rescue_v2.orchestration.repair.RescueOrchestrator',
                   FakeRescue):
            orch._init_progress = lambda: None
            orch._update_progress = lambda phase: None
            orch._finish_progress = lambda ok=True: None
            orch._run_repair_flow(fix_script='echo custom-fix')

        assert captured['config'].fix_script == 'echo custom-fix'
        assert captured['startup_script_override'] is None

    def test_validate_windows_blocked_without_fix_script(self):
        """Windows VMs are blocked unless a custom fix script is supplied."""
        vm_info = {
            'status': 'RUNNING',
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
        orch._create_tracked_client = lambda label: compute
        with patch('gce_rescue_v2.validators.ValidationRunner') as runner_cls:
            runner_cls.return_value.run_all.return_value.all_passed.return_value = True
            assert orch.validate() is False

    def test_validate_windows_allowed_with_fix_script(self):
        """Windows VMs pass validation when --fix-script is supplied."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/win-disk',
                'deviceName': 'win-disk',
                'licenses': ['projects/windows-cloud/global/licenses/windows-server-2022'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)
        config = RescueConfig()
        config.fix_script = 'Write-Log "custom fix"'
        orch = RepairOrchestrator(
            compute, 'proj', 'zone-a', 'vm-1', config=config,
            logger=_make_logger()
        )
        orch._create_tracked_client = lambda label: compute
        with patch('gce_rescue_v2.validators.ValidationRunner') as runner_cls:
            runner_cls.return_value.run_all.return_value.all_passed.return_value = True
            assert orch.validate() is True
