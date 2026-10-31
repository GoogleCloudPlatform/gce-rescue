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


# ===================================================================
# Checkpoint cleared after rollback
# ===================================================================


def test_restore_rollback_clears_checkpoint(stub_restore, monkeypatch):
    """Rollback after failure clears the checkpoint metadata."""
    from unittest.mock import MagicMock
    from gce_rescue_v2.orchestration.rollback import RollbackHandler

    # Let _rollback run (undo the no-op stub from fixture)
    monkeypatch.undo()

    # Re-apply all stubs except _rollback
    ALL_RESTORE_OPS = [
        StopVMOperation, DetachDiskOperation, AttachDiskOperation,
        SetMetadataOperation, StartVMOperation, DeleteDiskOperation,
    ]
    for op_cls in ALL_RESTORE_OPS:
        monkeypatch.setattr(op_cls, "execute", _success_execute)
    monkeypatch.setattr(CheckpointManager, "create_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(CheckpointManager, "update_checkpoint", lambda *a, **kw: None)
    monkeypatch.setattr(RestoreOrchestrator, "_is_disk_attached", lambda self, n: True)
    monkeypatch.setattr(RestoreOrchestrator, "_is_disk_boot", lambda self, n: False)
    monkeypatch.setattr(RestoreOrchestrator, "_get_vm_status", lambda self: "TERMINATED")
    monkeypatch.setattr(RestoreOrchestrator, "_disk_exists", lambda self, n: True)
    monkeypatch.setattr(RestoreOrchestrator, "_check_snapshot_status", lambda self: None)

    def _fake_get_disk_info(self):
        self.rescue_disk_name = "rescue-disk-123"
        self.rescue_device_name = "disk-rescue"
        self.original_disk_name = "orig-disk"
        self.original_device_name = "sda"

    monkeypatch.setattr(RestoreOrchestrator, "_get_disk_info", _fake_get_disk_info)
    monkeypatch.setattr(
        RestoreOrchestrator, "_get_clean_metadata",
        lambda self: [{"key": "ssh-keys", "value": "user:key"}],
    )

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


# ===================================================================
# Safety snapshot lookup (issue #85)
# ===================================================================


class _RecordingSnapshots:
    """snapshots() fake that records the filter and returns canned items."""

    def __init__(self, items):
        self.items = items
        self.filters = []

    def list(self, project=None, filter=None):
        self.filters.append(filter)
        return _Exec({'items': self.items})


def _make_snapshot_orch(items, vm_name="my-vm", disk_name="my-vm-boot"):
    from unittest.mock import MagicMock

    compute = FakeCompute()
    compute._snapshots = _RecordingSnapshots(items)
    logger = MagicMock()
    orch = RestoreOrchestrator(
        compute=compute, project="p", zone="z", vm_name=vm_name,
        config=RestoreConfig(), logger=logger, suppress_progress=True,
    )
    orch.original_disk_name = disk_name
    return orch, compute._snapshots, logger


def _info_messages(logger):
    return [str(c.args[0]) for c in logger.info.call_args_list if c.args]


def test_snapshot_check_filters_by_original_disk_name():
    """The snapshot filter uses the original disk name, never the VM name."""
    orch, snaps, _ = _make_snapshot_orch(
        [{'name': 'pre-rescue-my-vm-boot-123', 'status': 'READY',
          'creationTimestamp': '2026-01-01T00:00:00Z'}]
    )

    orch._check_snapshot_status()

    assert snaps.filters == ['name:pre-rescue-my-vm-boot-*']
    assert all('pre-rescue-my-vm-*' not in f for f in snaps.filters)


def test_snapshot_check_ready_snapshot_no_warning():
    """A READY snapshot named after the disk is found; no warning is printed."""
    orch, _, logger = _make_snapshot_orch(
        [{'name': 'pre-rescue-my-vm-boot-123', 'status': 'READY',
          'creationTimestamp': '2026-01-01T00:00:00Z'}]
    )

    orch._check_snapshot_status()

    assert not any('No safety snapshot' in m for m in _info_messages(logger))
    debug_messages = [str(c.args[0]) for c in logger.debug.call_args_list if c.args]
    assert any('Safety snapshot verified: pre-rescue-my-vm-boot-123' in m
               for m in debug_messages)


def test_snapshot_check_warns_when_none_found():
    """With no matching snapshot the existing warning is still shown."""
    orch, _, logger = _make_snapshot_orch([])

    orch._check_snapshot_status()

    assert any('No safety snapshot' in m for m in _info_messages(logger))


def test_snapshot_check_skipped_without_disk_name():
    """No disk name means nothing to look up; no API call, no warning."""
    orch, snaps, logger = _make_snapshot_orch([], disk_name=None)

    orch._check_snapshot_status()

    assert snaps.filters == []
    assert not any('No safety snapshot' in m for m in _info_messages(logger))


# ===================================================================
# enable-guest-attributes round trip (#146)
# ===================================================================

_RESCUE_ITEMS = [
    {"key": "startup-script", "value": "rescue script"},
    {"key": "rescue-mode", "value": "12345"},
    {"key": "rescue-original-disk", "value": "orig-disk"},
    {"key": "rescue-os-type", "value": "linux"},
    {"key": "enable-guest-attributes", "value": "TRUE"},
]


def _metadata_after_rescue(pre_rescue_items):
    """Metadata as SetMetadataOperation writes it during rescue."""
    op = SetMetadataOperation(FakeCompute(), "p", "z", None)
    return op._merge_with_backup(pre_rescue_items, _RESCUE_ITEMS)


def _clean_metadata(items):
    compute = FakeCompute([{"metadata": {"fingerprint": "fp", "items": items}}])
    orch = RestoreOrchestrator(
        compute=compute, project="p", zone="z", vm_name="vm",
        config=RestoreConfig(), logger=None,
    )
    return {item["key"]: item["value"] for item in orch._get_clean_metadata()}


def test_guest_attributes_absent_before_rescue_absent_after_restore():
    """Rescue adds the key; restore removes it again."""
    during = _metadata_after_rescue([{"key": "ssh-keys", "value": "user:key"}])
    during_map = {i["key"]: i["value"] for i in during}
    assert during_map["enable-guest-attributes"] == "TRUE"
    assert during_map["rescue-backup-absent-keys"] == "enable-guest-attributes"

    restored = _clean_metadata(during)

    assert restored == {"ssh-keys": "user:key"}


def test_guest_attributes_false_before_rescue_false_after_restore():
    """A user-set FALSE is backed up during rescue and put back on restore."""
    during = _metadata_after_rescue([
        {"key": "enable-guest-attributes", "value": "FALSE"},
        {"key": "ssh-keys", "value": "user:key"},
    ])
    during_map = {i["key"]: i["value"] for i in during}
    assert during_map["enable-guest-attributes"] == "TRUE"
    assert during_map["rescue-backup-enable-guest-attributes"] == "FALSE"
    assert "rescue-backup-absent-keys" not in during_map

    restored = _clean_metadata(during)

    assert restored == {"enable-guest-attributes": "FALSE", "ssh-keys": "user:key"}


def test_guest_attributes_true_before_rescue_kept_after_restore():
    """A user-set TRUE stays TRUE."""
    during = _metadata_after_rescue([{"key": "enable-guest-attributes", "value": "TRUE"}])

    restored = _clean_metadata(during)

    assert restored == {"enable-guest-attributes": "TRUE"}


def test_guest_attributes_no_record_left_unchanged():
    """VM rescued by an older version (no record): keep today's behaviour."""
    items = [
        {"key": "rescue-mode", "value": "12345"},
        {"key": "startup-script", "value": "rescue script"},
        {"key": "rescue-original-disk", "value": "orig-disk"},
        {"key": "enable-guest-attributes", "value": "TRUE"},
    ]

    restored = _clean_metadata(items)

    assert restored == {"enable-guest-attributes": "TRUE"}


def test_guest_attributes_record_survives_resumed_rescue():
    """Re-running the metadata step must not record rescue's own TRUE as the original."""
    first = _metadata_after_rescue([])
    second = _metadata_after_rescue(first)
    second_map = {i["key"]: i["value"] for i in second}
    assert "rescue-backup-enable-guest-attributes" not in second_map
    assert second_map["rescue-backup-absent-keys"] == "enable-guest-attributes"

    first_false = _metadata_after_rescue([{"key": "enable-guest-attributes", "value": "FALSE"}])
    second_false = _metadata_after_rescue(first_false)
    assert _clean_metadata(second_false)["enable-guest-attributes"] == "FALSE"


# ===================================================================
# Original boot attachment on restore (#170)
# ===================================================================

_REAL_GET_DISK_INFO = RestoreOrchestrator._get_disk_info

_ATTACHMENT_RECORD = (
    '{"autoDelete":true,"deviceName":"persistent-disk-0",'
    '"interface":"NVME","mode":"READ_WRITE"}'
)


def _rescued_vm(with_record=True):
    """VM in rescue mode: original disk is secondary with rescue's settings."""
    items = [{"key": "rescue-original-disk", "value": "my-orig-disk"}]
    if with_record:
        items.append({"key": "rescue-backup-boot-disk-attachment", "value": _ATTACHMENT_RECORD})
    return {
        "disks": [
            {"boot": True, "source": "projects/p/zones/z/disks/rescue-disk-999",
             "deviceName": "rescue-disk-999", "autoDelete": False},
            {"boot": False, "source": "projects/p/zones/z/disks/my-orig-disk",
             "deviceName": "my-orig-disk", "autoDelete": False},
        ],
        "metadata": {"items": items},
    }


def _capture_attach(monkeypatch):
    calls = []

    def _attach(self, *args, **kwargs):
        calls.append(kwargs)
        return OperationResult(
            operation_name=self.name, success=True, message="OK",
            rollback_data={"vm_name": "vm",
                           "device_name": kwargs.get("device_name") or kwargs.get("disk_name")},
        )

    monkeypatch.setattr(AttachDiskOperation, "execute", _attach)
    return calls


def test_restore_get_disk_info_reads_boot_attachment():
    """_get_disk_info loads the record written at rescue time."""
    orch = RestoreOrchestrator(
        compute=FakeCompute([_rescued_vm()]), project="p", zone="z", vm_name="vm",
        config=RestoreConfig(), logger=None,
    )

    orch._get_disk_info()

    assert orch.original_device_name == "my-orig-disk"  # current secondary
    assert orch.original_boot_attachment == {
        "deviceName": "persistent-disk-0", "autoDelete": True,
        "mode": "READ_WRITE", "interface": "NVME",
    }
    assert orch._get_checkpoint_context()["original_boot_attachment"] == orch.original_boot_attachment


def test_restore_reattaches_boot_with_original_settings(stub_restore, monkeypatch):
    """The boot re-attach uses the pre-rescue values, not the secondary attachment."""
    # Use the real disk-info lookup so the record is read from VM metadata.
    monkeypatch.setattr(RestoreOrchestrator, "_get_disk_info", _REAL_GET_DISK_INFO)
    calls = _capture_attach(monkeypatch)

    orch = RestoreOrchestrator(
        compute=FakeCompute([_rescued_vm(), {"disks": []}]), project="p", zone="z",
        vm_name="vm", config=RestoreConfig(delete_rescue_disk=False), logger=None,
        suppress_progress=True,
    )
    assert orch.execute() is True

    assert len(calls) == 1
    assert calls[0]["boot"] is True
    assert calls[0]["disk_name"] == "my-orig-disk"
    assert calls[0]["device_name"] == "persistent-disk-0"
    assert calls[0]["auto_delete"] is True
    assert calls[0]["read_only"] is False
    assert calls[0]["interface"] == "NVME"


def test_restore_without_record_uses_default_attach(stub_restore, monkeypatch):
    """VM rescued by an older version: attach exactly as before (no extra args)."""
    calls = _capture_attach(monkeypatch)

    orch = _make_orch()
    assert orch.execute() is True

    assert calls[0]["boot"] is True
    for key in ("device_name", "auto_delete", "read_only", "interface"):
        assert key not in calls[0]


def test_restore_boot_attach_kwargs_from_resumed_checkpoint(stub_restore):
    """A resumed restore takes the attachment from the checkpoint context."""
    from gce_rescue_v2.orchestration.checkpoint import CheckpointData

    checkpoint = CheckpointData(
        version=1, operation="restore", session_id="abcd1234",
        started_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:01:00Z",
        current_step=3, total_steps=7, completed_operations=[],
        context={
            "rescue_disk_name": "rescue-disk-1", "original_disk_name": "my-orig-disk",
            "original_boot_attachment": {"deviceName": "persistent-disk-0", "autoDelete": True},
        },
    )
    orch = _make_orch()
    orch.set_resume_state(checkpoint)

    assert orch._boot_attach_kwargs() == {"device_name": "persistent-disk-0", "auto_delete": True}


def test_restore_resume_without_context_reads_record_from_metadata():
    """Older restore checkpoint without the attachment: read it from metadata."""
    orch = RestoreOrchestrator(
        compute=FakeCompute([_rescued_vm()]), project="p", zone="z", vm_name="vm",
        config=RestoreConfig(), logger=None,
    )

    orch._load_boot_attachment()

    assert orch._boot_attach_kwargs()["device_name"] == "persistent-disk-0"


def test_restore_attach_rollback_detaches_original_device_name(monkeypatch):
    """Rolling back the boot re-attach detaches persistent-disk-0, the name used."""
    detached = []

    class _Instances(FakeInstances):
        def detachDisk(self, project=None, zone=None, instance=None, deviceName=None):
            detached.append(deviceName)
            return _Exec({})

    compute = FakeCompute()
    compute._instances = _Instances([])
    op = AttachDiskOperation(compute, "p", "z", None)
    monkeypatch.setattr(AttachDiskOperation, "_wait_for_operation", lambda self, o, timeout=300: True)

    result = op.execute(vm_name="vm", disk_name="my-orig-disk", boot=True,
                        device_name="persistent-disk-0", auto_delete=True)
    assert op.rollback(result.rollback_data) is True

    assert detached == ["persistent-disk-0"]


def test_restore_clean_metadata_drops_attachment_record():
    """The bookkeeping record must not come back as a 'boot-disk-attachment' key."""
    restored = _clean_metadata([
        {"key": "rescue-mode", "value": "1"},
        {"key": "rescue-backup-boot-disk-attachment", "value": _ATTACHMENT_RECORD},
        {"key": "ssh-keys", "value": "user:key"},
    ])

    assert restored == {"ssh-keys": "user:key"}
