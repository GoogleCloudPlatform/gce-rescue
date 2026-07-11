"""
Unit tests for Windows VM support.

Tests:
1. OS detection for Windows VMs
2. OS detection for Linux VMs
3. Correct startup script selection
4. Correct rescue image selection
"""

import re
from pathlib import Path
from unittest.mock import Mock, patch
import pytest

from gce_rescue_v2.core.config import OS_TYPE_LINUX, OS_TYPE_WINDOWS, RescueConfig
from gce_rescue_v2.utils.os_detection import detect_os_type, get_os_display_name

_BCD_FIX_SCRIPT = (
    Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'windows_bcd_fix.ps1'
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


class TestWindowsBcdFixScript:
    """The canned BCD-repair fix script (--fix-script) must honor the repair
    marker protocol and rebuild the BCD from the offline install via bcdboot."""

    def _script(self) -> str:
        return _BCD_FIX_SCRIPT.read_text(encoding='utf-8')

    def test_script_exists(self):
        assert _BCD_FIX_SCRIPT.exists()

    def test_emits_result_marker(self):
        assert 'GCE-REPAIR-RESULT:' in self._script()

    def test_single_result_emission_block(self):
        """SUCCESS / NO_ISSUES / FAILED are mutually exclusive branches at the
        end - exactly one result marker fires per run."""
        text = self._script()
        assert 'GCE-REPAIR-RESULT:SUCCESS:$Fixes' in text
        assert 'GCE-REPAIR-RESULT:NO_ISSUES:0' in text
        assert 'GCE-REPAIR-RESULT:FAILED:$FixReason' in text
        # All result emissions live after the single-emission-point banner.
        marker = text.index('Single result-emission point')
        for m in re.finditer(r'GCE-REPAIR-RESULT:', text):
            assert m.start() > marker

    def test_never_calls_exit(self):
        # 'exit' would abort the composed startup script before the completion
        # marker. Ignore comment lines and quoted strings (e.g. a log message
        # mentioning a bcdboot "exit" code is not a shell exit).
        for line in self._script().splitlines():
            code = line.split('#', 1)[0]
            code = re.sub(r'"[^"]*"', '""', code)
            code = re.sub(r"'[^']*'", "''", code)
            assert not re.search(r'\bexit\b', code), f"must not call exit: {line!r}"

    def test_uses_bcdboot_offline(self):
        text = self._script()
        assert 'bcdboot' in text
        # Targets the offline install's \Windows, both firmware modes.
        assert '/f UEFI' in text
        assert '/f BIOS' in text

    def test_locates_windows_by_config_hive(self):
        # Finds the offline install by its registry hive, not a hardcoded D:.
        assert 'Windows\\System32\\config\\SYSTEM' in self._script()

    def test_releases_temp_esp_letter(self):
        # Cleans up the temporary ESP drive letter so restore is clean.
        assert 'Remove-PartitionAccessPath' in self._script()

    def test_uses_lf_line_endings(self):
        assert b'\r' not in _BCD_FIX_SCRIPT.read_bytes()
