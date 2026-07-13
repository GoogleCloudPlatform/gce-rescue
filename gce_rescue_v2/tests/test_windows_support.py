"""
Unit tests for Windows VM support.

Tests:
1. OS detection for Windows VMs
2. OS detection for Linux VMs
3. Correct startup script selection
4. Correct rescue image selection
"""

from pathlib import Path
from unittest.mock import Mock, patch
import pytest

from gce_rescue_v2.core.config import OS_TYPE_LINUX, OS_TYPE_WINDOWS, RescueConfig
from gce_rescue_v2.utils.os_detection import detect_os_type, get_os_display_name

_WINDOWS_MOUNT_SCRIPT = (
    Path(__file__).parent.parent / 'startup_scripts' / 'rescue_mount_windows.ps1'
)


class TestOSDetection:
    """Tests for OS detection functionality."""

    def test_detect_linux_default(self):
        """Linux should be default when no Windows indicators present."""
        vm = {
            'disks': [
                {'boot': True, 'source': '/projects/p/zones/z/disks/my-disk'}
            ]
        }
        assert detect_os_type(vm) == OS_TYPE_LINUX

    def test_detect_windows_guest_os_features(self):
        """Windows should be detected via guestOsFeatures."""
        vm = {
            'disks': [
                {
                    'boot': True,
                    'source': '/projects/p/zones/z/disks/windows-disk',
                    'guestOsFeatures': [
                        {'type': 'UEFI_COMPATIBLE'},
                        {'type': 'WINDOWS'}
                    ]
                }
            ]
        }
        assert detect_os_type(vm) == OS_TYPE_WINDOWS

    def test_detect_windows_license(self):
        """Windows should be detected via license URL."""
        vm = {
            'disks': [
                {
                    'boot': True,
                    'source': '/projects/p/zones/z/disks/windows-disk',
                    'licenses': [
                        'https://compute.googleapis.com/compute/v1/projects/windows-cloud/global/licenses/windows-server-2022'
                    ]
                }
            ]
        }
        assert detect_os_type(vm) == OS_TYPE_WINDOWS

    def test_detect_windows_source_image(self):
        """Windows should be detected via source image name."""
        vm = {
            'disks': [
                {
                    'boot': True,
                    'source': '/projects/windows-cloud/zones/z/disks/windows-server-2022'
                }
            ]
        }
        assert detect_os_type(vm) == OS_TYPE_WINDOWS

    def test_get_os_display_name(self):
        """Display names should be human-readable."""
        assert get_os_display_name(OS_TYPE_WINDOWS) == "Windows"
        assert get_os_display_name(OS_TYPE_LINUX) == "Linux"


class TestRescueOrchestratorWindows:
    """Tests for Windows handling in rescue orchestrator."""

    def test_windows_uses_powershell_script_key(self):
        """Windows VMs should use windows-startup-script-ps1 metadata key."""
        from gce_rescue_v2.orchestration.rescue import RescueOrchestrator

        mock_compute = Mock()
        mock_compute.instances().get().execute.return_value = {
            'disks': [
                {
                    'boot': True,
                    'source': '/projects/p/zones/z/disks/win-disk',
                    'deviceName': 'win-disk',
                    'guestOsFeatures': [{'type': 'WINDOWS'}]
                }
            ]
        }

        orchestrator = RescueOrchestrator(
            compute=mock_compute,
            project='test-project',
            zone='us-central1-a',
            vm_name='windows-vm',
            config=RescueConfig(),
            logger=None
        )

        orchestrator._get_original_disk_info()
        assert orchestrator.os_type == OS_TYPE_WINDOWS

    def test_linux_uses_startup_script_key(self):
        """Linux VMs should use startup-script metadata key."""
        from gce_rescue_v2.orchestration.rescue import RescueOrchestrator

        mock_compute = Mock()
        mock_compute.instances().get().execute.return_value = {
            'disks': [
                {
                    'boot': True,
                    'source': '/projects/p/zones/z/disks/linux-disk',
                    'deviceName': 'linux-disk'
                }
            ]
        }

        orchestrator = RescueOrchestrator(
            compute=mock_compute,
            project='test-project',
            zone='us-central1-a',
            vm_name='linux-vm',
            config=RescueConfig(),
            logger=None
        )

        orchestrator._get_original_disk_info()
        assert orchestrator.os_type == OS_TYPE_LINUX


