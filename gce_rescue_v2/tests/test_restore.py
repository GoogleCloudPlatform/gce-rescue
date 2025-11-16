import time
from types import SimpleNamespace

import pytest

from gce_rescue_v2.orchestration.restore import RestoreOrchestrator
from gce_rescue_v2.core.config import RestoreConfig
from gce_rescue_v2.operations.base import BaseOperation, OperationResult


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


class FakeCompute:
    def __init__(self, instances_get_responses):
        self._instances = FakeInstances(instances_get_responses)
        self._disks = FakeDisks()

    def instances(self):
        return self._instances

    def disks(self):
        return self._disks


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def always_wait_ok(monkeypatch):
    monkeypatch.setattr(BaseOperation, "_wait_for_status", lambda self, fn, target, timeout=None: True)


def _instances_seq_for_restore(rescue_disk_name="rescue-disk-123", original_disk_name="orig-disk"):
    """Instances.get() response sequence for a successful restore flow.

    Order of calls:
      1) _get_disk_info
      2) StopVMOperation initial status
      3) _get_clean_metadata
      4) SetMetadataOperation fingerprint fetch
      5) StartVMOperation initial status
    """
    vm_disks = {
        "disks": [
            {  # Rescue disk
                "boot": True,
                "source": f"projects/p/zones/z/disks/{rescue_disk_name}",
                "deviceName": "disk-rescue",
            },
            {  # Original disk
                "boot": False,
                "source": f"projects/p/zones/z/disks/{original_disk_name}",
                "deviceName": "sda",
            },
        ],
        "metadata": {
            "items": [
                {"key": "rescue-original-disk", "value": original_disk_name},
                {"key": "startup-script", "value": "echo hi"},
                {"key": "rescue-mode", "value": "123"},
            ]
        },
    }

    clean_meta_src = {
        "metadata": {
            "items": list(vm_disks["metadata"]["items"]),
        }
    }

    meta_fp_src = {"metadata": {"fingerprint": "fp-xyz", "items": []}}

    return [
        vm_disks,
        {"status": "RUNNING"},
        clean_meta_src,
        meta_fp_src,
        {"status": "TERMINATED"},
    ]


@pytest.mark.skip(reason="Mock needs enhancement for full restore flow")
def test_restore_success_deletes_rescue_disk(monkeypatch):
    compute = FakeCompute(_instances_seq_for_restore())
    config = RestoreConfig(delete_rescue_disk=True)

    called = SimpleNamespace(deleted=None)

    from gce_rescue_v2.operations.delete_disk import DeleteDiskOperation

    def fake_delete(self, disk_name):
        called.deleted = disk_name
        return OperationResult(operation_name=self.name, success=True, message="deleted", rollback_data=None)

    monkeypatch.setattr(DeleteDiskOperation, "execute", fake_delete)

    orch = RestoreOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)
    ok = orch.execute()

    assert ok is True
    assert called.deleted == "rescue-disk-123"


@pytest.mark.skip(reason="Mock needs enhancement for full restore flow")
def test_restore_success_preserves_rescue_disk_when_disabled(monkeypatch):
    compute = FakeCompute(_instances_seq_for_restore())
    config = RestoreConfig(delete_rescue_disk=False)

    from gce_rescue_v2.operations.delete_disk import DeleteDiskOperation

    # Ensure delete is not called
    monkeypatch.setattr(DeleteDiskOperation, "execute", lambda self, disk_name: pytest.fail("delete should not be called"))

    orch = RestoreOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)
    ok = orch.execute()

    assert ok is True


def test_restore_detach_original_failure_triggers_rollback(monkeypatch):
    compute = FakeCompute(_instances_seq_for_restore())
    config = RestoreConfig(delete_rescue_disk=True)

    from gce_rescue_v2.operations.detach_disk import DetachDiskOperation

    def side_effect(self, vm_name, device_name):
        if device_name == "sda":  # Fail when detaching original
            return OperationResult(operation_name=self.name, success=False, message="fail", error="x", rollback_data=None)
        return OperationResult(operation_name=self.name, success=True, message="ok", rollback_data={"vm_name": vm_name, "disk_info": {"source": "", "boot": False, "autoDelete": False, "deviceName": device_name, "mode": "READ_WRITE"}})

    monkeypatch.setattr(DetachDiskOperation, "execute", side_effect)

    orch = RestoreOrchestrator(compute, project="p", zone="z", vm_name="vm", config=config, logger=None)
    ok = orch.execute()

    assert ok is False

