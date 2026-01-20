"""
Tests for checkpoint functionality (resumable operations).
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch, MagicMock

from ..orchestration.checkpoint import (
    CheckpointManager,
    CheckpointData,
    CompletedOperation,
    CHECKPOINT_KEY,
    CHECKPOINT_VERSION
)
from ..orchestration.state import OperationState, StateTracker


class TestCompletedOperation:
    """Tests for CompletedOperation dataclass."""

    def test_to_dict(self):
        """Test serialization to dict."""
        op = CompletedOperation(
            name="Stop VM",
            step=1,
            rollback_data={'vm_name': 'test-vm', 'original_state': 'RUNNING'}
        )
        result = op.to_dict()

        assert result['name'] == "Stop VM"
        assert result['step'] == 1
        assert result['rollback_data']['vm_name'] == 'test-vm'
        assert result['rollback_data']['original_state'] == 'RUNNING'

    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            'name': "Create Snapshot",
            'step': 3,
            'rollback_data': {'snapshot_name': 'pre-rescue-test-vm-123'}
        }
        op = CompletedOperation.from_dict(data)

        assert op.name == "Create Snapshot"
        assert op.step == 3
        assert op.rollback_data['snapshot_name'] == 'pre-rescue-test-vm-123'

    def test_from_dict_no_rollback_data(self):
        """Test deserialization when rollback_data is missing."""
        data = {'name': "Verify Startup", 'step': 9}
        op = CompletedOperation.from_dict(data)

        assert op.name == "Verify Startup"
        assert op.step == 9
        assert op.rollback_data == {}


class TestCheckpointData:
    """Tests for CheckpointData dataclass."""

    def test_to_dict_and_back(self):
        """Test round-trip serialization."""
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='abc123',
            started_at='2024-01-15T10:30:00Z',
            updated_at='2024-01-15T10:31:30Z',
            current_step=3,
            total_steps=9,
            completed_operations=[
                CompletedOperation("Stop VM", 1, {'vm_name': 'test-vm'}),
                CompletedOperation("Detach Boot Disk", 2, {'disk_name': 'test-vm-disk'}),
            ],
            context={'vm_name': 'test-vm', 'zone': 'us-central1-a'}
        )

        # To dict and back
        data = checkpoint.to_dict()
        restored = CheckpointData.from_dict(data)

        assert restored.version == checkpoint.version
        assert restored.operation == checkpoint.operation
        assert restored.session_id == checkpoint.session_id
        assert restored.current_step == checkpoint.current_step
        assert restored.total_steps == checkpoint.total_steps
        assert len(restored.completed_operations) == 2
        assert restored.completed_operations[0].name == "Stop VM"
        assert restored.context['vm_name'] == 'test-vm'

    def test_to_json_and_back(self):
        """Test JSON serialization round-trip."""
        checkpoint = CheckpointData(
            version=1,
            operation='restore',
            session_id='xyz789',
            started_at='2024-01-15T11:00:00Z',
            updated_at='2024-01-15T11:01:00Z',
            current_step=2,
            total_steps=7,
            completed_operations=[],
            context={'rescue_disk_name': 'rescue-disk-123'}
        )

        json_str = checkpoint.to_json()
        restored = CheckpointData.from_json(json_str)

        assert restored.operation == 'restore'
        assert restored.session_id == 'xyz789'
        assert restored.context['rescue_disk_name'] == 'rescue-disk-123'

    def test_get_last_completed_operation(self):
        """Test getting last completed operation name."""
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at='2024-01-15T10:30:00Z',
            updated_at='2024-01-15T10:30:00Z',
            current_step=2,
            total_steps=9,
            completed_operations=[
                CompletedOperation("Stop VM", 1, {}),
                CompletedOperation("Detach Boot Disk", 2, {}),
            ],
            context={}
        )

        assert checkpoint.get_last_completed_operation() == "Detach Boot Disk"

    def test_get_last_completed_operation_empty(self):
        """Test getting last completed operation when list is empty."""
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at='2024-01-15T10:30:00Z',
            updated_at='2024-01-15T10:30:00Z',
            current_step=0,
            total_steps=9,
            completed_operations=[],
            context={}
        )

        assert checkpoint.get_last_completed_operation() is None

    def test_get_next_step_number(self):
        """Test getting next step number."""
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at='2024-01-15T10:30:00Z',
            updated_at='2024-01-15T10:30:00Z',
            current_step=3,
            total_steps=9,
            completed_operations=[],
            context={}
        )

        assert checkpoint.get_next_step_number() == 4

    def test_get_age_display_seconds(self):
        """Test age display for recent checkpoint."""
        now = datetime.now(timezone.utc)
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at=now.isoformat().replace('+00:00', 'Z'),
            updated_at=now.isoformat().replace('+00:00', 'Z'),
            current_step=1,
            total_steps=9,
            completed_operations=[],
            context={}
        )

        # Should be in seconds
        age = checkpoint.get_age_display()
        assert 'second' in age

    def test_get_age_display_minutes(self):
        """Test age display for checkpoint a few minutes old."""
        now = datetime.now(timezone.utc)
        past = now - timedelta(minutes=5)
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at=past.isoformat().replace('+00:00', 'Z'),
            updated_at=past.isoformat().replace('+00:00', 'Z'),
            current_step=1,
            total_steps=9,
            completed_operations=[],
            context={}
        )

        age = checkpoint.get_age_display()
        assert 'minute' in age


class TestCheckpointManager:
    """Tests for CheckpointManager."""

    @pytest.fixture
    def mock_compute(self):
        """Create mock compute client."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': []
            }
        }
        compute.instances.return_value.setMetadata.return_value.execute.return_value = {
            'name': 'operation-123'
        }
        compute.zoneOperations.return_value.get.return_value.execute.return_value = {
            'status': 'DONE'
        }
        return compute

    @pytest.fixture
    def manager(self, mock_compute):
        """Create checkpoint manager with mock compute."""
        return CheckpointManager(
            compute=mock_compute,
            project='test-project',
            zone='us-central1-a',
            vm_name='test-vm'
        )

    def test_create_checkpoint(self, manager, mock_compute):
        """Test creating initial checkpoint."""
        session_id = manager.create_checkpoint(
            operation_type='rescue',
            total_steps=9,
            context={'vm_name': 'test-vm', 'zone': 'us-central1-a'}
        )

        # Should return a session ID
        assert session_id is not None
        assert len(session_id) == 8  # UUID first 8 chars

        # Should have called setMetadata
        mock_compute.instances.return_value.setMetadata.assert_called_once()

    def test_load_checkpoint_exists(self, manager, mock_compute):
        """Test loading existing checkpoint."""
        checkpoint_data = {
            'version': 1,
            'operation': 'rescue',
            'session_id': 'test123',
            'started_at': '2024-01-15T10:30:00Z',
            'updated_at': '2024-01-15T10:31:00Z',
            'current_step': 2,
            'total_steps': 9,
            'completed_operations': [
                {'name': 'Stop VM', 'step': 1, 'rollback_data': {}}
            ],
            'context': {'vm_name': 'test-vm'}
        }

        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': json.dumps(checkpoint_data)}
                ]
            }
        }

        checkpoint = manager.load_checkpoint()

        assert checkpoint is not None
        assert checkpoint.operation == 'rescue'
        assert checkpoint.session_id == 'test123'
        assert checkpoint.current_step == 2
        assert len(checkpoint.completed_operations) == 1

    def test_load_checkpoint_not_exists(self, manager, mock_compute):
        """Test loading checkpoint when none exists."""
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': []
            }
        }

        checkpoint = manager.load_checkpoint()
        assert checkpoint is None

    def test_load_checkpoint_corrupted(self, manager, mock_compute):
        """Test loading corrupted checkpoint data."""
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': 'not valid json {{{'}
                ]
            }
        }

        checkpoint = manager.load_checkpoint()
        assert checkpoint is None

    def test_detect_incomplete_found(self, manager, mock_compute):
        """Test detecting incomplete operation."""
        checkpoint_data = {
            'version': 1,
            'operation': 'rescue',
            'session_id': 'test123',
            'started_at': '2024-01-15T10:30:00Z',
            'updated_at': '2024-01-15T10:31:00Z',
            'current_step': 3,
            'total_steps': 9,
            'completed_operations': [],
            'context': {}
        }

        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': json.dumps(checkpoint_data)}
                ]
            }
        }

        incomplete = manager.detect_incomplete(operation_type='rescue')
        assert incomplete is not None
        assert incomplete.current_step == 3

    def test_detect_incomplete_completed(self, manager, mock_compute):
        """Test that completed operation is not detected as incomplete."""
        checkpoint_data = {
            'version': 1,
            'operation': 'rescue',
            'session_id': 'test123',
            'started_at': '2024-01-15T10:30:00Z',
            'updated_at': '2024-01-15T10:35:00Z',
            'current_step': 9,  # All steps done
            'total_steps': 9,
            'completed_operations': [],
            'context': {}
        }

        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': json.dumps(checkpoint_data)}
                ]
            }
        }

        incomplete = manager.detect_incomplete(operation_type='rescue')
        assert incomplete is None

    def test_detect_incomplete_wrong_operation_type(self, manager, mock_compute):
        """Test that wrong operation type is not detected."""
        checkpoint_data = {
            'version': 1,
            'operation': 'restore',  # Different operation type
            'session_id': 'test123',
            'started_at': '2024-01-15T10:30:00Z',
            'updated_at': '2024-01-15T10:31:00Z',
            'current_step': 3,
            'total_steps': 7,
            'completed_operations': [],
            'context': {}
        }

        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': json.dumps(checkpoint_data)}
                ]
            }
        }

        incomplete = manager.detect_incomplete(operation_type='rescue')
        assert incomplete is None

    def test_is_stale(self, manager):
        """Test stale checkpoint detection."""
        now = datetime.now(timezone.utc)

        # Recent checkpoint (not stale)
        recent = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at=now.isoformat().replace('+00:00', 'Z'),
            updated_at=now.isoformat().replace('+00:00', 'Z'),
            current_step=1,
            total_steps=9,
            completed_operations=[],
            context={}
        )
        assert manager.is_stale(recent) is False

        # Old checkpoint (stale)
        old = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at=(now - timedelta(hours=2)).isoformat().replace('+00:00', 'Z'),
            updated_at=(now - timedelta(hours=2)).isoformat().replace('+00:00', 'Z'),
            current_step=1,
            total_steps=9,
            completed_operations=[],
            context={}
        )
        assert manager.is_stale(old) is True

    def test_clear_checkpoint(self, manager, mock_compute):
        """Test clearing checkpoint."""
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            'metadata': {
                'fingerprint': 'abc123',
                'items': [
                    {'key': CHECKPOINT_KEY, 'value': '{}'},
                    {'key': 'other-key', 'value': 'other-value'}
                ]
            }
        }

        result = manager.clear_checkpoint()
        assert result is True

        # Verify setMetadata was called
        mock_compute.instances.return_value.setMetadata.assert_called_once()

    def test_get_rollback_operations(self, manager):
        """Test getting rollback operations in reverse order."""
        checkpoint = CheckpointData(
            version=1,
            operation='rescue',
            session_id='test',
            started_at='2024-01-15T10:30:00Z',
            updated_at='2024-01-15T10:30:00Z',
            current_step=3,
            total_steps=9,
            completed_operations=[
                CompletedOperation("Stop VM", 1, {'data': 'stop'}),
                CompletedOperation("Detach Boot Disk", 2, {'data': 'detach'}),
                CompletedOperation("Create Snapshot", 3, {'data': 'snapshot'}),
            ],
            context={}
        )

        rollback_ops = manager.get_rollback_operations(checkpoint)

        assert len(rollback_ops) == 3
        assert rollback_ops[0].name == "Create Snapshot"
        assert rollback_ops[1].name == "Detach Boot Disk"
        assert rollback_ops[2].name == "Stop VM"


