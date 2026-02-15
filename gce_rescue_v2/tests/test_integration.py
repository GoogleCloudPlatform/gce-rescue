"""
Integration test for full rescue → restore cycle.

Tests the complete workflow with mocked orchestrators:
1. Rescue VM (validate + execute)
2. Restore VM (validate + execute)
"""

from unittest.mock import Mock

from gce_rescue_v2 import main


def test_full_rescue_restore_cycle(monkeypatch):
    """Mock end-to-end rescue then restore path."""
    state = {"calls": [], "rescued": False, "restored": False}

    def fake_get_client(self, project=None):
        return Mock(), project or "default-project"

    class FakeRescueOrchestrator:
        def __init__(self, compute, project, zone, vm_name, config, logger, log_file=None):
            self.compute = compute
            self.project = project
            self.zone = zone
            self.vm_name = vm_name
            self.log_file = log_file
            # Required attributes for main.py success message
            self.os_type = 'linux'
            self.windows_rescue_password = None
            self.verification_succeeded = True
            self.snapshot_name = 'pre-rescue-test-disk-1234567890'
            self.original_disk_name = 'test-boot-disk'
            self.rescue_disk_name = 'rescue-disk-1234567890'

        def validate(self):
            state["calls"].append("rescue-validate")
            return True

        def execute(self):
            state["calls"].append("rescue-execute")
            state["rescued"] = True
            return True

    class FakeRestoreOrchestrator:
        def __init__(self, compute, project, zone, vm_name, config, logger, log_file=None):
            self.compute = compute
            self.project = project
            self.zone = zone
            self.vm_name = vm_name
            self.log_file = log_file
            # Required attributes for main.py success message
            self.original_disk_name = 'test-boot-disk'

        def validate(self):
            state["calls"].append("restore-validate")
            return state["rescued"]

        def execute(self):
            state["calls"].append("restore-execute")
            state["restored"] = True
            return True

    monkeypatch.setattr(main.AuthManager, "get_client", fake_get_client, raising=False)
    monkeypatch.setattr(main, "RescueOrchestrator", FakeRescueOrchestrator)
    monkeypatch.setattr(main, "RestoreOrchestrator", FakeRestoreOrchestrator)

    rescue_ok = main.rescue_vm("vm-1", "us-central1-a", project="test-project")
    restore_ok = main.restore_vm("vm-1", "us-central1-a", project="test-project")

    assert rescue_ok is True
    assert restore_ok is True
    assert state["calls"] == [
        "rescue-validate",
        "rescue-execute",
        "restore-validate",
        "restore-execute",
    ]
    assert state["rescued"] is True
    assert state["restored"] is True

