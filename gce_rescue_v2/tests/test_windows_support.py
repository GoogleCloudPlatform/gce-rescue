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


class TestWindowsGuidCollisionPrevention:
    """Issue #126 is PREVENTED, not repaired: the orchestrator rescues with a
    different image family than the target's, so the GPT disk GUID collision
    (which makes Windows regenerate the target's GUID and invalidate its BCD)
    never happens. The mount script keeps GUID-change detection as a tripwire
    and warns loudly - it must never rewrite the BCD automatically (unsafe on
    BitLocker disks; drops custom BCD entries)."""

    def _script(self) -> str:
        return _WINDOWS_MOUNT_SCRIPT.read_text(encoding='utf-8')

    def _orchestrator(self):
        from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
        orch = RescueOrchestrator(
            compute=Mock(), project='p', zone='z', vm_name='vm',
            config=RescueConfig(), logger=None
        )
        orch.original_disk_name = 'win-disk'
        return orch

    def _tracked(self, source_image, family):
        tracked = Mock()
        tracked.disks().get().execute.return_value = {'sourceImage': source_image}
        tracked.images().get().execute.return_value = {'family': family}
        return tracked

    # --- orchestrator: family auto-selection (the prevention) ---

    def test_same_family_target_selects_alternate(self):
        orch = self._orchestrator()
        tracked = self._tracked(
            'projects/windows-cloud/global/images/windows-server-2022-dc-v1',
            'windows-2022'
        )
        with patch.object(orch, '_create_tracked_compute', return_value=tracked):
            assert orch._select_windows_rescue_family() == 'windows-2019'

    def test_different_family_target_keeps_configured(self):
        orch = self._orchestrator()
        tracked = self._tracked(
            'projects/windows-cloud/global/images/windows-server-2019-dc-v1',
            'windows-2019'
        )
        with patch.object(orch, '_create_tracked_compute', return_value=tracked):
            assert orch._select_windows_rescue_family() == 'windows-2022'

    def test_2019_configured_alternates_to_2022(self):
        from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
        assert RescueOrchestrator._alternate_windows_family('windows-2019') == 'windows-2022'
        assert RescueOrchestrator._alternate_windows_family('windows-2022') == 'windows-2019'
        assert RescueOrchestrator._alternate_windows_family('windows-2025') == 'windows-2019'

    def test_custom_image_without_family_keeps_configured(self):
        orch = self._orchestrator()
        tracked = self._tracked(
            'projects/my-proj/global/images/golden-win', ''
        )
        with patch.object(orch, '_create_tracked_compute', return_value=tracked):
            assert orch._select_windows_rescue_family() == 'windows-2022'

    def test_lookup_failure_keeps_configured(self):
        orch = self._orchestrator()
        tracked = Mock()
        tracked.disks().get().execute.side_effect = Exception('api down')
        with patch.object(orch, '_create_tracked_compute', return_value=tracked):
            assert orch._select_windows_rescue_family() == 'windows-2022'

    # --- mount script: detection stays, automatic repair is gone ---

    def test_guid_captured_before_online(self):
        text = self._script()
        # The pre-online capture must precede the Set-Disk online call.
        capture = text.index('$originalGuid = (Get-Disk')
        online = text.index('Set-Disk -Number $disk.Number -IsOffline $false')
        assert capture < online, "GUID must be captured before the disk is onlined"

    def test_guid_never_restored(self):
        # Writing the old GUID back recreates the live collision (rescue disk
        # still online) and Windows re-resolves it later (live-tested).
        assert '-Guid $originalGuid' not in self._script()

    def test_change_detection_feeds_warning(self):
        text = self._script()
        assert 'if ($currentGuid -ne $originalGuid)' in text
        assert '$guidChangedDisks += $disk.Number' in text
        assert 'foreach ($diskNumber in $guidChangedDisks)' in text

    def test_no_automatic_bcd_rewrite(self):
        # Reviewer-agreed (#149): never rewrite the BCD automatically - it can
        # push BitLocker disks into recovery and drops custom BCD entries.
        text = self._script()
        assert 'BCD Realignment' not in text
        assert '/f UEFI' not in text
        assert 'Remove-PartitionAccessPath' not in text  # no temp ESP mounting

    def test_warning_names_the_consequence_and_remediation(self):
        text = self._script()
        assert 'may NOT BOOT after restore' in text
        assert 'windows_bcd_fix.ps1' in text
        assert 'BitLocker' in text

    def test_references_issue_126(self):
        assert '#126' in self._script()
