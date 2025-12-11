"""
Unit tests for CreateDiskOperation.

Tests:
- test_create_disk_success: Normal successful execution
- test_create_disk_timeout: Operation times out
- test_create_disk_api_error: GCP API returns error
- test_create_disk_rollback: Rollback (delete) works correctly
"""

import pytest
from unittest.mock import Mock, patch
from gce_rescue_v2.operations.create_disk import CreateDiskOperation

class TestCreateDiskOperation:

    def setup_method(self):
        """Setup test fixtures."""
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

        self.operation = CreateDiskOperation(
            self.mock_compute,
            self.project,
            self.zone,
            self.logger
        )

    def test_create_disk_success(self):
        """Test successful disk creation."""
        # Arrange
        with patch.object(self.operation, '_wait_for_status', return_value=True):
            result = self.operation.execute(
                disk_name='rescue-disk',
                size_gb=10,
                source_image='projects/debian-cloud/global/images/family/debian-12'
            )

        # Assert
        assert result.success is True
        assert result.rollback_data['disk_name'] == 'rescue-disk'
        self.mock_compute.disks().insert.assert_called_once()
        args = self.mock_compute.disks().insert.call_args[1]
        assert args['body']['name'] == 'rescue-disk'
        assert args['body']['sizeGb'] == '10'

    def test_create_disk_timeout(self):
        """Test timeout waiting for disk creation."""
        # Arrange
        with patch.object(self.operation, '_wait_for_status', return_value=False):
            result = self.operation.execute(disk_name='rescue-disk', timeout=1)

        # Assert
        assert result.success is False
        assert "Timeout" in result.message

    def test_create_disk_api_error(self):
        """Test API error during creation."""
        # Arrange
        self.mock_compute.disks().insert().execute.side_effect = Exception("Quota Exceeded")

        # Act
        result = self.operation.execute(disk_name='rescue-disk')

        # Assert
        assert result.success is False
        assert "Quota Exceeded" in result.error

    def test_create_disk_rollback(self):
        """Test rollback deletes the disk."""
        # Arrange
        rollback_data = {'disk_name': 'rescue-disk'}
        
        # Act
        success = self.operation.rollback(rollback_data)

        # Assert
        assert success is True
        self.mock_compute.disks().delete.assert_called_with(
            project=self.project, zone=self.zone, disk='rescue-disk'
        )