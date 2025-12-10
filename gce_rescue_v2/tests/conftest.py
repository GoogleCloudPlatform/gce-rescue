"""
Pytest fixtures for GCE Rescue V2 tests.

Provides:
- mock_compute: Mocked GCP compute client
- mock_vm_response: Sample VM API response
- sample_rescue_config: RescueConfig with test values
"""

import sys
from pathlib import Path

# Add parent directory to path so imports work both when:
# 1. Running pytest from gce_rescue_v2/tests/ directory
# 2. Running pytest from repo root after pip install -e .
parent_dir = Path(__file__).parent.parent
if str(parent_dir) not in sys.path:
    sys.path.insert(0, str(parent_dir))

import pytest
from unittest.mock import Mock, MagicMock
from core.config import RescueConfig

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