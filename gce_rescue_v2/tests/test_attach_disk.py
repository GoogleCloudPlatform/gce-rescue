"""
Unit tests for AttachDiskOperation.

Tests:
- test_attach_disk_success: Normal successful execution
- test_attach_disk_api_error: GCP API returns error
- test_attach_disk_rollback: Rollback (detach) works correctly
"""

import pytest
from unittest.mock import Mock, patch
from operations.attach_disk import AttachDiskOperation

class TestAttachDiskOperation:

    def setup_method(self):
        """Setup test fixtures."""
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

        self.operation = AttachDiskOperation(
            self.mock_compute,
            self.project,
            self.zone,
            self.logger
        )

    def test_attach_disk_success(self):
        """Test successful disk attachment."""
        # Arrange
        # Use patched _wait_for_operation from new BaseOperation fix
        with patch.object(self.operation, '_wait_for_operation', return_value=True):
            result = self.operation.execute(
                vm_name='test-vm',
                disk_name='rescue-disk',
                boot=True
            )

        # Assert
        assert result.success is True
        assert result.rollback_data['vm_name'] == 'test-vm'
        assert result.rollback_data['device_name'] == 'rescue-disk'
        
        self.mock_compute.instances().attachDisk.assert_called_once()
        args = self.mock_compute.instances().attachDisk.call_args[1]
        assert args['body']['boot'] is True
        assert args['body']['deviceName'] == 'rescue-disk'

    def test_attach_disk_api_error(self):
        """Test API error during attachment."""
        # Arrange
        self.mock_compute.instances().attachDisk().execute.side_effect = Exception("Disk not found")

        # Act
        result = self.operation.execute(vm_name='test-vm', disk_name='rescue-disk')

        # Assert
        assert result.success is False
        assert "Disk not found" in result.error

    def test_attach_disk_rollback(self):
        """Test rollback detaches the disk."""
        # Arrange
        rollback_data = {'vm_name': 'test-vm', 'device_name': 'rescue-disk'}
        
        with patch.object(self.operation, '_wait_for_operation', return_value=True):
            success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.instances().detachDisk.assert_called_with(
            project=self.project,
            zone=self.zone,
            instance='test-vm',
            deviceName='rescue-disk'
        )