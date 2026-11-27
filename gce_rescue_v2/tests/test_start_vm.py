"""
Unit tests for StartVMOperation.

Tests:
- test_start_vm_success: Normal successful execution
- test_start_vm_already_running: VM is already RUNNING
- test_start_vm_timeout: Operation times out
- test_start_vm_api_error: GCP API returns error
- test_start_vm_rollback: Rollback (stop) works correctly
"""

from uuid import UUID
from unittest.mock import ANY
import pytest
from unittest.mock import Mock, patch
from gce_rescue_v2.operations.start_vm import StartVMOperation

class TestStartVMOperation:

    def setup_method(self):
        """Setup test fixtures."""
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

        self.operation = StartVMOperation(
            self.mock_compute,
            self.project,
            self.zone,
            self.logger
        )

        self.mock_compute.instances().start.return_value.execute.return_value = {'name': 'start-op'}
        self.mock_compute.zoneOperations().get.return_value.execute.return_value = {'status': 'DONE'}

    @pytest.mark.parametrize("statuses, operation_results", [
        (['TERMINATED'], ['error']),
        (['STOPPING', 'TERMINATED'], ['error']),
        (['TERMINATED', 'STOPPING', 'TERMINATED'], ['ok', 'error']),
    ])
    def test_failed_start_reports_operation_error(self, statuses, operation_results, monkeypatch):
        monkeypatch.setattr("time.sleep", lambda _: None)
        self.mock_compute.instances().get.return_value.execute.side_effect = [
            {'status': status} for status in statuses + ['RUNNING']
        ]
        self.mock_compute.zoneOperations().get.return_value.execute.side_effect = [
            {'status': 'DONE'} if outcome == 'ok' else
            {'status': 'DONE', 'error': {'errors': [{'message': 'Insufficient zone capacity'}]}}
            for outcome in operation_results
        ]
        result = self.operation.execute('test-vm', timeout=5)
        assert not result.success
        assert 'Insufficient zone capacity' in result.message
        assert self.mock_compute.instances().start.call_count == len(operation_results)
        assert self.mock_compute.instances().get.return_value.execute.call_count == len(statuses)

    def test_start_vm_success(self):
        """Test successful start VM."""
        # Arrange
        self.mock_compute.instances().get().execute.side_effect = [
            {'status': 'TERMINATED'},  # Initial check
            {'status': 'RUNNING'}      # Check after start
        ]
        
        # Act
        result = self.operation.execute(vm_name='test-vm', timeout=5)

        # Assert
        assert result.success is True
        assert result.rollback_data['original_status'] == 'TERMINATED'
        self.mock_compute.instances().start.assert_called_once()

    def test_start_vm_already_running(self):
        """Test starting an already running VM."""
        # Arrange
        self.mock_compute.instances().get().execute.return_value = {'status': 'RUNNING'}

        # Act
        result = self.operation.execute(vm_name='test-vm')

        # Assert
        assert result.success is True
        assert result.message == "VM already running"
        self.mock_compute.instances().start.assert_not_called()

    def test_start_vm_rollback(self):
        """Test rollback stops the VM."""
        # Arrange
        rollback_data = {'vm_name': 'test-vm', 'original_status': 'TERMINATED'}
        
        with patch.object(self.operation, '_wait_for_status', return_value=True):
            success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.instances().stop.assert_called_with(
            requestId=ANY,
            project=self.project, zone=self.zone, instance='test-vm'
        )

    def test_start_vm_rollback_not_needed(self):
        """Test rollback skips if VM was originally running."""
        # Arrange
        rollback_data = {'vm_name': 'test-vm', 'original_status': 'RUNNING'}

        # Act
        success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.instances().stop.assert_not_called()

    def test_start_vm_retries_when_stopping_then_terminated(self, monkeypatch):
        """Test that StartVMOperation re-issues start() if VM transitions STOPPING -> TERMINATED."""
        import time
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        self.mock_compute.instances().get().execute.side_effect = [
            {'status': 'TERMINATED'},  # Initial check
            {'status': 'STOPPING'},    # Poll 1: lingering stop transition
            {'status': 'TERMINATED'},  # Poll 2: settled in TERMINATED -> triggers retry
            {'status': 'STAGING'},     # Poll 3: starting
            {'status': 'RUNNING'},     # Poll 4: running
        ]

        result = self.operation.execute(vm_name='test-vm', timeout=30)

        assert result.success is True
        calls = self.mock_compute.instances().start.call_args_list
        assert len(calls) == 2
        first_id, second_id = (UUID(call.kwargs["requestId"]) for call in calls)
        assert first_id != second_id

    def test_start_vm_retries_after_consecutive_terminated_polls_up_to_cap(self, monkeypatch):
        """Test consecutive TERMINATED polls trigger at most 2 retries (3 start calls total)."""
        import time
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        self.mock_compute.instances().get().execute.side_effect = [
            {'status': 'TERMINATED'},  # Initial check (start #1)
            {'status': 'TERMINATED'},  # Poll 1
            {'status': 'TERMINATED'},  # Poll 2
            {'status': 'TERMINATED'},  # Poll 3 -> retry 1 (start #2)
            {'status': 'TERMINATED'},  # Poll 4
            {'status': 'TERMINATED'},  # Poll 5
            {'status': 'TERMINATED'},  # Poll 6 -> retry 2 (start #3)
            {'status': 'TERMINATED'},  # Poll 7
            {'status': 'TERMINATED'},  # Poll 8
            {'status': 'TERMINATED'},  # Poll 9 -> cap reached, no start #4
            {'status': 'RUNNING'},     # Poll 10 -> succeeds
        ]

        result = self.operation.execute(vm_name='test-vm', timeout=30)

        assert result.success is True
        assert self.mock_compute.instances().start.call_count == 3
