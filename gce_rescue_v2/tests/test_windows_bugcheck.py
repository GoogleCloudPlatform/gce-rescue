"""Tests for the windows_bugcheck diagnose category.

windows_bugcheck detects Windows kernel STOP errors (bugchecks / BSOD) on the
serial console. Every pattern is os-scoped to Windows and detect-only. These
tests verify the YAML loads with the right flags, that each regex matches a
realistic Windows STOP snippet, and — critically — that no regex fires on a
healthy Windows boot, a Linux emergency-mode buffer, or our own rescue-mode
script output.
"""

from pathlib import Path
import re

import pytest
import yaml

from gce_rescue_v2.core.diagnosis import (
    BOOT_ERROR_PATTERNS,
    analyze_serial_output,
)
from gce_rescue_v2.core import fix_catalog

CATEGORY = 'windows_bugcheck'
YAML_PATH = (
    Path(__file__).resolve().parent.parent
    / 'core' / 'diagnose_rules' / 'windows_bugcheck.yaml'
)

# --- Negative fixtures (must never match a windows_bugcheck regex) ---------

# A healthy Windows Server 2022 GCE boot: firmware BdsDxe lines (which print
# the literal "Windows Boot Manager"), GCEGuestAgent output, and benign
# warnings containing the words error/failed/Offline/not found.
HEALTHY_WINDOWS = (
    'BdsDxe: loading Boot0003 "Windows Boot Manager" from '
    'HD(2,GPT,CA3FEE5F,0x8000,0x32000)/\\EFI\\Microsoft\\Boot\\bootmgfw.efi\n'
    'UEFI: Attempting to start image.\n'
    '2026-07-10T10:39:01.2353Z  GCEGuestAgentManager: [INFO]: Initializing '
    'Google Guest Agent...\n'
    'CorePlugin: [WARNING]: Failed to find previous certificate with error: '
    'unable to find certificate: Cannot find object or property.\n'
    'CorePlugin: [WARNING]: ... The system cannot find the file specified.\n'
    '2026/07/10 10:40:15 GCEInstanceSetup: Instance setup finished. '
    'rb-win is ready to use.\n'
)

# A broken *Linux* boot (systemd emergency mode) — windows patterns must be
# inert against it even though it is a genuine failure buffer.
LINUX_EMERGENCY = (
    '[    5.123456] systemd[1]: Failed to mount /boot.\n'
    'You are in emergency mode. After logging in, type "journalctl -xb".\n'
    'Give root password for maintenance (or press Control-D to continue):\n'
    '[   10.001] EXT4-fs (sda1): Remounting filesystem read-only\n'
)

# Our own rescue-mode PowerShell startup script output.
RESCUE_MARKERS = (
    '[INFO]: Metadata key("windows-startup-script-ps1"), '
    'command("powershell.exe"): === GCE Rescue Mode - Windows ===\n'
    'Target drive letter: D:\n'
    'Status: Offline\n'
    'Bringing disk online...\n'
    'Created new user: rescue_admin\n'
    'GCE-RESCUE-COMPLETE\n'
    '=== Startup script completed successfully ===\n'
)

NEGATIVE_FIXTURES = {
    'healthy_windows': HEALTHY_WINDOWS,
    'linux_emergency': LINUX_EMERGENCY,
    'rescue_markers': RESCUE_MARKERS,
}


def _bugcheck_patterns():
    return [p for p in BOOT_ERROR_PATTERNS if p.category == CATEGORY]


class TestWindowsBugcheckYaml:
    """The YAML loads with the flags the engine + fix_catalog require."""

    def test_yaml_loads_and_is_scoped_to_windows(self):
        patterns = _bugcheck_patterns()
        assert patterns, "windows_bugcheck category produced no patterns"
        assert all(p.os == 'windows' for p in patterns)

    def test_is_detect_only(self):
        patterns = _bugcheck_patterns()
        assert all(p.detect_only for p in patterns)
        # detect_only categories must NOT survive boot success by default.
        assert all(not p.survives_boot_success for p in patterns)

    def test_all_patterns_critical(self):
        # Every bugcheck is boot/OS-fatal, so all are critical severity.
        assert all(p.severity == 'critical' for p in _bugcheck_patterns())

    def test_pattern_name_prefix_convention(self):
        assert all(
            p.name.startswith('windows_bugcheck_')
            for p in _bugcheck_patterns()
        )

    def test_auto_repair_false_and_has_fix_guidance(self):
        data = yaml.safe_load(YAML_PATH.read_text(encoding='utf-8'))
        assert data['auto_repair'] is False
        assert data['detect_only'] is True
        assert data.get('fix_guidance')

    def test_not_in_auto_repair_catalog_but_is_detect_only(self):
        # Guards the fix_catalog/diagnosis detect-only parity contract.
        assert CATEGORY not in fix_catalog.SUPPORTED_FIX_CATEGORIES
        assert CATEGORY in fix_catalog.DETECT_ONLY_CATEGORIES


