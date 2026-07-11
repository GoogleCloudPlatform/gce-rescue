"""Tests for the windows_recovery diagnose category.

The category is knowledge-based (no serial corpus captures WinRE / Windows
Update loop screens) and detect-only. These tests pin the three invariants
that keep it safe:

  1. It is OS-scoped to Windows (os == 'windows'), detect-only, and never
     auto-repairs.
  2. Every regex matches a realistic Windows recovery / update-loop snippet.
  3. No regex matches (a) a healthy GCE Windows boot, (b) a Linux
     emergency-mode boot, or (c) our own rescue-mode startup output.

Regex matching mirrors the engine: ANSI escapes / carriage returns are
stripped first, then re.search with re.MULTILINE | re.IGNORECASE.
"""

import re
from pathlib import Path

import pytest
import yaml

from gce_rescue_v2.core.diagnosis import (
    _load_patterns_from_yaml,
    analyze_serial_output,
)

CATEGORY = 'windows_recovery'
_YAML_PATH = (
    Path(__file__).resolve().parents[1]
    / 'core' / 'diagnose_rules' / 'windows_recovery.yaml'
)

# Same flags the engine uses in analyze_serial_output's pattern loop.
_FLAGS = re.MULTILINE | re.IGNORECASE
# Same pre-processing the engine applies before matching.
_ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\r')


def _strip(text: str) -> str:
    return _ANSI.sub('', text)


# --- Negative fixtures (must never match any windows_recovery regex) -------

# Healthy GCE Windows Server boot: firmware BdsDxe line loading the real
# Windows Boot Manager, then GCEGuestAgent / GCEInstanceSetup logging.
HEALTHY_WINDOWS = _strip(
    'CSM BBS Table full.\n'
    'BdsDxe: loading Boot0003 "Windows Boot Manager" from '
    'HD(2,GPT,CA3FEE5F-0000-0000-0000-000000000000,0x8000,0x32000)/'
    '\\EFI\\Microsoft\\Boot\\bootmgfw.efi\n'
    'Description: Windows Boot Manager\n'
    'UEFI: Attempting to start image.\n'
    '2026-07-10T10:39:01.2353Z  GCEGuestAgentManager: [INFO]: '
    'Initializing Google Guest Agent...\n'
    'CorePlugin: [WARNING]: Failed to find previous certificate with '
    'error: unable to find certificate: Cannot find object or property.\n'
    '2026/07/10 10:40:15 GCEInstanceSetup: Starting sysprep specialize '
    'phase.\n'
    '2026/07/10 10:40:20 GCEInstanceSetup: Configuring WinRM...\n'
    '--- Instance setup finished. rb-win is ready to use. ---\n'
)

# Linux emergency-mode boot: none of the Windows UI wording appears.
LINUX_EMERGENCY = (
    '[   12.345678] systemd[1]: Failed to mount /home.\n'
    'You are in emergency mode. After logging in, type "journalctl -xb" '
    'to view system logs.\n'
    'Cannot open access to console, the root account is locked.\n'
    'Give root password for maintenance (or press Control-D to '
    'continue):\n'
)

# Our own rescue-mode startup output on a Windows rescue disk.
RESCUE_MARKERS = (
    '[INFO]: Metadata key("windows-startup-script-ps1"), '
    'command("powershell.exe"): === GCE Rescue Mode - Windows ===\n'
    'Target drive letter: D:\n'
    '=== Creating Rescue Admin Account ===\n'
    'Created new user: rescue_admin\n'
    'Status: Offline\n'
    'Bringing disk online...\n'
    'GCE-RESCUE-COMPLETE\n'
    '=== GCE Rescue Ready ===\n'
)

_NEGATIVES = {
    'healthy_windows': HEALTHY_WINDOWS,
    'linux_emergency': LINUX_EMERGENCY,
    'rescue_markers': RESCUE_MARKERS,
}

# --- Positive fixtures: one realistic snippet per pattern name -------------
# Knowledge-based wording (WinRE / Windows Update screens), rendered as it
# would appear once ANSI escapes are stripped.
_POSITIVES = {
    'windows_recovery_automatic_repair': (
        'Preparing Automatic Repair\n'
        'Diagnosing your PC\n'
        'Automatic Repair couldn’t repair your PC\n'
        'Your PC didn’t start correctly\n'
    ),
    'windows_recovery_needs_repair': (
        'Recovery\n'
        'Your PC needs to be repaired\n'
        'The application or operating system couldn’t be loaded.\n'
    ),
    'windows_recovery_update_rollback': (
        'Failure configuring Windows updates. Reverting changes.\n'
        'We couldn’t complete the updates. Undoing changes.\n'
        'Undoing changes made to your computer\n'
    ),
    'windows_recovery_update_pending': (
        'Getting Windows ready. Don’t turn off your computer.\n'
        'Please wait while we install a system update.\n'
    ),
}


