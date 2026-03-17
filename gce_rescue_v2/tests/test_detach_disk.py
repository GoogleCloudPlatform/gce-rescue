"""
Unit tests for DetachDiskOperation.

Tests:
- test_detach_disk_success: Normal successful execution
- test_detach_disk_already_done: Disk not found on VM
- test_detach_disk_timeout: Operation times out
- test_detach_disk_api_error: GCP API returns error
- test_detach_disk_rollback: Rollback works correctly
"""

from unittest.mock import Mock

import pytest

from gce_rescue_v2.operations.detach_disk import DetachDiskOperation


def _exec_stub(payload=None, exc=None):
    stub = Mock()
    if exc:
        stub.execute.side_effect = exc
    else:
        stub.execute.return_value = payload
    return stub


class TestDetachDiskOperation:
    """Tests for DetachDiskOperation behavior."""

    def setup_method(self):
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

    def test_detach_disk_success(self, mock_compute):
        """Test successful disk detachment."""
        vm_payload = {
            "disks": [
                {
                    "source": "projects/test/zones/us-central1-a/disks/test-disk",
                    "boot": True,
                    "autoDelete": False,
                    "deviceName": "test-disk",
                    "mode": "READ_WRITE",
                }
            ]
        }
        instances = mock_compute.instances.return_value
        instances.get.return_value = _exec_stub(vm_payload)
        instances.detachDisk.return_value = _exec_stub({})
        mock_compute.zoneOperations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }

        op = DetachDiskOperation(mock_compute, self.project, self.zone, self.logger)
        result = op.execute(vm_name="vm-1", device_name="test-disk")

        assert result.success is True
        assert result.rollback_data["disk_info"]["deviceName"] == "test-disk"
        instances.detachDisk.assert_called_once()

    def test_detach_disk_not_found(self, mock_compute):
        """Test disk not found on VM."""
        instances = mock_compute.instances.return_value
        instances.get.return_value = _exec_stub({"disks": []})

        op = DetachDiskOperation(mock_compute, self.project, self.zone, self.logger)
        result = op.execute(vm_name="vm-1", device_name="missing-disk")

        assert result.success is False
        assert "not found" in result.message

    def test_detach_disk_timeout(self, mock_compute, monkeypatch):
        """Test detach timeout handling."""
        vm_payload = {
            "disks": [
                {
                    "source": "projects/test/zones/us-central1-a/disks/test-disk",
                    "boot": False,
                    "autoDelete": False,
                    "deviceName": "test-disk",
                    "mode": "READ_WRITE",
                }
            ]
        }
        instances = mock_compute.instances.return_value
        instances.get.return_value = _exec_stub(vm_payload)
        instances.detachDisk.return_value = _exec_stub({"name": "op-1"})

        # Simulate timeout via helper
        monkeypatch.setattr(
            DetachDiskOperation,
            "_wait_for_operation",
            lambda self, op_data, timeout=300: False,
        )

        op = DetachDiskOperation(mock_compute, self.project, self.zone, self.logger)
        result = op.execute(vm_name="vm-1", device_name="test-disk",)

        assert result.success is False
        assert "Timeout" in result.message

    def test_detach_disk_api_error(self, mock_compute):
        """Test detach disk API error handling."""
        instances = mock_compute.instances.return_value
        instances.get.side_effect = Exception("api failure")

        op = DetachDiskOperation(mock_compute, self.project, self.zone, self.logger)
        result = op.execute(vm_name="vm-1", device_name="test-disk")

        assert result.success is False
        assert "api failure" in result.message

    def test_detach_disk_rollback(self, mock_compute):
        """Test rollback re-attaches the disk."""
        instances = mock_compute.instances.return_value
        instances.attachDisk.return_value = _exec_stub({})
        mock_compute.zoneOperations.return_value.get.return_value.execute.return_value = {
            "status": "DONE"
        }

        op = DetachDiskOperation(mock_compute, self.project, self.zone, self.logger)
        rollback_data = {
            "vm_name": "vm-1",
            "disk_info": {
                "source": "projects/test/zones/us-central1-a/disks/test-disk",
                "boot": True,
                "autoDelete": False,
                "deviceName": "test-disk",
                "mode": "READ_WRITE",
            },
        }

        assert op.rollback(rollback_data) is True
        instances.attachDisk.assert_called_once()
        call_kwargs = instances.attachDisk.call_args.kwargs
        assert call_kwargs["body"]["source"] == rollback_data["disk_info"]["source"]
        assert call_kwargs["body"]["boot"] is True
