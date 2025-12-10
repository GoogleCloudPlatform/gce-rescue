"""
Edge case tests for GCE Rescue V2.

Exercises unusual scenarios that might break the rescue workflow.
"""

from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

from operations.attach_disk import AttachDiskOperation
from operations.stop_vm import StopVMOperation
from operations.create_snapshot import CreateSnapshotOperation
from validators.vm_state import VMStateValidator
from orchestration.rollback import RollbackHandler
from orchestration.state import StateTracker


def _exec(payload=None, exc=None):
    stub = Mock()
    if exc:
        stub.execute.side_effect = exc
    else:
        stub.execute.return_value = payload
    return stub


class TestDiskNameEdgeCases:
    """Tests for unusual disk names."""

    @pytest.mark.parametrize(
        "disk_name",
        [
            "disk-123-boot",
            "a",
            "a" * 63,
            "my--disk",
        ],
    )
    def test_disk_name_variants_attach(self, mock_compute, disk_name):
        """AttachDiskOperation should pass through disk name unchanged."""
        instances = mock_compute.instances.return_value
        instances.attachDisk.return_value = _exec({})

        op = AttachDiskOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", disk_name=disk_name)

        assert result.success is True
        instances.attachDisk.assert_called_once()
        call_kwargs = instances.attachDisk.call_args.kwargs
        assert call_kwargs["body"]["deviceName"] == disk_name


class TestVMStateEdgeCases:
    """Tests for unusual VM states."""

    def test_vm_state_changes_during_stop(self, mock_compute):
        """VM transitions RUNNING -> STOPPING -> TERMINATED during stop."""
        instances = mock_compute.instances.return_value
        instances.get.side_effect = [
            _exec({"status": "RUNNING"}),  # initial fetch
            _exec({"status": "RUNNING"}),
            _exec({"status": "STOPPING"}),
            _exec({"status": "TERMINATED"}),
        ]
        instances.stop.return_value = _exec({})

        op = StopVMOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", timeout=30)

        assert result.success is True
        assert "VM stopped" in result.message

    def test_vm_deleted_during_operation(self, mock_compute):
        """Stop operation handles VM disappearing."""
        resp = Mock(status=404, reason="Not Found")
        error = HttpError(resp, b"{}")
        instances = mock_compute.instances.return_value
        instances.get.side_effect = error

        op = StopVMOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1")

        assert result.success is False
        assert "Failed to stop VM" in result.message

    def test_vm_with_no_boot_disk(self, mock_compute):
        """VM missing boot disk should fail validation."""
        payload = {"status": "RUNNING", "disks": []}
        instances = mock_compute.instances.return_value
        instances.get.return_value.execute.return_value = payload

        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")
        result = validator.validate()

        assert result.passed is False
        assert "no boot disk" in result.message.lower()

    def test_vm_with_multiple_boot_disks(self, mock_compute):
        """Multiple boot disks: validator should still pass using first boot disk."""
        payload = {
            "status": "RUNNING",
            "disks": [
                {"source": "projects/p/zones/z/disks/d1", "boot": True, "deviceName": "d1"},
                {"source": "projects/p/zones/z/disks/d2", "boot": True, "deviceName": "d2"},
            ],
        }
        instances = mock_compute.instances.return_value
        instances.get.return_value.execute.return_value = payload

        validator = VMStateValidator(mock_compute, "proj", "zone", "vm-1")
        result = validator.validate()

        assert result.passed is True
        assert result.details["boot_disk"] == "d1"


class TestSnapshotEdgeCases:
    """Tests for snapshot edge cases."""

    def test_snapshot_quota_exceeded(self, mock_compute):
        """Quota exceeded should surface as failure."""
        disks = mock_compute.disks.return_value
        disks.createSnapshot.side_effect = Exception("quota exceeded")

        op = CreateSnapshotOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(disk_name="d1", wait=False)

        assert result.success is False
        assert "Failed to create snapshot" in result.message

    def test_snapshot_name_collision(self, mock_compute):
        """Name collision handled as failure."""
        disks = mock_compute.disks.return_value
        disks.createSnapshot.side_effect = Exception("already exists")

        op = CreateSnapshotOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(disk_name="d1", snapshot_name="dup", wait=False)

        assert result.success is False
        assert "already exists" in result.error

    def test_snapshot_on_encrypted_disk_failure(self, mock_compute):
        """Snapshot creation failure returns proper error."""
        disks = mock_compute.disks.return_value
        disks.createSnapshot.return_value = _exec({})
        snaps = mock_compute.snapshots.return_value
        snaps.get.return_value = _exec({"status": "FAILED"})

        op = CreateSnapshotOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(disk_name="d1", snapshot_name="snap-1", wait=True, timeout=10)

        assert result.success is False
        assert result.error == "snapshot_failed"


