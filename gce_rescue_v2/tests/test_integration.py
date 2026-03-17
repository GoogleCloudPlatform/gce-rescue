"""
Integration test for full rescue -> restore cycle.

Tests the complete workflow with mocked orchestrators:
1. Rescue VM (validate + execute)
2. Restore VM (validate + execute)
"""

from unittest.mock import Mock

from gce_rescue_v2 import cli


def test_full_rescue_restore_cycle(monkeypatch):
    """Mock end-to-end rescue then restore path via CLI handlers."""
    state = {"calls": [], "rescued": False, "restored": False}

    # Mock AuthManager with dynamic VM state:
    # Before rescue: no rescue-mode metadata
    # After rescue: rescue-mode metadata present (so restore preflight passes)
    mock_auth = Mock()
    mock_compute = Mock()

    def _get_vm_info():
        if state["rescued"]:
            return {
                "disks": [],
                "metadata": {"items": [{"key": "rescue-mode", "value": "true"}]},
            }
        return {"disks": []}

    mock_compute.instances.return_value.get.return_value.execute.side_effect = _get_vm_info
    mock_compute.snapshots.return_value.list.return_value.execute.return_value = {"items": []}
    mock_auth.get_client.return_value = (mock_compute, "test-project")
    monkeypatch.setattr("gce_rescue_v2.core.auth.AuthManager", lambda: mock_auth)

    # Mock gcloud config
    from gce_rescue_v2.cli import preflight
    monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-project")

    class FakeRescueOrchestrator:
        def __init__(self, **kwargs):
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
        def __init__(self, **kwargs):
            self.original_disk_name = 'test-boot-disk'

        def validate(self):
            state["calls"].append("restore-validate")
            return state["rescued"]

        def execute(self):
            state["calls"].append("restore-execute")
            state["restored"] = True
            return True

    monkeypatch.setattr(
        "gce_rescue_v2.cli.rescue.RescueOrchestrator", FakeRescueOrchestrator
    )
    monkeypatch.setattr(
        "gce_rescue_v2.cli.restore.RestoreOrchestrator", FakeRestoreOrchestrator
    )

    parser = cli.create_parser()

    # Rescue
    rescue_args = parser.parse_args([
        "rescue", "vm-1", "--zone", "us-central1-a", "--quiet", "--format", "disable"
    ])
    rescue_code = cli.handle_rescue(rescue_args)

    # Restore
    restore_args = parser.parse_args([
        "restore", "vm-1", "--zone", "us-central1-a", "--quiet", "--format", "disable"
    ])
    restore_code = cli.handle_restore(restore_args)

    assert rescue_code == 0
    assert restore_code == 0
    assert state["calls"] == [
        "rescue-validate",
        "rescue-execute",
        "restore-validate",
        "restore-execute",
    ]
    assert state["rescued"] is True
    assert state["restored"] is True
