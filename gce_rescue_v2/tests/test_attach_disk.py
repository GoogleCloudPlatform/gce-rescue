"""
Unit tests for AttachDiskOperation.

Tests:
- test_attach_disk_success: Normal successful execution
- test_attach_disk_api_error: GCP API returns error
- test_attach_disk_rollback: Rollback (detach) works correctly
"""

import pytest
from unittest.mock import Mock, patch
from gce_rescue_v2.operations.attach_disk import AttachDiskOperation

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
        assert "Disk not found" in result.message

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
    def test_attach_disk_defaults_device_name_to_disk_name(self):
        """Without device_name the disk name is used and no interface is sent."""
        with patch.object(self.operation, '_wait_for_operation', return_value=True):
            result = self.operation.execute(vm_name='test-vm', disk_name='orig-disk')

        body = self.mock_compute.instances().attachDisk.call_args[1]['body']
        assert body['deviceName'] == 'orig-disk'
        assert body['autoDelete'] is False
        assert 'interface' not in body
        assert result.rollback_data['device_name'] == 'orig-disk'

    def test_attach_disk_uses_given_device_name_and_interface(self):
        """Restore passes the original boot attachment settings through (#170)."""
        with patch.object(self.operation, '_wait_for_operation', return_value=True):
            result = self.operation.execute(
                vm_name='test-vm', disk_name='orig-disk', boot=True,
                auto_delete=True, device_name='persistent-disk-0', interface='NVME'
            )

        body = self.mock_compute.instances().attachDisk.call_args[1]['body']
        assert body['deviceName'] == 'persistent-disk-0'
        assert body['autoDelete'] is True
        assert body['interface'] == 'NVME'
        assert body['mode'] == 'READ_WRITE'
        assert result.rollback_data['device_name'] == 'persistent-disk-0'

    def test_attach_disk_rollback_detaches_device_name_used(self):
        """Rollback must detach by the device name actually attached, not the disk name."""
        with patch.object(self.operation, '_wait_for_operation', return_value=True):
            result = self.operation.execute(
                vm_name='test-vm', disk_name='orig-disk', boot=True,
                device_name='persistent-disk-0'
            )
            assert self.operation.rollback(result.rollback_data) is True

        self.mock_compute.instances().detachDisk.assert_called_with(
            project=self.project,
            zone=self.zone,
            instance='test-vm',
            deviceName='persistent-disk-0'
        )
