"""
Tests for the filesystem fix script (startup_scripts/fixes/filesystem_fix.sh).

The script cannot be executed end-to-end in unit tests (it needs real block
devices), so these tests validate it the same way the fstab fix script is
validated: bash syntax check, marker-protocol compliance, safety guards, and
composition with the real base mount script via compose_startup_script.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import List

import pytest
import yaml

from gce_rescue_v2.orchestration.compose import (
    compose_startup_script,
    extract_premount_blocks,
    strip_shebang,
)

FIX_PATH = (
    Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'filesystem_fix.sh'
)
BASE_PATH = Path(__file__).parent.parent / 'startup_scripts' / 'rescue_mount.sh'
YAML_PATH = (
    Path(__file__).parent.parent / 'core' / 'diagnose_rules' / 'filesystem.yaml'
)


def _script() -> str:
    return FIX_PATH.read_text()


def _code_lines(content: str) -> List[str]:
    """Script lines with full-line comments removed."""
    return [
        line for line in content.split('\n')
        if not line.strip().startswith('#')
    ]


def _find_bash() -> str:
    """Locate a working bash (the Windows WSL stub in System32 is not one)."""
    candidates = [
        shutil.which('bash'),
        r'C:\Program Files\Git\usr\bin\bash.exe',
        '/usr/bin/bash',
        '/bin/bash',
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        try:
            probe = subprocess.run(
                [candidate, '-c', 'echo ok'],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and 'ok' in probe.stdout:
            return candidate
    return ''


# ---------------------------------------------------------------------------
# TestFilesystemFixScript - static validation of the script file
# ---------------------------------------------------------------------------

class TestFilesystemFixScript:
    """Validate that the filesystem fix script file is well-formed."""

    def test_fix_script_exists(self):
        """filesystem_fix.sh should exist in startup_scripts/fixes/."""
        assert FIX_PATH.exists(), f"Fix script not found at {FIX_PATH}"

    def test_bash_syntax_is_valid(self):
        """bash -n should accept the script."""
        bash = _find_bash()
        if not bash:
            pytest.skip('no working bash available')
        # Forward slashes so Git bash on Windows resolves the path.
        path = str(FIX_PATH).replace('\\', '/')
        result = subprocess.run(
            [bash, '-n', path], capture_output=True, text=True, timeout=30
        )
        assert result.returncode == 0, f"bash -n failed: {result.stderr}"

    def test_emits_repair_markers(self):
        """Script should emit GCE-REPAIR-LINE and GCE-REPAIR-RESULT markers."""
        content = _script()
        assert 'GCE-REPAIR-LINE:' in content
        assert 'GCE-REPAIR-RESULT:' in content

    def test_single_result_emission_helper(self):
        """The result marker string must appear once, inside repair_result()."""
        content = _script()
        assert content.count('GCE-REPAIR-RESULT:') == 1

    def test_result_branches_cover_all_outcomes(self):
        """One result call per outcome: FAILED / SUCCESS / FAILED / NO_ISSUES."""
        content = _script()
        assert content.count('repair_result "') == 4
        assert 'repair_result "SUCCESS:$FSFIX_FIXED"' in content
        assert 'repair_result "NO_ISSUES:0"' in content
        assert 'repair_result "FAILED:' in content
        # Mutually exclusive if/elif/else chain drives the result decision
        assert 'elif' in content

    def test_result_requires_successful_mount(self):
        """SUCCESS must be gated on /mnt/sysroot actually being mounted."""
        content = _script()
        assert 'mountpoint -q "$SYSROOT"' in content

    def test_no_exit_calls(self):
        """exit would abort the whole startup script; guards must be used."""
        for line in _code_lines(_script()):
            assert not re.search(r'(?:^|[;&|`(]\s*)exit\b', line), (
                f"Fix script must never call exit: {line!r}"
            )

    def test_no_completion_marker(self):
        """Completion signal is owned by the composer, never the fix script."""
        content = _script()
        assert 'GCE-RESCUE-COMPLETE' not in content
        assert not re.search(r'^\s*signal_complete\s*$', content, re.MULTILINE)

    def test_premount_block_well_formed(self):
        """Exactly one pre-mount block; result decision stays post-mount."""
        blocks, remainder = extract_premount_blocks(strip_shebang(_script()))
        assert len(blocks) == 1
        block = blocks[0]
        # All fsck work happens pre-mount, on the unmounted disk
        assert 'lsblk' in block
        assert 'e2fsck' in block
        assert 'xfs_repair' in block
        assert '/dev/disk/by-id/google-${disk}' in block
        # The single final result marker is emitted by the post-mount part
        assert 'GCE-REPAIR-RESULT' not in block
        assert 'GCE-REPAIR-RESULT' in remainder

    def test_ext_fsck_uses_force_and_yes(self):
        """ext repair must run e2fsck -f -y and honor its exit-code contract."""
        content = _script()
        assert 'e2fsck -f -y' in content
        # 0=clean, 1/2(/3)=fixed, >=4=unfixed - all three classes handled
        assert '-eq 0' in content
        assert '-le 3' in content

    def test_xfs_never_uses_destructive_log_zeroing(self):
        """xfs_repair -L must never be executed (only mentioned as guidance)."""
        for args in re.findall(r'\$\(xfs_repair([^)]*)\)', _script()):
            assert ' -L' not in args, 'xfs_repair -L must not be executed'
        for line in _code_lines(_script()):
            assert not re.search(r'^\s*xfs_repair\s', line), (
                f"xfs_repair should only run in captured form: {line!r}"
            )
        # ...but the manual last resort is surfaced in the failure guidance
        assert 'xfs_repair -L' in _script()

    def test_xfs_dirty_log_replayed_via_mount_cycle(self):
        """Dirty XFS log: mount/unmount replay + single retry, no -L."""
        content = _script()
        assert 'mount -o nouuid' in content
        assert 'umount' in content
        assert 'replay' in content.lower()

    def test_xfs_tool_installed_when_missing(self):
        """xfsprogs may be absent on the Debian rescue image."""
        content = _script()
        assert 'command -v' in content
        assert 'apt-get install -y' in content
        assert 'xfsprogs' in content

    def test_btrfs_check_is_read_only(self):
        """btrfs is checked read-only; --repair must never run."""
        content = _script()
        assert 'btrfs check --readonly' in content
        for line in _code_lines(content):
            assert '--repair' not in line, (
                f"btrfs check --repair must not be executed: {line!r}"
            )

    def test_btrfs_findings_not_counted_as_fixed(self):
        """btrfs errors are reported, recorded as failure, never as a fix."""
        content = _script()
        assert '[INFO] filesystem: btrfs errors found' in content
        btrfs_fn = content.split('fsfix_check_btrfs()')[1].split('\n}')[0]
        assert 'FSFIX_FIXED=$((FSFIX_FIXED + 1))' not in btrfs_fn
        assert 'fsfix_add_failure' in btrfs_fn

    def test_handles_whole_disk_filesystem(self):
        """lsblk enumeration must include the no-partition-table case."""
        content = _script()
        assert 'lsblk -pnro NAME,FSTYPE' in content
        assert 'whole disk' in content

    def test_snapshot_is_documented_safety_net(self):
        """The pre-rescue snapshot is the backup for fsck modifications."""
        assert 'snapshot' in _script().lower()

    def test_copies_log_to_disk(self):
        """Full repair log should survive restore on the original disk."""
        assert 'gce-repair.log' in _script()

    def test_state_shared_via_shell_variables(self):
        """Pre-mount state reaches the result decision via FSFIX_* variables."""
        blocks, remainder = extract_premount_blocks(strip_shebang(_script()))
        assert 'FSFIX_FIXED=0' in blocks[0]
        assert 'FSFIX_FAIL_REASONS' in blocks[0]
        assert '${FSFIX_FIXED:-0}' in remainder
        assert '${FSFIX_FAIL_REASONS:-}' in remainder

    def test_failure_reasons_are_single_line(self):
        """FAILED:<reason> must never contain newlines (joined with '; ')."""
        content = _script()
        assert 'FSFIX_FAIL_REASONS; $1' in content
        for match in re.finditer(r'fsfix_add_failure\s+"([^"]*)"', content):
            assert '\n' not in match.group(1)


# ---------------------------------------------------------------------------
# TestFilesystemFixComposition - script composed with the real base script
# ---------------------------------------------------------------------------

class TestFilesystemFixComposition:
    """Compose filesystem_fix.sh with the real rescue_mount.sh."""

    def _compose(self) -> str:
        base = BASE_PATH.read_text().replace('DISK_NAME_PLACEHOLDER', 'orig-disk')
        return compose_startup_script(
            base, [strip_shebang(_script())], repair_targets=[]
        )

    def test_premount_block_runs_before_mount_attempt(self):
        """fsck must execute after the disk-wait loop, before any mount."""
        combined = self._compose()
        premount_pos = combined.index('filesystem repair (pre-mount) started')
        assert combined.index('Waiting for disk to appear') < premount_pos
        assert premount_pos < combined.index('log "Detecting filesystem type..."')

    def test_result_decision_runs_after_mount(self):
        """The result marker is emitted from the appended post-mount part."""
        combined = self._compose()
        anchor = combined.index('=== GCE Repair Fix Scripts ===')
        assert combined.index('GCE-REPAIR-RESULT:$1', anchor) > anchor

    def test_completion_signal_relocated_to_end(self):
        """Exactly one active signal_complete call, after the fix script."""
        combined = self._compose()
        calls = re.findall(r'^\s*signal_complete\s*$', combined, re.MULTILINE)
        assert len(calls) == 1
        assert combined.rindex('signal_complete') > combined.index(
            'repair_result "NO_ISSUES:0"'
        )


# ---------------------------------------------------------------------------
# TestFilesystemAutoRepairFlag - YAML flag flipped with the script in place
# ---------------------------------------------------------------------------

class TestFilesystemAutoRepairFlag:
    """filesystem.yaml must enable auto_repair now that the script exists."""

    def _config(self) -> dict:
        return yaml.safe_load(YAML_PATH.read_text())

    def test_auto_repair_enabled(self):
        config = self._config()
        assert config['category'] == 'filesystem'
        assert config['auto_repair'] is True

    def test_fix_script_present_for_flag(self):
        """auto_repair:true is only valid with the fix script on disk."""
        assert self._config()['auto_repair'] is True
        assert FIX_PATH.exists()

    def test_survives_boot_success_preserved(self):
        """Corrupt nofail secondary disks outlive a successful boot."""
        assert self._config()['survives_boot_success'] is True


class TestFilesystemFixLiveProbe:
    """Freshly hot-attached disks have a stale udev cache: lsblk's FSTYPE
    can be blank for a healthy XFS partition (observed live on Rocky 9,
    misrouting the root away from xfs_repair). The fix must live-probe."""

    def test_probe_function_uses_low_level_blkid(self):
        text = _script()
        assert 'blkid -p -o value -s TYPE' in text

    def test_udevadm_settle_before_enumeration(self):
        text = _script()
        assert 'udevadm settle' in text
        # Settle must come before the lsblk enumeration
        assert text.index('udevadm settle') < text.index('lsblk -pnro NAME,FSTYPE')

    def test_blank_fstype_reprobed_before_blank_recovery(self):
        """Empty FSTYPE goes through the live probe before being treated as
        a signatureless (wiped-superblock) device."""
        text = _script()
        assert 'fsfix_probe_type' in text
        probe_call = text.rindex('fsfix_probe_type "$fsfix_dev"')
        blank_call = text.rindex('fsfix_check_blank "$fsfix_dev"')
        assert probe_call < blank_call
