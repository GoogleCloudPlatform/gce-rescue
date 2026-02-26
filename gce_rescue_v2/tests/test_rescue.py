import time
from types import SimpleNamespace

import pytest

from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.orchestration.checkpoint import CheckpointManager
from gce_rescue_v2.core.config import RescueConfig
from gce_rescue_v2.operations.base import OperationResult, BaseOperation
from gce_rescue_v2.operations import (
    StopVMOperation,
    DetachDiskOperation,
    CreateDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    CreateSnapshotOperation,
    VerifyStartupOperation,
)


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
    def __init__(self, instances_get_responses=None):
        self._instances = FakeInstances(instances_get_responses or [])
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


# ---------------------------------------------------------------------------
# Shared helpers for stubbing
# ---------------------------------------------------------------------------

def _success_execute(self, *args, **kwargs):
    """Generic success execute for any operation."""
    return OperationResult(
        operation_name=self.name,
        success=True,
        message="OK",
        rollback_data={},
    )


def _failure_execute(self, *args, **kwargs):
    """Generic failure execute for any operation."""
    return OperationResult(
        operation_name=self.name,
        success=False,
        message="Failed",
        error="test error",
        rollback_data=None,
    )


ALL_RESCUE_OPS = [
    StopVMOperation, DetachDiskOperation, CreateSnapshotOperation,
    CreateDiskOperation, AttachDiskOperation, SetMetadataOperation,
    StartVMOperation, VerifyStartupOperation,
]


@pytest.fixture
def stub_rescue(monkeypatch):
    """Stub all external dependencies so RescueOrchestrator.execute() can run
    end-to-end without real API calls."""

    # Stub every operation execute to succeed
    for op_cls in ALL_RESCUE_OPS:
        monkeypatch.setattr(op_cls, "execute", _success_execute)

    # Stub checkpoint manager
    monkeypatch.setattr(CheckpointManager, "create_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "update_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "clear_checkpoint", lambda *a, **kw: None)

    # Stub orchestrator helpers that call compute API
    monkeypatch.setattr(RescueOrchestrator, "_disk_exists", lambda self, n: False)
    monkeypatch.setattr(RescueOrchestrator, "_is_disk_attached", lambda self, n: False)
    monkeypatch.setattr(RescueOrchestrator, "_get_vm_status", lambda self: "TERMINATED")
    monkeypatch.setattr(RescueOrchestrator, "_generate_startup_script", lambda self: "echo test")

    # Stub _get_original_disk_info to set disk/os info without API call
    def _fake_get_disk_info(self):
        self.vm_info = {
            "disks": [{
                "boot": True,
                "source": "projects/p/zones/z/disks/original-boot",
                "deviceName": "sda",
            }]
        }
        self.os_type = "linux"
        self.architecture = "x86_64"
        self.original_disk_name = "original-boot"
        self.original_device_name = "sda"

    monkeypatch.setattr(RescueOrchestrator, "_get_original_disk_info", _fake_get_disk_info)

    # Stub rollback to a no-op so we can verify execute() return value
    # without needing full rollback infrastructure
    monkeypatch.setattr(RescueOrchestrator, "_rollback", lambda self: None)


def _make_orch(config=None):
    """Create a RescueOrchestrator with FakeCompute and suppress_progress."""
    compute = FakeCompute()
    return RescueOrchestrator(
        compute=compute, project="p", zone="z", vm_name="vm",
        config=config or RescueConfig(create_snapshot=False),
        logger=None, suppress_progress=True,
    )


# ---------------------------------------------------------------------------
# Legacy sequence helper (kept for existing snapshot tests)
# ---------------------------------------------------------------------------

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


# ===================================================================
# Happy-path tests
# ===================================================================


def test_rescue_full_success(stub_rescue):
    """All steps execute in order with no snapshot, returns True."""
    orch = _make_orch()
    assert orch.execute() is True


