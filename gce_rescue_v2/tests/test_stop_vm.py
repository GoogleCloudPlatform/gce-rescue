"""
Unit tests for StopVMOperation.

Tests:
- test_stop_vm_success: Normal successful execution
- test_stop_vm_already_stopped: VM is already TERMINATED
- test_stop_vm_timeout: Operation times out
- test_stop_vm_api_error: GCP API returns error
- test_stop_vm_rollback: Rollback (restart) works correctly
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from gce_rescue_v2.operations.stop_vm import StopVMOperation

class TestStopVMOperation:

    def setup_method(self):
        """Setup test fixtures."""
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

        self.operation = StopVMOperation(
            self.mock_compute,
            self.project,
            self.zone,
            self.logger
        )

    def test_stop_vm_success(self):
        """Test successful stop VM."""
        # Arrange
        # 1. Get current status -> RUNNING
        self.mock_compute.instances().get().execute.side_effect = [
            {'status': 'RUNNING'},    # Initial check
            {'status': 'TERMINATED'}  # Check after stop
        ]
        
        # Act
        result = self.operation.execute(vm_name='test-vm', timeout=5)

        # Assert
        assert result.success is True
        assert result.rollback_data['original_status'] == 'RUNNING'
        self.mock_compute.instances().stop.assert_called_once()

    def test_stop_vm_already_stopped(self):
        """Test stopping an already stopped VM."""
        # Arrange
        self.mock_compute.instances().get().execute.return_value = {'status': 'TERMINATED'}

        # Act
        result = self.operation.execute(vm_name='test-vm')

        # Assert
        assert result.success is True
        assert result.message == "VM already stopped"
        self.mock_compute.instances().stop.assert_not_called()

    def test_stop_vm_timeout(self):
        """Test timeout waiting for stop."""
        # Arrange
        # Always return RUNNING even after stop called
        self.mock_compute.instances().get().execute.return_value = {'status': 'RUNNING'}

        # Act
        # Force a very short timeout/wait loop for test
        with patch.object(self.operation, '_wait_for_status', return_value=False):
            result = self.operation.execute(vm_name='test-vm', timeout=1)

        # Assert
        assert result.success is False
        assert "Timeout" in result.message

    def test_stop_vm_api_error(self):
        """Test API error during stop."""
        # Arrange
        self.mock_compute.instances().get().execute.return_value = {'status': 'RUNNING'}
        self.mock_compute.instances().stop().execute.side_effect = Exception("API Error")

        # Act
        result = self.operation.execute(vm_name='test-vm')

        # Assert
        assert result.success is False
        assert "API Error" in result.error

    def test_stop_vm_rollback(self):
        """Test rollback restarts the VM."""
        # Arrange
        rollback_data = {'vm_name': 'test-vm', 'original_status': 'RUNNING'}
        
        # Mock wait_for_status to succeed immediately
        with patch.object(self.operation, '_wait_for_status', return_value=True):
            success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.instances().start.assert_called_with(
            project=self.project, zone=self.zone, instance='test-vm'
        )

    def test_stop_vm_rollback_not_needed(self):
        """Test rollback skips if VM was originally stopped."""
        # Arrange
        rollback_data = {'vm_name': 'test-vm', 'original_status': 'TERMINATED'}

        # Act
        success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.instances().start.assert_not_called()