class TestRestoreOrchestratorWindows:
    """Tests for Windows handling in restore orchestrator."""

    def test_clean_metadata_removes_windows_keys(self):
        """Restore should clean both Linux and Windows startup script keys."""
        from gce_rescue_v2.orchestration.restore import RestoreOrchestrator

        mock_compute = Mock()
        mock_compute.instances().get().execute.return_value = {
            'metadata': {
                'items': [
                    {'key': 'rescue-mode', 'value': '123456'},
                    {'key': 'windows-startup-script-ps1', 'value': '# script'},
                    {'key': 'startup-script', 'value': '#!/bin/bash'},
                    {'key': 'rescue-original-disk', 'value': 'my-disk'},
                    {'key': 'rescue-os-type', 'value': 'windows'},
                    {'key': 'user-data', 'value': 'keep-this'}
                ]
            },
            'disks': []
        }

        orchestrator = RestoreOrchestrator(
            compute=mock_compute,
            project='test-project',
            zone='us-central1-a',
            vm_name='test-vm',
            logger=None
        )

        clean_items = orchestrator._get_clean_metadata()

        # Should only keep non-rescue metadata
        keys = [item['key'] for item in clean_items]
        assert 'user-data' in keys
        assert 'rescue-mode' not in keys
        assert 'windows-startup-script-ps1' not in keys
        assert 'startup-script' not in keys
        assert 'rescue-original-disk' not in keys
        assert 'rescue-os-type' not in keys


class TestConfigWindows:
    """Tests for Windows configuration settings."""

    def test_config_has_windows_settings(self):
        """RescueConfig should have Windows-specific settings."""
        config = RescueConfig()

        assert hasattr(config, 'windows_rescue_image_family')
        assert hasattr(config, 'windows_rescue_image_project')
        assert hasattr(config, 'windows_rescue_disk_size_gb')

        # Windows disk should be larger than Linux
        assert config.windows_rescue_disk_size_gb > config.rescue_disk_size_gb

    def test_windows_image_settings(self):
        """Windows image settings should be correct."""
        config = RescueConfig()

        assert config.windows_rescue_image_family == 'windows-2022'
        assert config.windows_rescue_image_project == 'windows-cloud'
        assert config.windows_rescue_disk_size_gb == 50


class TestWindowsBcdRealignment:
    """The mount script must repair the #126 GUID-collision damage via BCD
    realignment, not GUID restoration.

    The default Windows rescue image (windows-2022) shares a GPT disk GUID
    with same-family affected disks; onlining the affected disk makes Windows
    regenerate its GUID, invalidating the disk's BCD (unbootable at
    0xc000000e/0xc0000225 after restore). Restoring the old GUID does NOT
    work (live-tested: the rescue disk with the same GUID is still online, so
    the collision immediately recurs). Instead the script detects the GUID
    change and rebuilds the disk's BCD against its new, stable identity with
    bcdboot - and only when a change was actually detected.
    """

    def _script(self) -> str:
        return _WINDOWS_MOUNT_SCRIPT.read_text(encoding='utf-8')

    def test_guid_captured_before_online(self):
        text = self._script()
        # The pre-online capture must precede the Set-Disk online call.
        capture = text.index('$originalGuid = (Get-Disk')
        online = text.index('Set-Disk -Number $disk.Number -IsOffline $false')
        assert capture < online, "GUID must be captured before the disk is onlined"

    def test_guid_never_restored(self):
        # The failed first approach: writing the old GUID back recreates the
        # live collision (rescue disk still online) and Windows re-resolves
        # it later. The script must not attempt it.
        assert '-Guid $originalGuid' not in self._script()

    def test_change_detection_feeds_realignment(self):
        text = self._script()
        assert 'if ($currentGuid -ne $originalGuid)' in text
        assert '$guidChangedDisks += $disk.Number' in text

    def test_bcdboot_realignment_after_mounting(self):
        text = self._script()
        # bcdboot runs in the realignment section, after the partition
        # mounting loop (it needs the offline \Windows drive letter).
        assert 'bcdboot' in text
        assert '/f UEFI' in text
        assert '/f BIOS' in text
        mount_loop = text.index('Assigning drive letter')
        realign = text.index('BCD Realignment')
        assert mount_loop < realign

    def test_realignment_only_for_changed_disks(self):
        # A collision-free rescue must never touch the BCD.
        assert 'foreach ($diskNumber in $guidChangedDisks)' in self._script()

    def test_locates_windows_by_config_hive(self):
        # Finds the offline install by its registry hive, not a hardcoded D:.
        assert 'Windows\\System32\\config\\SYSTEM' in self._script()

    def test_releases_temp_esp_letter(self):
        assert 'Remove-PartitionAccessPath' in self._script()

    def test_references_issue_126(self):
        assert '#126' in self._script()
