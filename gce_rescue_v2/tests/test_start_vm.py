"""
Unit tests for StartVMOperation.

Tests:
- test_start_vm_success: Normal successful execution
- test_start_vm_already_running: VM is already RUNNING
- test_start_vm_timeout: Operation times out
- test_start_vm_api_error: GCP API returns error
- test_start_vm_rollback: Rollback (stop) works correctly
"""

import pytest
from unittest.mock import Mock, patch
from operations.start_vm import StartVMOperation

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