class TestOperationStateSerialization:
    """Tests for OperationState serialization."""

    def test_to_dict(self):
        """Test OperationState to dict."""
        state = OperationState(
            operation_name="Stop VM",
            success=True,
            message="VM stopped",
            rollback_data={'vm_name': 'test-vm'},
            step_number=1
        )

        data = state.to_dict()

        assert data['operation_name'] == "Stop VM"
        assert data['success'] is True
        assert data['message'] == "VM stopped"
        assert data['rollback_data']['vm_name'] == 'test-vm'
        assert data['step_number'] == 1

    def test_from_dict(self):
        """Test OperationState from dict."""
        data = {
            'operation_name': "Create Snapshot",
            'success': True,
            'message': "Snapshot created",
            'rollback_data': {'snapshot_name': 'snap-123'},
            'timestamp': '2024-01-15T10:30:00',
            'step_number': 3
        }

        state = OperationState.from_dict(data)

        assert state.operation_name == "Create Snapshot"
        assert state.success is True
        assert state.step_number == 3


class TestStateTrackerSerialization:
    """Tests for StateTracker serialization."""

    def test_to_dict_and_back(self):
        """Test StateTracker round-trip serialization."""
        tracker = StateTracker()
        tracker.add_operation("Stop VM", True, "VM stopped", {'vm_name': 'test'}, step_number=1)
        tracker.add_operation("Detach Disk", True, "Disk detached", {'disk': 'test'}, step_number=2)

        # To dict and back
        data = tracker.to_dict()
        restored = StateTracker.from_dict(data)

        assert len(restored.operations) == 2
        assert restored.operations[0].operation_name == "Stop VM"
        assert restored.operations[1].operation_name == "Detach Disk"
        assert restored.operations[0].step_number == 1
        assert restored.operations[1].step_number == 2

    def test_get_current_step(self):
        """Test getting current step number."""
        tracker = StateTracker()
        assert tracker.get_current_step() == 0

        tracker.add_operation("Stop VM", True, "VM stopped")
        assert tracker.get_current_step() == 1

        tracker.add_operation("Detach Disk", True, "Disk detached")
        assert tracker.get_current_step() == 2

    def test_get_last_operation(self):
        """Test getting last operation name."""
        tracker = StateTracker()
        assert tracker.get_last_operation() is None

        tracker.add_operation("Stop VM", True, "VM stopped")
        assert tracker.get_last_operation() == "Stop VM"

        tracker.add_operation("Detach Disk", True, "Disk detached")
        assert tracker.get_last_operation() == "Detach Disk"