class TestWindowsBugcheckPositiveMatches:
    """Each pattern matches a realistic Windows STOP snippet."""

    # (pattern_name, matching snippet). Both the bugcheck-name form and the
    # STOP-code form are exercised where the pattern carries both.
    POSITIVE_CASES = [
        (
            'windows_bugcheck_inaccessible_boot_device',
            'A problem has been detected...\n'
            '*** STOP: 0x0000007B (0xFFFFF880009A97E8)\n'
            'INACCESSIBLE_BOOT_DEVICE\n',
        ),
        (
            'windows_bugcheck_unmountable_boot_volume',
            'STOP: 0x000000ED UNMOUNTABLE_BOOT_VOLUME\n',
        ),
        (
            'windows_bugcheck_critical_process_died',
            'Your PC ran into a problem. Stop code: CRITICAL_PROCESS_DIED\n',
        ),
        (
            'windows_bugcheck_system_thread_exception',
            'STOP: 0x1000007E SYSTEM_THREAD_EXCEPTION_NOT_HANDLED '
            '(0xFFFFFFFFC0000005, ..., nvlddmkm.sys)\n',
        ),
        (
            'windows_bugcheck_generic',
            'A problem has been detected and Windows has been shut down '
            'to prevent damage to your computer.\n',
        ),
    ]

    @pytest.mark.parametrize('name,snippet', POSITIVE_CASES)
    def test_pattern_matches_failure_snippet(self, name, snippet):
        pattern = next(p for p in _bugcheck_patterns() if p.name == name)
        matched = any(
            re.search(rx, snippet, re.MULTILINE | re.IGNORECASE)
            for rx in pattern.patterns
        )
        assert matched, f"{name} did not match its failure snippet"

    def test_stop_code_form_matches_lowercase_hex(self):
        # Serial output renders codes lowercase; the engine matches
        # case-insensitively, so the STOP-code regex must still fire.
        pattern = next(
            p for p in _bugcheck_patterns()
            if p.name == 'windows_bugcheck_inaccessible_boot_device'
        )
        snippet = 'stop: 0x0000007b\n'
        matched = any(
            re.search(rx, snippet, re.MULTILINE | re.IGNORECASE)
            for rx in pattern.patterns
        )
        assert matched


class TestWindowsBugcheckNoFalsePositives:
    """No pattern fires on healthy/foreign/rescue buffers."""

    def _all_regexes(self):
        rxs = []
        for p in _bugcheck_patterns():
            rxs.extend(p.patterns)
        return rxs

    @pytest.mark.parametrize('fixture_name', list(NEGATIVE_FIXTURES))
    def test_no_regex_matches_negative_fixture(self, fixture_name):
        buffer = NEGATIVE_FIXTURES[fixture_name]
        for rx in self._all_regexes():
            assert not re.search(rx, buffer, re.MULTILINE | re.IGNORECASE), (
                f"regex {rx!r} unexpectedly matched {fixture_name}"
            )


class TestWindowsBugcheckOsScoping:
    """End-to-end: the engine only surfaces these on Windows/unknown VMs."""

    BSOD = (
        'BdsDxe: starting Boot0001 "UEFI Google PersistentDisk"\n'
        'A problem has been detected and Windows has been shut down '
        'to prevent damage to your computer.\n'
        '*** STOP: 0x0000007B (0xFFFFF880009A97E8)\n'
        'INACCESSIBLE_BOOT_DEVICE\n'
    ) * 2  # pad past the 50-char guard, mimic a boot-loop buffer

    def _has_bugcheck(self, result):
        return any(e.category == CATEGORY for e in result.boot_errors)

    def test_detected_on_windows_vm(self):
        result = analyze_serial_output(
            self.BSOD, 'win-vm', 'zone-a', 'RUNNING', os_type='windows'
        )
        assert self._has_bugcheck(result)

    def test_skipped_on_linux_vm(self):
        result = analyze_serial_output(
            self.BSOD, 'lin-vm', 'zone-a', 'RUNNING', os_type='linux'
        )
        assert not self._has_bugcheck(result)

    def test_runs_on_unknown_os(self):
        # Degraded-permission path (os_type unknown) must still surface it.
        result = analyze_serial_output(
            self.BSOD, 'vm', 'zone-a', 'RUNNING', os_type='unknown'
        )
        assert self._has_bugcheck(result)

    def test_healthy_windows_boot_is_clean(self):
        result = analyze_serial_output(
            HEALTHY_WINDOWS, 'win-vm', 'zone-a', 'RUNNING', os_type='windows'
        )
        assert not self._has_bugcheck(result)