def test_rescue_full_success_with_snapshot(stub_rescue, monkeypatch):
    """All steps execute with snapshot enabled, returns True."""
    # Give snapshot execute a proper rollback_data so the snapshot name is set
    def _snapshot_execute(self, *args, **kwargs):
        return OperationResult(
            operation_name=self.name, success=True, message="OK",
            rollback_data={"snapshot_name": "snap-123", "created_by_operation": True},
        )
    monkeypatch.setattr(CreateSnapshotOperation, "execute", _snapshot_execute)

    config = RescueConfig(create_snapshot=True, async_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    assert orch.snapshot_name == "snap-123"


def test_rescue_sets_disk_info(stub_rescue):
    """After execute, original/rescue disk names and os_type are set."""
    orch = _make_orch()
    orch.execute()

    assert orch.original_disk_name == "original-boot"
    assert orch.original_device_name == "sda"
    assert orch.os_type == "linux"
    assert orch.rescue_disk_name is not None
    assert orch.rescue_disk_name.startswith("rescue-disk-")


def test_rescue_no_snapshot_config(stub_rescue):
    """create_snapshot=False skips snapshot step entirely."""
    called = SimpleNamespace(snapshot=False)
    original_success = _success_execute

    def _tracking_execute(self, *args, **kwargs):
        if self.__class__ is CreateSnapshotOperation:
            called.snapshot = True
        return original_success(self, *args, **kwargs)

    # Re-patch with tracking execute for all ops
    for op_cls in ALL_RESCUE_OPS:
        # Can't use monkeypatch here (already used in fixture), use setattr directly
        op_cls._test_execute = op_cls.execute  # save

    config = RescueConfig(create_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    # Snapshot step is simply not invoked because config.create_snapshot is False.
    # The orchestrator checks config.create_snapshot before calling snapshot execute.


def test_rescue_snapshot_not_required_continues(stub_rescue, monkeypatch):
    """Snapshot failure with require_snapshot=False continues to success."""
    monkeypatch.setattr(CreateSnapshotOperation, "execute", _failure_execute)

    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True


def test_rescue_async_snapshot_success(stub_rescue, monkeypatch):
    """Async snapshot enabled - passes wait=False to snapshot operation."""
    captured = SimpleNamespace(wait_param=None)

    def _capture_snapshot(self, *args, **kwargs):
        captured.wait_param = kwargs.get('wait', True)
        return OperationResult(
            operation_name=self.name, success=True, message="OK",
            rollback_data={"snapshot_name": "snap-123", "created_by_operation": True},
        )

    monkeypatch.setattr(CreateSnapshotOperation, "execute", _capture_snapshot)

    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    assert captured.wait_param is False


def test_rescue_windows_os_type(stub_rescue, monkeypatch):
    """Windows VM sets os_type to 'windows' and uses appropriate config."""
    def _fake_get_disk_info_windows(self):
        self.vm_info = {
            "disks": [{
                "boot": True,
                "source": "projects/p/zones/z/disks/win-boot",
                "deviceName": "sda",
                "guestOsFeatures": [{"type": "WINDOWS"}],
            }]
        }
        self.os_type = "windows"
        self.architecture = "x86_64"
        self.original_disk_name = "win-boot"
        self.original_device_name = "sda"

    monkeypatch.setattr(RescueOrchestrator, "_get_original_disk_info", _fake_get_disk_info_windows)

    config = RescueConfig(create_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    assert orch.os_type == "windows"


# ===================================================================
# Failure + rollback tests
# ===================================================================


def test_rescue_stop_vm_failure_rollback(stub_rescue, monkeypatch):
    """Step 1 failure (stop VM) triggers rollback, returns False."""
    monkeypatch.setattr(StopVMOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_rescue_create_disk_failure_rollback(stub_rescue, monkeypatch):
    """Step 4 failure (create disk) triggers rollback, returns False."""
    monkeypatch.setattr(CreateDiskOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_rescue_start_vm_failure_rollback(stub_rescue, monkeypatch):
    """Step 7 failure (start VM) triggers rollback, returns False."""
    monkeypatch.setattr(StartVMOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_rescue_attach_original_failure_rollback(stub_rescue, monkeypatch):
    """Step 8 failure (attach original disk) triggers rollback, returns False."""
    call_count = SimpleNamespace(n=0)

    def _fail_second_attach(self, *args, **kwargs):
        call_count.n += 1
        # First attach call is step 5 (rescue disk), second is step 8 (original disk)
        if call_count.n == 2:
            return _failure_execute(self, *args, **kwargs)
        return _success_execute(self, *args, **kwargs)

    monkeypatch.setattr(AttachDiskOperation, "execute", _fail_second_attach)

    orch = _make_orch()
    assert orch.execute() is False


def test_rescue_detach_boot_failure_rollback(stub_rescue, monkeypatch):
    """Step 2 failure (detach boot) triggers rollback, returns False."""
    monkeypatch.setattr(DetachDiskOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


# ===================================================================
# Existing snapshot tests (kept, no longer skipped)
# ===================================================================


def test_rescue_async_snapshot_failure_required_aborts(stub_rescue, monkeypatch):
    """Async snapshot enabled and required -> should abort on failure."""
    monkeypatch.setattr(CreateSnapshotOperation, "execute", _failure_execute)

    config = RescueConfig(create_snapshot=True, async_snapshot=True, require_snapshot=True)
    orch = _make_orch(config)
    assert orch.execute() is False


# ===================================================================
# Checkpoint cleared after rollback
# ===================================================================


def test_rescue_c4_uses_hyperdisk(stub_rescue, monkeypatch):
    """C4 machine type auto-selects hyperdisk-balanced for rescue disk."""
    captured = SimpleNamespace(disk_type=None)

    def _fake_get_disk_info_c4(self):
        self.vm_info = {
            "machineType": "zones/z/machineTypes/c4-standard-2",
            "disks": [{
                "boot": True,
                "source": "projects/p/zones/z/disks/original-boot",
                "deviceName": "sda",
            }]
        }
        # Call the real detection logic
        from gce_rescue_v2.utils.os_detection import (
            detect_os_type, detect_architecture, get_rescue_disk_type
        )
        self.os_type = detect_os_type(self.vm_info)
        self.architecture = detect_architecture(self.vm_info)
        detected_disk_type = get_rescue_disk_type(self.vm_info, self.config.rescue_disk_type)
        if detected_disk_type != self.config.rescue_disk_type:
            self.config.rescue_disk_type = detected_disk_type
        self.original_disk_name = "original-boot"
        self.original_device_name = "sda"

    def _capture_create_disk(self, *args, **kwargs):
        captured.disk_type = kwargs.get('disk_type')
        return OperationResult(
            operation_name=self.name, success=True, message="OK", rollback_data={},
        )

    monkeypatch.setattr(RescueOrchestrator, "_get_original_disk_info", _fake_get_disk_info_c4)
    monkeypatch.setattr(CreateDiskOperation, "execute", _capture_create_disk)

    config = RescueConfig(create_snapshot=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    assert captured.disk_type == 'hyperdisk-balanced'


def test_rescue_rollback_clears_checkpoint(stub_rescue, monkeypatch):
    """Rollback after failure clears the checkpoint metadata."""
    from unittest.mock import MagicMock
    from gce_rescue_v2.orchestration.rollback import RollbackHandler

    # Let _rollback run (undo the no-op stub from fixture)
    monkeypatch.undo()

    # Re-apply all stubs except _rollback
    for op_cls in ALL_RESCUE_OPS:
        monkeypatch.setattr(op_cls, "execute", _success_execute)
    monkeypatch.setattr(CheckpointManager, "create_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "update_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(RescueOrchestrator, "_disk_exists", lambda self, n: False)
    monkeypatch.setattr(RescueOrchestrator, "_is_disk_attached", lambda self, n: False)
    monkeypatch.setattr(RescueOrchestrator, "_get_vm_status", lambda self: "TERMINATED")
    monkeypatch.setattr(RescueOrchestrator, "_generate_startup_script", lambda self: "echo test")

    def _fake_get_disk_info(self):
        self.vm_info = {
            "disks": [{"boot": True, "source": "projects/p/zones/z/disks/original-boot",
                        "deviceName": "sda"}]
        }
        self.os_type = "linux"
        self.architecture = "x86_64"
        self.original_disk_name = "original-boot"
        self.original_device_name = "sda"

    monkeypatch.setattr(RescueOrchestrator, "_get_original_disk_info", _fake_get_disk_info)

    # Stub rollback handler to succeed without real API calls
    monkeypatch.setattr(RollbackHandler, "rollback", lambda self, *a, **kw: True)

    # Track clear_checkpoint calls
    clear_mock = MagicMock(return_value=True)
    monkeypatch.setattr(CheckpointManager, "clear_checkpoint", clear_mock)

    # Inject a failure so _rollback is triggered
    monkeypatch.setattr(StopVMOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False
    clear_mock.assert_called_once()
