"""Tests for Ctrl+C interrupt recovery and VM state reconciliation."""

import logging
from unittest.mock import Mock, patch, MagicMock

import pytest

from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.orchestration.restore import RestoreOrchestrator
from gce_rescue_v2.orchestration.state import StateTracker
from gce_rescue_v2.orchestration.checkpoint import CheckpointData, CompletedOperation
from gce_rescue_v2.core.config import RescueConfig
from gce_rescue_v2.cli import _reconcile_rescue_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_compute(vm_info=None):
    """Create a minimal fake compute client."""
    compute = Mock()
    if vm_info is None:
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/proj/zones/zone-a/disks/original-boot',
                'deviceName': 'original-boot',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
    compute.instances.return_value.get.return_value.execute.return_value = vm_info
    return compute


def _make_logger():
    logger = logging.getLogger('test_interrupt')
    logger.setLevel(logging.DEBUG)
    logger.console_level = logging.WARNING
    return logger


def _make_checkpoint(step=1, operations=None, context=None):
    """Create a CheckpointData with defaults."""
    if operations is None:
        operations = [
            CompletedOperation(
                name='Stop VM', step=1,
                rollback_data={'vm_name': 'test-vm', 'original_status': 'RUNNING'}
            )
        ]
    if context is None:
        context = {
            'vm_name': 'test-vm',
            'zone': 'zone-a',
            'original_disk_name': 'original-boot',
            'original_device_name': 'original-boot',
            'rescue_disk_name': 'rescue-disk-12345',
        }
    return CheckpointData(
        version=1,
        operation='rescue',
        session_id='abcd1234',
        started_at='2026-01-01T00:00:00Z',
        updated_at='2026-01-01T00:01:00Z',
        current_step=step,
        total_steps=9,
        completed_operations=operations,
        context=context,
    )


# ---------------------------------------------------------------------------
# Layer 1: KeyboardInterrupt triggers in-memory rollback
# ---------------------------------------------------------------------------

