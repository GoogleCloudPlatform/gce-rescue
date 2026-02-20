import time
from types import SimpleNamespace

import pytest

from gce_rescue_v2.orchestration.restore import RestoreOrchestrator
from gce_rescue_v2.orchestration.checkpoint import CheckpointManager
from gce_rescue_v2.core.config import RestoreConfig
from gce_rescue_v2.operations.base import BaseOperation, OperationResult
from gce_rescue_v2.operations import (
    StopVMOperation,
    DetachDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    DeleteDiskOperation,
)


class _Exec:
    def __init__(self, value=None):
        self._value = value

    def execute(self):
        return self._value


class FakeInstances:
    def __init__(self, get_responses):
        self._get_responses = list(get_responses)

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
    def delete(self, project=None, zone=None, disk=None):
        return _Exec({})


class FakeSnapshots:
    def list(self, project=None, filter=None):
        return _Exec({'items': []})


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
    monkeypatch.setattr(BaseOperation, "_wait_for_status", lambda self, fn, target, timeout=None: True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _success_execute(self, *args, **kwargs):
    return OperationResult(
        operation_name=self.name, success=True, message="OK", rollback_data={},
    )


def _failure_execute(self, *args, **kwargs):
    return OperationResult(
        operation_name=self.name, success=False, message="Failed",
        error="test error", rollback_data=None,
    )


ALL_RESTORE_OPS = [
    StopVMOperation, DetachDiskOperation, AttachDiskOperation,
    SetMetadataOperation, StartVMOperation, DeleteDiskOperation,
]


@pytest.fixture
def stub_restore(monkeypatch):
    """Stub all external dependencies so RestoreOrchestrator.execute() can run."""

    for op_cls in ALL_RESTORE_OPS:
        monkeypatch.setattr(op_cls, "execute", _success_execute)

    # Stub checkpoint manager
    monkeypatch.setattr(CheckpointManager, "create_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "update_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "clear_checkpoint", lambda *a, **kw: None)

    # Stub orchestrator helpers
    monkeypatch.setattr(RestoreOrchestrator, "_is_disk_attached", lambda self, n: True)
    monkeypatch.setattr(RestoreOrchestrator, "_is_disk_boot", lambda self, n: False)
    monkeypatch.setattr(RestoreOrchestrator, "_get_vm_status", lambda self: "TERMINATED")
    monkeypatch.setattr(RestoreOrchestrator, "_disk_exists", lambda self, n: True)
    monkeypatch.setattr(RestoreOrchestrator, "_check_snapshot_status", lambda self: None)

    # Stub _get_disk_info to set disk names without API call
    def _fake_get_disk_info(self):
        self.rescue_disk_name = "rescue-disk-123"
        self.rescue_device_name = "disk-rescue"
        self.original_disk_name = "orig-disk"
        self.original_device_name = "sda"

    monkeypatch.setattr(RestoreOrchestrator, "_get_disk_info", _fake_get_disk_info)

    # Stub _get_clean_metadata
    monkeypatch.setattr(
        RestoreOrchestrator, "_get_clean_metadata",
        lambda self: [{"key": "ssh-keys", "value": "user:key"}],
    )

    # Stub rollback
    monkeypatch.setattr(RestoreOrchestrator, "_rollback", lambda self: None)


def _make_orch(config=None):
    compute = FakeCompute()
    return RestoreOrchestrator(
        compute=compute, project="p", zone="z", vm_name="vm",
        config=config or RestoreConfig(delete_rescue_disk=True),
        logger=None, suppress_progress=True,
    )


# ===================================================================
# Happy-path tests
# ===================================================================


def test_restore_full_success(stub_restore):
    """All 7 steps execute, rescue disk deleted, returns True."""
    orch = _make_orch()
    assert orch.execute() is True


def test_restore_preserves_rescue_disk(stub_restore, monkeypatch):
    """delete_rescue_disk=False skips deletion step."""
    called = SimpleNamespace(deleted=False)

    def _track_delete(self, *args, **kwargs):
        called.deleted = True
        return _success_execute(self, *args, **kwargs)

    monkeypatch.setattr(DeleteDiskOperation, "execute", _track_delete)

    config = RestoreConfig(delete_rescue_disk=False)
    orch = _make_orch(config)
    assert orch.execute() is True
    assert called.deleted is False


def test_restore_disk_info_set(stub_restore):
    """After execute, disk names are correctly populated."""
    orch = _make_orch()
    orch.execute()

    assert orch.rescue_disk_name == "rescue-disk-123"
    assert orch.original_disk_name == "orig-disk"
    assert orch.rescue_device_name == "disk-rescue"
    assert orch.original_device_name == "sda"


def test_restore_delete_disk_failure_nonfatal(stub_restore, monkeypatch):
    """Step 7 delete failure doesn't trigger rollback - returns True."""
    monkeypatch.setattr(DeleteDiskOperation, "execute", _failure_execute)

    orch = _make_orch()
    # Delete failure is non-fatal (rescue disk can be cleaned up manually)
    assert orch.execute() is True


# ===================================================================
# Failure + rollback tests
# ===================================================================


def test_restore_stop_vm_failure_rollback(stub_restore, monkeypatch):
    """Step 1 failure (stop VM) triggers rollback, returns False."""
    monkeypatch.setattr(StopVMOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_restore_attach_original_failure_rollback(stub_restore, monkeypatch):
    """Step 4 failure (attach original as boot) triggers rollback, returns False."""
    monkeypatch.setattr(AttachDiskOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_restore_start_vm_failure_rollback(stub_restore, monkeypatch):
    """Step 6 failure (start VM) triggers rollback, returns False."""
    monkeypatch.setattr(StartVMOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


def test_restore_set_metadata_failure_rollback(stub_restore, monkeypatch):
    """Step 5 failure (set metadata) triggers rollback, returns False."""
    monkeypatch.setattr(SetMetadataOperation, "execute", _failure_execute)

    orch = _make_orch()
    assert orch.execute() is False


# ===================================================================
# Unit tests for helper methods
# ===================================================================


def test_restore_get_clean_metadata():
    """_get_clean_metadata() removes rescue keys and restores backups."""
    vm_response = {
        "metadata": {
            "fingerprint": "fp",
            "items": [
                {"key": "rescue-mode", "value": "12345"},
                {"key": "startup-script", "value": "rescue script"},
                {"key": "rescue-original-disk", "value": "orig"},
                {"key": "rescue-os-type", "value": "linux"},
                {"key": "rescue-backup-startup-script", "value": "user script"},
                {"key": "ssh-keys", "value": "user:pubkey"},
            ],
        }
    }

    compute = FakeCompute([vm_response])
    orch = RestoreOrchestrator(
        compute=compute, project="p", zone="z", vm_name="vm",
        config=RestoreConfig(), logger=None,
    )

    clean = orch._get_clean_metadata()

    keys = {item["key"] for item in clean}

    restored = {item["key"]: item["value"] for item in clean}

    # Rescue keys removed
    assert "rescue-mode" not in keys
    assert "rescue-original-disk" not in keys
    assert "rescue-os-type" not in keys
    assert "rescue-backup-startup-script" not in keys

    # Backup restored (rescue-backup-startup-script -> startup-script with original value)
    assert restored["startup-script"] == "user script"

    # Non-rescue keys preserved
    assert restored["ssh-keys"] == "user:pubkey"

    # Only 2 keys remain
    assert len(clean) == 2


def test_restore_get_disk_info():
    """_get_disk_info() correctly identifies rescue and original disks."""
    vm_response = {
        "disks": [
            {
                "boot": True,
                "source": "projects/p/zones/z/disks/rescue-disk-999",
                "deviceName": "disk-rescue",
            },
            {
                "boot": False,
                "source": "projects/p/zones/z/disks/my-orig-disk",
                "deviceName": "sda",
            },
        ],
        "metadata": {
            "items": [
                {"key": "rescue-original-disk", "value": "my-orig-disk"},
            ]
        },
    }

    compute = FakeCompute([vm_response])
    orch = RestoreOrchestrator(
        compute=compute, project="p", zone="z", vm_name="vm",
        config=RestoreConfig(), logger=None,
    )

    orch._get_disk_info()

    assert orch.rescue_disk_name == "rescue-disk-999"
    assert orch.rescue_device_name == "disk-rescue"
    assert orch.original_disk_name == "my-orig-disk"
    assert orch.original_device_name == "sda"


def test_restore_detach_original_failure_triggers_rollback(stub_restore, monkeypatch):
    """Detach original disk failure triggers rollback."""
    call_count = SimpleNamespace(n=0)

    def _fail_second_detach(self, *args, **kwargs):
        call_count.n += 1
        # First detach = step 2 (rescue disk), second = step 3 (original disk)
        if call_count.n == 2:
            return _failure_execute(self, *args, **kwargs)
        return _success_execute(self, *args, **kwargs)

    monkeypatch.setattr(DetachDiskOperation, "execute", _fail_second_detach)

    orch = _make_orch()
    assert orch.execute() is False
