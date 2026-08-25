"""
Pytest fixtures for GCE Rescue V2 tests.

Provides:
- mock_compute: Mocked GCP compute client
- mock_vm_response: Sample VM API response
- sample_rescue_config: RescueConfig with test values
"""

import os
import pytest
from unittest.mock import Mock, MagicMock
from gce_rescue_v2.core.config import RescueConfig

@pytest.fixture(autouse=True, scope='session')
def change_to_tmpdir(tmp_path_factory):
    """Run the suite from a temp directory so CLI log files never land in the repo.

    Uses TEST_TMPDIR under the Bazel sandbox, otherwise a pytest-managed temp dir.
    """
    target = os.environ.get('TEST_TMPDIR') or str(tmp_path_factory.mktemp('cwd'))
    original = os.getcwd()
    os.chdir(target)
    yield
    os.chdir(original)

@pytest.fixture
def mock_compute():
    """Mocked GCP compute client."""
    mock = MagicMock()
    # Mock resources
    mock.instances.return_value = MagicMock()
    mock.disks.return_value = MagicMock()
    mock.zoneOperations.return_value = MagicMock()
    mock.snapshots.return_value = MagicMock()
    return mock

@pytest.fixture
def mock_vm_response():
    """Sample VM API response."""
    return {
        'name': 'test-vm',
        'status': 'RUNNING',
        'disks': [
            {
                'source': 'projects/test-project/zones/us-central1-a/disks/boot-disk',
                'boot': True,
                'deviceName': 'boot-disk',
                'autoDelete': True,
                'mode': 'READ_WRITE'
            }
        ],
        'metadata': {
            'fingerprint': 'test-fingerprint',
            'items': [{'key': 'test', 'value': 'value'}]
        }
    }

@pytest.fixture
def sample_rescue_config():
    """Sample RescueConfig for testing."""
    return RescueConfig(
        rescue_disk_size_gb=10,
        create_snapshot=False,  # Faster tests
        async_snapshot=False,
        vm_stop_timeout=1,
        vm_start_timeout=1,
        disk_create_timeout=1
    )