@pytest.fixture(scope='module')
def category_patterns():
    """Load the shipped patterns and return only the windows_recovery ones."""
    all_patterns = _load_patterns_from_yaml()
    return [p for p in all_patterns if p.category == CATEGORY]


@pytest.fixture(scope='module')
def raw_yaml():
    with _YAML_PATH.open(encoding='utf-8') as fh:
        return yaml.safe_load(fh)


class TestWindowsRecoveryMetadata:
    """File-level flags and OS scoping."""

    def test_yaml_loads_with_patterns(self, category_patterns):
        assert len(category_patterns) == 4

    def test_os_scoped_to_windows(self, category_patterns):
        assert all(p.os == 'windows' for p in category_patterns)

    def test_detect_only(self, category_patterns):
        assert all(p.detect_only for p in category_patterns)

    def test_auto_repair_false(self, raw_yaml):
        assert raw_yaml.get('auto_repair') is False

    def test_has_fix_guidance(self, raw_yaml):
        # Required for fix_catalog parity: any detect_only YAML must carry
        # fix_guidance or test_detect_only_sets_agree fails.
        assert raw_yaml.get('fix_guidance')

    def test_all_patterns_critical(self, category_patterns):
        assert all(p.severity == 'critical' for p in category_patterns)

    def test_pattern_names_prefixed(self, category_patterns):
        assert all(p.name.startswith('windows_recovery') for p in category_patterns)

    def test_every_pattern_has_fixes(self, category_patterns):
        assert all(p.fixes for p in category_patterns)

    def test_no_bcd_status_codes(self, category_patterns):
        # Boot Manager status codes belong to windows_boot_manager; this
        # category owns only the recovery/update wording.
        for pat in category_patterns:
            for rgx in pat.patterns:
                assert '0xc0' not in rgx.lower()


class TestWindowsRecoveryPositiveMatches:
    """Each regex matches a realistic failure snippet."""

    @pytest.mark.parametrize('name', list(_POSITIVES))
    def test_pattern_matches_its_snippet(self, category_patterns, name):
        pat = next(p for p in category_patterns if p.name == name)
        snippet = _strip(_POSITIVES[name])
        matched = any(
            re.search(rgx, snippet, _FLAGS) for rgx in pat.patterns
        )
        assert matched, f'{name} did not match its own failure snippet'


class TestWindowsRecoveryNegativeMatches:
    """No regex fires on healthy / Linux / rescue output."""

    @pytest.mark.parametrize('label', list(_NEGATIVES))
    def test_no_regex_matches_negative(self, category_patterns, label):
        text = _NEGATIVES[label]
        for pat in category_patterns:
            for rgx in pat.patterns:
                assert not re.search(rgx, text, _FLAGS), (
                    f'{pat.name} regex {rgx!r} cross-fired on {label}'
                )


class TestWindowsRecoveryEngineScoping:
    """End-to-end: the engine only surfaces these on Windows VMs."""

    def _serial(self) -> str:
        # A buffer long enough to clear the engine's <50-char guard, with
        # firmware framing plus the Automatic Repair loop wording.
        return (
            'CSM BBS Table full.\n'
            'BdsDxe: starting Boot0001 "UEFI Google PersistentDisk"\n'
            'UEFI: Attempting to start image.\n'
            + _POSITIVES['windows_recovery_automatic_repair']
        )

    def test_surfaces_on_windows(self):
        result = analyze_serial_output(
            self._serial(), 'test-vm', 'zone-a', 'TERMINATED',
            os_type='windows',
        )
        categories = {e.category for e in result.boot_errors}
        assert CATEGORY in categories

    def test_skipped_on_linux(self):
        result = analyze_serial_output(
            self._serial(), 'test-vm', 'zone-a', 'TERMINATED',
            os_type='linux',
        )
        categories = {e.category for e in result.boot_errors}
        assert CATEGORY not in categories

    def test_runs_on_unknown(self):
        # Degraded-permission path (os_type unknown) runs all patterns.
        result = analyze_serial_output(
            self._serial(), 'test-vm', 'zone-a', 'TERMINATED',
            os_type='unknown',
        )
        categories = {e.category for e in result.boot_errors}
        assert CATEGORY in categories