class TestNetworkEdgeCases:
    """Tests for network/API edge cases."""

    def test_api_timeout_on_stop(self, mock_compute, monkeypatch):
        """Timeout during stop operation returns failure."""
        instances = mock_compute.instances.return_value
        instances.get.return_value = _exec({"status": "RUNNING"})
        instances.stop.return_value = _exec({})

        monkeypatch.setattr(
            StopVMOperation, "_wait_for_status", lambda self, func, target, timeout=300: False
        )

        op = StopVMOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", timeout=1)

        assert result.success is False
        assert "Timeout" in result.message

    def test_api_timeout_on_attach(self, mock_compute):
        """Timeout during attach operation bubbles up as error."""
        instances = mock_compute.instances.return_value
        instances.attachDisk.side_effect = TimeoutError("attach timeout")

        op = AttachDiskOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", disk_name="disk-1")

        assert result.success is False
        assert "attach timeout" in result.error

    def test_intermittent_api_failures(self, mock_compute):
        """Intermittent API failure results in failure outcome."""
        instances = mock_compute.instances.return_value
        instances.attachDisk.side_effect = [Exception("flaky"), _exec({})]

        op = AttachDiskOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", disk_name="disk-1")

        assert result.success is False
        assert "flaky" in result.error

    def test_rate_limiting(self, mock_compute):
        """429 errors treated as failures."""
        resp = Mock(status=429, reason="Too Many Requests")
        error = HttpError(resp, b"{}")
        instances = mock_compute.instances.return_value
        instances.attachDisk.side_effect = error

        op = AttachDiskOperation(mock_compute, "proj", "zone", Mock())
        result = op.execute(vm_name="vm-1", disk_name="disk-1")

        assert result.success is False
        assert "Too Many Requests" in str(result.error)


class TestRollbackEdgeCases:
    """Tests for rollback edge cases."""

    def test_rollback_when_operation_missing(self):
        """Missing operation in map should mark failure."""
        tracker = StateTracker()
        tracker.add_operation("OpMissing", True, "ok", rollback_data={"x": 1})

        handler = RollbackHandler(Mock())
        result = handler.rollback(tracker, {})

        assert result is False
        assert "OpMissing" in handler.failed_rollbacks

    def test_rollback_when_disk_in_use(self):
        """Rollback failure recorded when operation returns False."""
        tracker = StateTracker()
        tracker.add_operation("Detach Boot Disk", True, "ok", rollback_data={"x": 1})

        class BusyOp:
            def rollback(self, data):
                return False

        handler = RollbackHandler(Mock())
        result = handler.rollback(tracker, {"Detach Boot Disk": BusyOp()})

        assert result is False
        assert "Detach Boot Disk" in handler.failed_rollbacks

    def test_partial_rollback_recovery(self):
        """Partial rollback still attempts remaining operations."""
        tracker = StateTracker()
        tracker.add_operation("Op1", True, "ok", rollback_data={"a": 1})
        tracker.add_operation("Op2", True, "ok", rollback_data={"b": 2})

        class FailOnce:
            def __init__(self):
                self.calls = 0

            def rollback(self, data):
                self.calls += 1
                return False

        class Succeed:
            def __init__(self):
                self.calls = 0

            def rollback(self, data):
                self.calls += 1
                return True

        op1 = FailOnce()
        op2 = Succeed()

        handler = RollbackHandler(Mock())
        result = handler.rollback(tracker, {"Op1": op1, "Op2": op2})

        assert result is False
        assert op1.calls == 1
        assert op2.calls == 1

