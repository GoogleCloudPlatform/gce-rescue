"""Tests for the windows_boot_manager diagnose category (Windows, detect-only).

Verifies the YAML loads with the correct OS scope and flags, that every
regex fires on a realistic Windows Boot Manager failure snippet, and -
crucially - that no regex cross-fires on (i) a healthy GCE Windows boot,
(ii) a Linux emergency-mode buffer, or (iii) our own rescue-script output.
"""

import re

import pytest

from gce_rescue_v2.core.diagnosis import (
    BOOT_ERROR_PATTERNS,
    DETECT_ONLY_CATEGORIES,
    SURVIVES_BOOT_SUCCESS_CATEGORIES,
)
from gce_rescue_v2.core import fix_catalog

CATEGORY = 'windows_boot_manager'

# Mirror the engine: it strips ANSI escapes then matches every regex with
# re.MULTILINE | re.IGNORECASE (diagnosis.py analyze_serial_output).
_ANSI = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\r')


def _matches(regex: str, text: str) -> bool:
    """True if regex matches text the way the engine would."""
    stripped = _ANSI.sub('', text)
    return re.search(regex, stripped, re.MULTILINE | re.IGNORECASE) is not None


@pytest.fixture
def patterns():
    return [p for p in BOOT_ERROR_PATTERNS if p.category == CATEGORY]


@pytest.fixture
def regexes(patterns):
    return [r for p in patterns for r in p.patterns]


# --- Realistic failure fixtures --------------------------------------------

# Verbatim (ANSI-stripped) fragment of the observed 0xc000000e Boot Manager
# screen from rbwin-postrestore-serial.txt.
FAIL_0XC000000E = (
    "                            Windows Boot Manager                          "
    "Windows failed to start. A recent hardware or software change might be the"
    "cause. To fix the problem: "
    "1. Insert your Windows installation disc and restart your computer."
    "Status: 0xc000000e"
    "Info: A required device isn't connected or can't be accessed. "
    "ENTER=OS Selection                 ESC=UEFI Firmware Settings "
)

FAIL_0XC0000225 = (
    "Windows Boot Manager\n"
    "Windows failed to start.\n"
    "File: \\Windows\\system32\\winload.efi\n"
    "Status: 0xc0000225\n"
    "Info: The boot selection failed because a required device is inaccessible."
)

FAIL_BCD_CORRUPT = (
    "Windows Boot Manager\n"
    "Windows failed to start.\n"
    "Status: 0xc000014c\n"
    "Info: The Boot Configuration Data file is missing some required "
    "information."
)

FAIL_BOOTMGR_MISSING = "BOOTMGR is missing\nPress Ctrl+Alt+Del to restart"


# --- Negative fixtures (must NEVER match) ----------------------------------

# Healthy GCE Windows boot: contains "Windows Boot Manager" (BdsDxe), plus
# benign error/failed/not-found/Offline vocabulary from GCEGuestAgent.
HEALTHY_WINDOWS = (
    'BdsDxe: loading Boot0003 "Windows Boot Manager" from '
    'HD(2,GPT,CA3FEE5F,0x8000,0x32000)/\\EFI\\Microsoft\\Boot\\bootmgfw.efi\n'
    'UEFI: Attempting to start image.\n'
    'CSM BBS Table full.\n'
    '2026-07-10T10:39:01.2353Z  GCEGuestAgentManager: [INFO]: Initializing '
    'Google Guest Agent...\n'
    'CorePlugin: [WARNING]: Could not get previous serial number, will skip '
    'cleanup: The system cannot find the file specified.\n'
    'CorePlugin: [WARNING]: Failed to find previous certificate with error: '
    'Cannot find object or property.\n'
    '2026/07/10 10:40:15 GCEInstanceSetup: Instance setup finished. '
    'rb-win is ready to use.'
)

# Linux emergency-mode buffer.
LINUX_EMERGENCY = (
    "[   6.123456] systemd[1]: Failed to mount /home.\n"
    "You are in emergency mode. After logging in, type \"journalctl -xb\".\n"
    "[FAILED] Failed to mount /boot.\n"
    "Give root password for maintenance\n"
    "error: no such partition.\n"
    "Kernel panic - not syncing: VFS: Unable to mount root fs"
)

