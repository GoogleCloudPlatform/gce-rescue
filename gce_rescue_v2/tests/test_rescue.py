import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.core.config import RescueConfig
from gce_rescue_v2.operations.base import OperationResult, BaseOperation


class _Exec:
    def __init__(self, value=None):
        self._value = value

    def execute(self):
        return self._value


class FakeInstances:
    """Minimal fake for compute.instances() used in orchestration/operations."""

    def __init__(self, get_responses):
        # Each call to get().execute() returns the next dict in this list
        self._get_responses = list(get_responses)

    # RPC builders
    def get(self, project=None, zone=None, instance=None):
        value = self._get_responses.pop(0) if self._get_responses else {}
        return _Exec(value)

    def stop(self, project=None, zone=None, instance=None):
        return _Exec({})

    def start(self, project=None, zone=None, instance=None):
        return _Exec({})

    def detachDisk(self, project=None, zone=None, instance=None, deviceName=None):
        return _Exec({})

    def attachDisk(self, project=None, zone=None, instance=None, body=None):
        return _Exec({})

    def setMetadata(self, project=None, zone=None, instance=None, body=None):
        return _Exec({})


class FakeDisks:
    def insert(self, project=None, zone=None, body=None):
        return _Exec({})

    def createSnapshot(self, project=None, zone=None, disk=None, body=None):
        return _Exec({})


class FakeSnapshots:
    def get(self, project=None, snapshot=None):
        # Return READY status for snapshot wait check
        return _Exec({'status': 'READY'})


class FakeCompute:
    def __init__(self, instances_get_responses):
        self._instances = FakeInstances(instances_get_responses)
        self._disks = FakeDisks()
        self._snapshots = FakeSnapshots()

    def instances(self):
        return self._instances

    def disks(self):
        return self._disks

    def snapshots(self):
        return self._snapshots


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def always_wait_ok(monkeypatch):
    # Avoid polling in operations; pretend target state is reached
    monkeypatch.setattr(BaseOperation, "_wait_for_status", lambda self, fn, target, timeout=None: True)


def _default_instances_get_sequence(original_device_name="sda", original_disk_name="original-boot"):
    """Sequence of responses for instances().get().execute() calls in happy path.

    Order:
      1) _get_original_disk_info
      2) StopVMOperation initial status
      3) SetMetadataOperation fetch fingerprint
      4) StartVMOperation initial status
    """
    return [
        {
            "disks": [
                {
                    "boot": True,
                    "source": f"projects/p/zones/z/disks/{original_disk_name}",
                    "deviceName": original_device_name,
                }
            ]
        },
        {"status": "RUNNING"},
        {"metadata": {"fingerprint": "fp123", "items": []}},
        {"status": "TERMINATED"},
        {"status": "TERMINATED"},  # Extra for additional checks if needed
    ]


@pytest.mark.skip(reason="Mock needs enhancement for full rescue flow")
def test_rescue_async_snapshot_success(monkeypatch):
    # Arrange: async snapshot enabled, don't require completion
    compute = FakeCompute(_default_instances_get_sequence())
    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=False)

    captured = SimpleNamespace(wait_param=None)

    def fake_execute(self, disk_name, snapshot_name=None, description=None, timeout=600, wait=True):
        captured.wait_param = wait
        return OperationResult(
            operation_name=self.name,
            success=True,
            message="Snapshot started",
            rollback_data={"snapshot_name": "snap-123", "created_by_operation": True},
        )

    from gce_rescue_v2.operations.create_snapshot import CreateSnapshotOperation

    monkeypatch.setattr(CreateSnapshotOperation, "execute", fake_execute)

    orch = RescueOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)

    # Act
    ok = orch.execute()

    # Assert
    assert ok is True
    assert captured.wait_param is False  # async path should not wait


def test_rescue_async_snapshot_failure_required_aborts(monkeypatch):
    # Arrange: async snapshot enabled and required -> should abort on failure
    compute = FakeCompute(_default_instances_get_sequence())
    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=True)

    def failing_snapshot(self, disk_name, snapshot_name=None, description=None, timeout=600, wait=True):
        return OperationResult(
            operation_name=self.name,
            success=False,
            message="Snapshot failed",
            error="boom",
            rollback_data=None,
        )

    from gce_rescue_v2.operations.create_snapshot import CreateSnapshotOperation

    monkeypatch.setattr(CreateSnapshotOperation, "execute", failing_snapshot)

    orch = RescueOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)

    # Act
    ok = orch.execute()

    # Assert
    assert ok is False  # Abort when snapshot is required and fails


@pytest.mark.skip(reason="Mock needs enhancement for full rescue flow")
def test_rescue_async_snapshot_failure_not_required_continues(monkeypatch):
    # Arrange: async snapshot enabled but not required -> should continue and succeed
    compute = FakeCompute(_default_instances_get_sequence())
    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=False)

    def failing_snapshot(self, disk_name, snapshot_name=None, description=None, timeout=600, wait=True):
        return OperationResult(
            operation_name=self.name,
            success=False,
            message="Snapshot failed",
            error="boom",
            rollback_data=None,
        )

    from gce_rescue_v2.operations.create_snapshot import CreateSnapshotOperation

    monkeypatch.setattr(CreateSnapshotOperation, "execute", failing_snapshot)

    orch = RescueOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)

    # Act
    ok = orch.execute()

    # Assert
    assert ok is True  # Should proceed without snapshot when not required