class TestKeyboardInterruptRollback:
    """Verify KeyboardInterrupt in execute() triggers rollback and re-raises."""

    def test_rescue_keyboard_interrupt_triggers_rollback(self):
        """Ctrl+C during rescue should rollback completed ops and re-raise."""
        compute = _make_compute()
        logger = _make_logger()
        orch = RescueOrchestrator(
            compute, 'proj', 'zone-a', 'test-vm',
            config=RescueConfig(), logger=logger,
            suppress_progress=True
        )

        # Mock StopVMOperation to succeed, then DetachDiskOperation to raise
        with patch.object(orch, '_get_original_disk_info'), \
             patch.object(orch, 'checkpoint_manager') as mock_cp:
            mock_cp.create_checkpoint.return_value = 'sess-1'
            mock_cp.update_checkpoint.return_value = True
            mock_cp.load_checkpoint.return_value = None

            # Make step 1 succeed, step 2 raise KeyboardInterrupt
            call_count = [0]
            original_execute = None

            def stop_vm_execute(**kwargs):
                from gce_rescue_v2.operations.base import OperationResult
                return OperationResult(
                    operation_name='Stop VM', success=True,
                    message='Stopped',
                    rollback_data={'vm_name': 'test-vm', 'original_status': 'RUNNING'}
                )

            def detach_execute(**kwargs):
                raise KeyboardInterrupt()

            with patch('gce_rescue_v2.orchestration.rescue.StopVMOperation') as MockStop, \
                 patch('gce_rescue_v2.orchestration.rescue.DetachDiskOperation') as MockDetach, \
                 patch('gce_rescue_v2.orchestration.rescue.CreateSnapshotOperation'), \
                 patch('gce_rescue_v2.orchestration.rescue.CreateDiskOperation'), \
                 patch('gce_rescue_v2.orchestration.rescue.AttachDiskOperation'), \
                 patch('gce_rescue_v2.orchestration.rescue.SetMetadataOperation'), \
                 patch('gce_rescue_v2.orchestration.rescue.StartVMOperation'), \
                 patch('gce_rescue_v2.orchestration.rescue.VerifyStartupOperation'):

                MockStop.return_value.execute.side_effect = stop_vm_execute
                MockDetach.return_value.execute.side_effect = detach_execute

                with pytest.raises(KeyboardInterrupt):
                    orch.execute()

                # Verify rollback was called on the stop operation
                assert len(orch.state_tracker.get_successful_operations()) == 1
                assert orch.state_tracker.get_successful_operations()[0].operation_name == 'Stop VM'

    def test_restore_keyboard_interrupt_triggers_rollback(self):
        """Ctrl+C during restore should rollback completed ops and re-raise."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [
                {
                    'boot': True,
                    'source': 'projects/proj/zones/zone-a/disks/rescue-disk-123',
                    'deviceName': 'rescue-disk-123',
                },
                {
                    'boot': False,
                    'source': 'projects/proj/zones/zone-a/disks/original-boot',
                    'deviceName': 'original-boot',
                }
            ],
            'metadata': {
                'items': [
                    {'key': 'rescue-mode', 'value': '123'},
                    {'key': 'rescue-original-disk', 'value': 'original-boot'},
                ],
                'fingerprint': 'abc'
            },
        }
        compute = _make_compute(vm_info)
        logger = _make_logger()
        orch = RestoreOrchestrator(
            compute, 'proj', 'zone-a', 'test-vm',
            logger=logger, suppress_progress=True
        )

        with patch.object(orch, 'checkpoint_manager') as mock_cp:
            mock_cp.create_checkpoint.return_value = 'sess-1'
            mock_cp.update_checkpoint.return_value = True
            mock_cp.load_checkpoint.return_value = None

            def stop_vm_execute(**kwargs):
                from gce_rescue_v2.operations.base import OperationResult
                return OperationResult(
                    operation_name='Stop VM', success=True,
                    message='Stopped',
                    rollback_data={'vm_name': 'test-vm', 'original_status': 'RUNNING'}
                )

            def detach_execute(**kwargs):
                raise KeyboardInterrupt()

            with patch('gce_rescue_v2.orchestration.restore.StopVMOperation') as MockStop, \
                 patch('gce_rescue_v2.orchestration.restore.DetachDiskOperation') as MockDetach, \
                 patch('gce_rescue_v2.orchestration.restore.AttachDiskOperation'), \
                 patch('gce_rescue_v2.orchestration.restore.SetMetadataOperation'), \
                 patch('gce_rescue_v2.orchestration.restore.StartVMOperation'), \
                 patch('gce_rescue_v2.orchestration.restore.DeleteDiskOperation'):

                MockStop.return_value.execute.side_effect = stop_vm_execute
                MockDetach.return_value.execute.side_effect = detach_execute

                with pytest.raises(KeyboardInterrupt):
                    orch.execute()

                assert len(orch.state_tracker.get_successful_operations()) == 1
                assert orch.state_tracker.get_successful_operations()[0].operation_name == 'Stop VM'


# ---------------------------------------------------------------------------
# Layer 2: VM state reconciliation
# ---------------------------------------------------------------------------

class TestReconcileRescueState:
    """Verify _reconcile_rescue_state detects uncheckpointed operations."""

    def test_reconcile_finds_detached_boot_disk(self):
        """Boot disk detached but not checkpointed should be added to rollback."""
        # VM has no boot disk attached (it was detached)
        vm_info = {
            'status': 'TERMINATED',
            'disks': [],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)

        # Checkpoint only has step 1 (Stop VM)
        checkpoint = _make_checkpoint(step=1)

        state_tracker = StateTracker()
        state_tracker.add_operation(
            'Stop VM', True, 'Stopped',
            {'vm_name': 'test-vm', 'original_status': 'RUNNING'}, step_number=1
        )

        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )

        # Should have added "Detach Boot Disk" for rollback
        op_names = [op.operation_name for op in state_tracker.operations]
        assert 'Detach Boot Disk' in op_names

        detach_op = [op for op in state_tracker.operations
                     if op.operation_name == 'Detach Boot Disk'][0]
        assert detach_op.rollback_data['disk_info']['boot'] is True
        assert 'original-boot' in detach_op.rollback_data['disk_info']['source']

    def test_reconcile_noop_when_consistent(self):
        """When checkpoint and VM state match, no extra operations added."""
        # VM still has boot disk attached (consistent with checkpoint at step 1)
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/proj/zones/zone-a/disks/original-boot',
                'deviceName': 'original-boot',
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)
        # Rescue disk should NOT exist in this consistent state
        compute.disks.return_value.get.return_value.execute.side_effect = Exception(
            "notFound: Resource 'rescue-disk-12345' was not found"
        )

        checkpoint = _make_checkpoint(step=1)

        state_tracker = StateTracker()
        state_tracker.add_operation(
            'Stop VM', True, 'Stopped',
            {'vm_name': 'test-vm', 'original_status': 'RUNNING'}, step_number=1
        )

        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )

        # Should NOT have added any extra operations
        op_names = [op.operation_name for op in state_tracker.operations]
        assert op_names == ['Stop VM']

    def test_reconcile_finds_rescue_disk_attached(self):
        """Rescue disk attached but not checkpointed should be added to rollback."""
        # VM has rescue disk attached but no boot disk
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/proj/zones/zone-a/disks/rescue-disk-12345',
                'deviceName': 'rescue-disk-12345',
            }],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)
        # Also make disks().get() work for the rescue disk existence check
        compute.disks.return_value.get.return_value.execute.return_value = {
            'name': 'rescue-disk-12345'
        }

        checkpoint = _make_checkpoint(step=1)

        state_tracker = StateTracker()
        state_tracker.add_operation(
            'Stop VM', True, 'Stopped',
            {'vm_name': 'test-vm', 'original_status': 'RUNNING'}, step_number=1
        )

        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )

        op_names = [op.operation_name for op in state_tracker.operations]
        # Should detect: boot disk detached + rescue disk created + rescue disk attached
        assert 'Detach Boot Disk' in op_names
        assert 'Create Rescue Disk' in op_names
        assert 'Attach Rescue Disk' in op_names

    def test_reconcile_finds_rescue_metadata(self):
        """Rescue metadata set but not checkpointed should be added to rollback."""
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/proj/zones/zone-a/disks/rescue-disk-12345',
                'deviceName': 'rescue-disk-12345',
            }],
            'metadata': {
                'items': [
                    {'key': 'rescue-mode', 'value': '12345'},
                    {'key': 'startup-script', 'value': '#!/bin/bash\necho test'},
                    {'key': 'rescue-original-disk', 'value': 'original-boot'},
                    {'key': 'rescue-backup-startup-script', 'value': 'original script'},
                ],
                'fingerprint': 'xyz'
            },
        }
        compute = _make_compute(vm_info)

        checkpoint = _make_checkpoint(step=1)

        state_tracker = StateTracker()
        state_tracker.add_operation(
            'Stop VM', True, 'Stopped',
            {'vm_name': 'test-vm', 'original_status': 'RUNNING'}, step_number=1
        )

        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )

        op_names = [op.operation_name for op in state_tracker.operations]
        assert 'Set Metadata' in op_names

        meta_op = [op for op in state_tracker.operations
                   if op.operation_name == 'Set Metadata'][0]
        # Verify the reconstructed original_metadata removes rescue keys
        original_items = meta_op.rollback_data['original_metadata']['items']
        original_keys = [item['key'] for item in original_items]
        assert 'rescue-mode' not in original_keys
        assert 'rescue-original-disk' not in original_keys
        # Verify backup key was restored
        assert 'startup-script' in original_keys
        startup_item = [i for i in original_items if i['key'] == 'startup-script'][0]
        assert startup_item['value'] == 'original script'

    def test_reconcile_skips_when_no_original_disk_in_context(self):
        """Reconciliation should skip if checkpoint has no original_disk_name."""
        compute = _make_compute()
        checkpoint = _make_checkpoint(
            step=1,
            context={'vm_name': 'test-vm', 'zone': 'zone-a'}
        )
        state_tracker = StateTracker()

        # Should not raise
        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )
        assert len(state_tracker.operations) == 0

    def test_reconcile_skips_already_checkpointed_operations(self):
        """Operations already in checkpoint should not be duplicated."""
        vm_info = {
            'status': 'TERMINATED',
            'disks': [],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = _make_compute(vm_info)

        # Checkpoint has both Stop VM and Detach Boot Disk already
        checkpoint = _make_checkpoint(
            step=2,
            operations=[
                CompletedOperation(
                    name='Stop VM', step=1,
                    rollback_data={'vm_name': 'test-vm', 'original_status': 'RUNNING'}
                ),
                CompletedOperation(
                    name='Detach Boot Disk', step=2,
                    rollback_data={
                        'vm_name': 'test-vm',
                        'disk_info': {
                            'source': 'projects/proj/zones/zone-a/disks/original-boot',
                            'boot': True, 'autoDelete': True,
                            'deviceName': 'original-boot', 'mode': 'READ_WRITE'
                        }
                    }
                )
            ]
        )

        state_tracker = StateTracker()
        state_tracker.add_operation(
            'Stop VM', True, 'Stopped',
            {'vm_name': 'test-vm', 'original_status': 'RUNNING'}, step_number=1
        )
        state_tracker.add_operation(
            'Detach Boot Disk', True, 'Detached',
            {'vm_name': 'test-vm', 'disk_info': {}}, step_number=2
        )

        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )

        # Should NOT have added another Detach Boot Disk
        detach_ops = [op for op in state_tracker.operations
                      if op.operation_name == 'Detach Boot Disk']
        assert len(detach_ops) == 1

    def test_reconcile_handles_api_error_gracefully(self):
        """If VM API call fails, reconciliation should not raise."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = Exception("API error")

        checkpoint = _make_checkpoint(step=1)
        state_tracker = StateTracker()

        # Should not raise
        _reconcile_rescue_state(
            compute, 'proj', 'zone-a', 'test-vm',
            checkpoint, state_tracker, _make_logger()
        )
        # No operations added due to API failure
        assert len(state_tracker.operations) == 0