# Our own rescue-script output - note it logs the token "Status: Offline".
RESCUE_MARKERS = (
    '[INFO]: Metadata key("windows-startup-script-ps1"), '
    'command("powershell.exe"): === GCE Rescue Mode - Windows ===\n'
    'Target drive letter: D:\n'
    'Status: Offline\n'
    'Bringing disk online...\n'
    '=== Creating Rescue Admin Account ===\n'
    'Created new user: rescue_admin\n'
    'GCE-RESCUE-COMPLETE\n'
    '=== GCE Rescue Ready ==='
)


class TestYamlStructure:
    """Structural / metadata assertions for the shipped YAML."""

    def test_category_present(self, patterns):
        assert len(patterns) == 5

    def test_os_scope_is_windows(self, patterns):
        for p in patterns:
            assert p.os == 'windows', f"{p.name} is not scoped to windows"

    def test_names_prefixed(self, patterns):
        for p in patterns:
            assert p.name.startswith('windows_'), (
                f"Pattern '{p.name}' missing 'windows_' prefix"
            )

    def test_all_critical(self, patterns):
        for p in patterns:
            assert p.severity == 'critical'

    def test_all_have_inline_fixes(self, patterns):
        for p in patterns:
            assert len(p.fixes) > 0, f"Pattern '{p.name}' has no inline fixes"

    def test_detect_only(self):
        # Windows repair is a future branch: detect-only, never auto_repair.
        assert CATEGORY in DETECT_ONLY_CATEGORIES
        assert CATEGORY not in fix_catalog.SUPPORTED_FIX_CATEGORIES

    def test_not_survives_boot_success(self):
        # A boot-manager failure blocks boot; it is not a runtime condition
        # that lingers past a later successful boot.
        assert CATEGORY not in SURVIVES_BOOT_SUCCESS_CATEGORIES

    def test_has_fix_guidance(self):
        # detect_only categories MUST carry fix_guidance (fix_catalog parity).
        assert fix_catalog.CATEGORY_FIX_GUIDANCE.get(CATEGORY)


class TestPositiveMatches:
    """Every regex must fire on a realistic Windows failure snippet."""

    def test_0xc000000e_status_code(self, regexes):
        assert any(_matches(r, FAIL_0XC000000E) for r in regexes)

    def test_0xc000000e_info_sentence(self, regexes):
        assert any(
            _matches(r, "A required device isn't connected or can't be accessed")
            for r in regexes
        )

    def test_failed_to_start_header(self, regexes):
        assert any(_matches(r, FAIL_0XC000000E) for r in regexes)

    def test_0xc0000225_needs_repair(self, regexes):
        assert any(_matches(r, FAIL_0XC0000225) for r in regexes)

    def test_winload_path(self, regexes):
        assert any(
            _matches(r, "File: \\Windows\\system32\\winload.efi") for r in regexes
        )

    def test_bcd_corrupt_code(self, regexes):
        assert any(_matches(r, FAIL_BCD_CORRUPT) for r in regexes)

    def test_bootmgr_missing(self, regexes):
        assert any(_matches(r, FAIL_BOOTMGR_MISSING) for r in regexes)

    def test_every_regex_matches_something(self, regexes):
        """Each individual regex must match at least one failure fixture,
        proving no regex is dead."""
        fixtures = [
            FAIL_0XC000000E,
            FAIL_0XC0000225,
            FAIL_BCD_CORRUPT,
            FAIL_BOOTMGR_MISSING,
            # Remaining knowledge-based status codes covered by the
            # needs_repair / bcd_corrupt alternations.
            "Windows failed to start.\nStatus: 0xc000000f\nFile: \\Boot\\BCD",
            "Windows failed to start.\nStatus: 0xc0000034",
            "Windows failed to start.\nStatus: 0xc0000098",
            "Windows failed to start.\nStatus: 0xc0000102",
        ]
        for r in regexes:
            assert any(_matches(r, f) for f in fixtures), (
                f"Regex never matches any failure fixture: {r}"
            )


class TestNoCrossFire:
    """No regex may match healthy Windows, Linux, or rescue-script output."""

    def test_no_match_healthy_windows(self, regexes):
        for r in regexes:
            assert not _matches(r, HEALTHY_WINDOWS), (
                f"Regex cross-fired on a healthy Windows boot: {r}"
            )

    def test_no_match_linux_emergency(self, regexes):
        for r in regexes:
            assert not _matches(r, LINUX_EMERGENCY), (
                f"Regex cross-fired on a Linux emergency buffer: {r}"
            )

    def test_no_match_rescue_markers(self, regexes):
        for r in regexes:
            assert not _matches(r, RESCUE_MARKERS), (
                f"Regex cross-fired on rescue-script output: {r}"
            )
