"""Tests for the grub fix script (startup_scripts/fixes/grub_fix.sh).

Mirrors the TestFstabFixScript approach in test_repair.py: the script is
validated statically (substring/structure assertions on the file text) plus
a bash syntax check when a bash interpreter is available on the dev box.
No test actually executes the repair logic — that requires a mounted
sysroot and runs only in E2E.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

import pytest
import yaml

FIX_SCRIPT_PATH = (
    Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'grub_fix.sh'
)
GRUB_YAML_PATH = (
    Path(__file__).parent.parent / 'core' / 'diagnose_rules' / 'grub.yaml'
)


def _usable_bash() -> Optional[str]:
    """Full path to a working bash, or None.

    On Windows, subprocess's CreateProcess search finds the WSL stub in
    System32 before Git Bash on PATH, so the resolved path must be probed
    and passed explicitly.
    """
    bash = shutil.which('bash')
    if not bash:
        return None
    try:
        probe = subprocess.run(
            [bash, '-c', 'echo ok'], capture_output=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0 or b'ok' not in probe.stdout:
        return None
    return bash


BASH = _usable_bash()


def _script_text() -> str:
    return FIX_SCRIPT_PATH.read_text(encoding='utf-8')


def _code_lines() -> List[str]:
    """Script lines with full-line comments and blank lines removed."""
    lines = []
    for raw in _script_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            continue
        lines.append(raw)
    return lines


class TestGrubFixScriptLanding:
    """The auto_repair flip and the fix script must land together."""

    def test_fix_script_exists(self) -> None:
        assert FIX_SCRIPT_PATH.exists(), (
            'grub_fix.sh must exist: RepairOrchestrator.validate() fails '
            'preflight for ALL categories if an auto_repair category lacks '
            'its script'
        )

    def test_fix_script_not_empty(self) -> None:
        assert FIX_SCRIPT_PATH.stat().st_size > 0

    def test_grub_yaml_auto_repair_enabled(self) -> None:
        with open(GRUB_YAML_PATH, encoding='utf-8') as f:
            rules = yaml.safe_load(f)
        assert rules['category'] == 'grub'
        assert rules['auto_repair'] is True


class TestGrubFixScriptSyntax:

    @pytest.mark.skipif(BASH is None,
                        reason='no usable bash on this machine')
    def test_bash_syntax_check(self) -> None:
        # Feed the script via stdin so Windows path translation can never
        # break the invocation.
        proc = subprocess.run(
            [BASH, '-n'],
            input=FIX_SCRIPT_PATH.read_bytes(),
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, (
            f'bash -n failed: {proc.stderr.decode(errors="replace")}'
        )

    def test_has_shebang(self) -> None:
        assert _script_text().startswith('#!/bin/bash')

    def test_uses_lf_line_endings(self) -> None:
        # CRLF would break bash keyword parsing on the rescue VM.
        assert b'\r' not in FIX_SCRIPT_PATH.read_bytes()


class TestGrubFixScriptMarkerProtocol:
    """Serial-marker contract enforced on the script text."""

    def test_emits_repair_line_marker(self) -> None:
        assert 'GCE-REPAIR-LINE:' in _script_text()

    def test_emits_repair_result_marker(self) -> None:
        assert 'GCE-REPAIR-RESULT:' in _script_text()

    def test_markers_go_to_stderr(self) -> None:
        text = _script_text()
        assert re.search(r'GCE-REPAIR-LINE:\$1"\s*>&2', text)
        assert re.search(r'GCE-REPAIR-RESULT:\$1"\s*>&2', text)

    def test_never_calls_exit(self) -> None:
        # 'exit' would abort the composed startup script, killing later fix
        # scripts and the completion marker (repair -> mount_failed).
        # Single-quoted segments are stripped first: an 'exit' inside an awk
        # program terminates awk, not the shell.
        for line in _code_lines():
            code = line.split('#')[0]
            code = re.sub(r"'[^']*'", "''", code)
            assert not re.search(r'\bexit\b', code), (
                f'fix scripts must never call exit: {line!r}'
            )

    def test_never_signals_completion(self) -> None:
        # The composer relocates the completion signal after all fixes.
        text = _script_text()
        assert 'GCE-RESCUE-COMPLETE' not in text
        assert 'signal_complete' not in text

    def test_single_result_emission_point(self) -> None:
        """All result calls sit in one mutually-exclusive block at the end.

        One SUCCESS branch plus three FAILED branches (FAIL_REASON,
        INSTALL_FAIL, both) - a grub-install failure must not mask a later
        regen failure, and vice versa. Exactly one branch runs at runtime.
        """
        lines = _script_text().split('\n')
        marker_idx = next(
            i for i, line in enumerate(lines)
            if 'Single result emission point' in line
        )
        calls = [
            (i, line) for i, line in enumerate(lines)
            if re.search(r'^\s*repair_result\s+"', line)
        ]
        success_calls = [c for _, c in calls if 'SUCCESS' in c]
        failed_calls = [c for _, c in calls if 'FAILED' in c]
        assert len(success_calls) == 1
        assert len(failed_calls) == 3
        assert len(calls) == 4
        # Single emission POINT: every call site lives after the final
        # emission-block comment, inside one if/elif/else chain.
        assert all(i > marker_idx for i, _ in calls)

    def test_no_issues_is_not_an_outcome(self) -> None:
        # Repair only runs when diagnosis flagged grub, so the script always
        # attempts the reinstall/regen: SUCCESS or FAILED only. (Comments
        # may explain this; only code lines are checked.)
        for line in _code_lines():
            assert 'NO_ISSUES' not in line.split('#')[0], (
                f'grub fix must not emit NO_ISSUES: {line!r}'
            )

    def test_success_reports_fix_counter(self) -> None:
        text = _script_text()
        assert re.search(r'repair_result\s+"SUCCESS:\$fixes"', text)
        assert 'fixes=$((fixes + 1))' in text

    def test_failed_reason_kept_single_line(self) -> None:
        # Tool output is squeezed to one line before it lands in the
        # FAILED reason (result markers must stay on one serial line).
        text = _script_text()
        assert 'last_line()' in text
        assert re.search(r'repair_result\s+"FAILED:\$FAIL_REASON"', text)

    def test_fix_lines_never_mention_fstab(self) -> None:
        # cli/repair.py prints an fstab-backup note whenever any fix line
        # contains 'fstab' — grub lines must not false-trigger it.
        for line in _code_lines():
            if re.search(r'^\s*repair_line\b', line):
                assert 'fstab' not in line.lower(), (
                    f'grub repair_line must not contain "fstab": {line!r}'
                )

    def test_no_premount_block(self) -> None:
        # grub repair is post-mount only; it must not lift code before the
        # base script's mount attempt.
        assert 'GCE-REPAIR-PREMOUNT' not in _script_text()


class TestGrubFixScriptBehavior:
    """Structural checks of the repair procedure itself."""

    def test_detects_distro_family_from_target_os_release(self) -> None:
        text = _script_text()
        assert '$SYSROOT/etc/os-release' in text
        for family_pattern in ('*debian*', '*rhel*', '*suse*'):
            assert family_pattern in text

    def test_unsupported_family_fails_without_touching_anything(self) -> None:
        text = _script_text()
        assert 'unsupported distro family' in text
        # The failure must be recorded via the FAIL_REASON gate, not by
        # emitting a result mid-script.
        assert re.search(r'FAIL_REASON="unsupported distro family', text)

    def test_detects_firmware_from_target_disk_esp(self) -> None:
        # The rescue image can boot in a different mode than the target
        # disk, so firmware detection must come from the TARGET disk's EFI
        # System Partition (PARTTYPE GUID), not the rescue VM's /sys.
        text = _script_text()
        assert 'c12a7328-f81f-11d2-ba4b-00a0c93ec93b' in text
        assert 'lsblk -pnro NAME,PARTTYPE' in text

    def test_os_prober_suppressed_in_chroot(self) -> None:
        # The rescue VM's own boot disk is attached during repair; os-prober
        # would bake dead menu entries for it into the target's grub.cfg
        # (observed live on Rocky 9).
        assert 'GRUB_DISABLE_OS_PROBER=true' in _script_text()

    def test_resolves_whole_disk_via_lsblk_pkname(self) -> None:
        text = _script_text()
        # -d/--nodeps is load-bearing: without it lsblk lists children too
        # and the parent walk loops forever on any partitioned disk.
        assert 'lsblk -dno pkname' in text
        assert 'lsblk -no pkname' not in text
        assert 'findmnt -no SOURCE' in text

    def test_mounts_separate_boot_and_efi_from_fstab(self) -> None:
        text = _script_text()
        assert 'fstab_device_for /boot' in text
        assert 'fstab_device_for /boot/efi' in text
        # UUID=/LABEL= specs resolved via blkid
        assert 'blkid -t' in text
        # Skip cleanly when already mounted
        assert 'mountpoint -q "$SYSROOT/boot"' in text
        assert 'mountpoint -q "$SYSROOT/boot/efi"' in text

    def test_unmounts_own_mounts_in_reverse_order(self) -> None:
        text = _script_text()
        umount_run = text.index('umount "$SYSROOT/run"')
        umount_efi = text.index('umount "$SYSROOT/boot/efi"')
        umount_boot = text.index('umount "$SYSROOT/boot"')
        assert umount_run < umount_efi < umount_boot
        # Only mounts the script itself created are undone.
        for flag in ('BOUND_RUN', 'MOUNTED_EFI', 'MOUNTED_BOOT'):
            assert f'"${flag}" -eq 1' in text

    def test_binds_run_for_chroot_tooling(self) -> None:
        assert 'mount --bind /run "$SYSROOT/run"' in _script_text()

    def test_backs_up_grub_cfg_before_regenerating(self) -> None:
        text = _script_text()
        assert 'gce-repair-backup' in text
        backup_pos = text.index('gce-repair-backup')
        regen_pos = text.index('Regenerating GRUB config')
        assert backup_pos < regen_pos, 'backup must happen before regen'

    def test_restores_backup_when_regeneration_fails(self) -> None:
        # A failed regen can leave a truncated grub.cfg; the script must put
        # the backup back (damage containment) while still reporting FAILED.
        text = _script_text()
        assert 'restored the pre-repair grub.cfg from backup' in text
        restore_pos = text.index('restored the pre-repair grub.cfg from backup')
        fail_pos = text.index('GRUB config regeneration failed')
        assert fail_pos < restore_pos, 'restore happens on the failure path'
        # The restore must NOT increment the fix count (not a [FIXED] line).
        assert '[FIXED] grub: Regeneration failed' not in text

    def test_runs_target_tools_inside_chroot(self) -> None:
        text = _script_text()
        assert 'chroot "$SYSROOT"' in text
        # Debian-family vs RHEL/SUSE-family tool split
        assert 'grub-install $DISK' in text
        assert 'grub2-install $DISK' in text
        assert 'update-grub' in text
        assert 'grub2-mkconfig -o $CFG_INSIDE' in text
        assert '/boot/grub2/grub.cfg' in text
        assert '/boot/grub/grub.cfg' in text

    def test_uefi_debian_uses_efi_directory(self) -> None:
        assert 'grub-install --efi-directory=/boot/efi' in _script_text()

    def test_uefi_rhel_family_regenerates_config_only(self) -> None:
        # grub2-install on RHEL/SUSE UEFI would clobber the signed
        # package-managed EFI binaries — the script must only regenerate
        # the config there and say so in a repair line.
        text = _script_text()
        assert 'regenerating config only' in text
        assert re.search(
            r'repair_line "grub: UEFI \$FAMILY system[^"]*'
            r'regenerating config only"', text
        )

    def test_guards_against_readonly_sysroot(self) -> None:
        text = _script_text()
        assert 'mountpoint -q "$SYSROOT"' in text
        assert 'read-only' in text

    def test_never_sources_target_os_release(self) -> None:
        # os-release comes from the damaged disk; it is parsed with sed,
        # never executed in the rescue VM's root shell.
        for line in _code_lines():
            assert not re.search(r'(^|\s)(\.|source)\s+"?\$SYSROOT/etc/os-release', line), (
                f'target os-release must not be sourced: {line!r}'
            )

    def test_copies_log_to_sysroot(self) -> None:
        assert '$SYSROOT/var/log/gce-repair.log' in _script_text()

    def test_defines_standard_helpers(self) -> None:
        text = _script_text()
        for helper in ('log()', 'repair_line()', 'repair_result()'):
            assert helper in text
