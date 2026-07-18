"""Tests for the initramfs fix script and its auto_repair wiring.

Mirrors the static-analysis style of TestFstabFixScript in test_repair.py:
the script is validated textually (plus a bash -n syntax check where a
bash interpreter is available) because the suite runs under pytest on
Windows and never executes GCP-side shell code.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import pytest
import yaml

FIX_PATH = (
    Path(__file__).parent.parent / 'startup_scripts' / 'fixes' / 'initramfs_fix.sh'
)
YAML_PATH = (
    Path(__file__).parent.parent / 'core' / 'diagnose_rules' / 'initramfs.yaml'
)


def _find_bash() -> Optional[str]:
    """Locate a usable bash (Git Bash on Windows, system bash elsewhere)."""
    candidates = [
        r'C:\Program Files\Git\bin\bash.exe',
        r'C:\Program Files\Git\usr\bin\bash.exe',
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    found = shutil.which('bash')
    # WSL's System32 bash.exe needs a configured distro; don't rely on it.
    if found and 'system32' not in found.lower():
        return found
    return None


BASH = _find_bash()


class TestInitramfsFixScript:
    """Validate that the initramfs fix script file is well-formed."""

    def test_fix_script_exists(self) -> None:
        """initramfs_fix.sh should exist in startup_scripts/fixes/."""
        assert FIX_PATH.exists(), f"Fix script not found at {FIX_PATH}"

    @pytest.mark.skipif(BASH is None, reason='no bash interpreter available')
    def test_fix_script_bash_syntax(self) -> None:
        """bash -n must accept the script (syntax check, no execution)."""
        script = str(FIX_PATH).replace('\\', '/')
        proc = subprocess.run(
            [BASH, '-n', script], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"bash -n failed: {proc.stderr}"

    def test_fix_script_emits_repair_markers(self) -> None:
        """Script should emit GCE-REPAIR-LINE and GCE-REPAIR-RESULT markers."""
        content = FIX_PATH.read_text()
        assert 'GCE-REPAIR-LINE:' in content
        assert 'GCE-REPAIR-RESULT:' in content

    def test_fix_script_markers_go_to_stderr(self) -> None:
        """Marker echo lines must redirect to stderr (>&2)."""
        content = FIX_PATH.read_text()
        assert 'echo "GCE-REPAIR-LINE:$1" >&2' in content
        assert 'echo "GCE-REPAIR-RESULT:$1" >&2' in content

    def test_fix_script_never_calls_exit(self) -> None:
        """'exit' would abort the whole startup script - must never appear."""
        content = FIX_PATH.read_text()
        for lineno, line in enumerate(content.splitlines(), 1):
            code = line.split('#', 1)[0]
            assert not re.search(r'\bexit\b', code), (
                f"'exit' found on line {lineno}: {line.strip()}"
            )

    def test_fix_script_has_single_result_call_site(self) -> None:
        """Exactly one repair_result invocation (single final emission)."""
        content = FIX_PATH.read_text()
        calls = [
            line for line in content.splitlines()
            if re.match(r'\s*repair_result\b', line) and '()' not in line
        ]
        assert len(calls) == 1, f"Expected 1 repair_result call, got: {calls}"

    def test_fix_script_result_statuses(self) -> None:
        """Both SUCCESS and FAILED outcomes must be reachable."""
        content = FIX_PATH.read_text()
        assert 'SUCCESS:$fixes' in content
        assert 'FAILED:' in content

    def test_fix_script_no_completion_signal(self) -> None:
        """Must never emit the rescue completion marker - the composer
        relocates signal_complete after all fixes."""
        content = FIX_PATH.read_text()
        assert 'GCE-RESCUE-COMPLETE' not in content
        assert 'signal_complete' not in content

    def test_fix_script_has_no_premount_block(self) -> None:
        """initramfs repair is post-mount only (chroot-based)."""
        content = FIX_PATH.read_text()
        assert 'GCE-REPAIR-PREMOUNT' not in content

    def test_fix_script_detects_distro_family(self) -> None:
        """Family detection must read the TARGET's os-release."""
        content = FIX_PATH.read_text()
        assert '/etc/os-release' in content
        assert 'ID_LIKE' in content
        for family in ('debian', 'rhel', 'suse'):
            assert family in content

    def test_fix_script_uses_per_family_rebuild_tools(self) -> None:
        """update-initramfs (Debian) and dracut -f (RHEL/SUSE) via chroot."""
        content = FIX_PATH.read_text()
        assert 'update-initramfs -u -k' in content
        assert 'update-initramfs -c -k' in content
        assert 'dracut -f' in content
        assert 'chroot "$SYSROOT"' in content

    def test_fix_script_per_family_initrd_names(self) -> None:
        """Debian initrd.img-VER, RHEL initramfs-VER.img, SUSE initrd-VER."""
        content = FIX_PATH.read_text()
        assert '/boot/initrd.img-${kver}' in content
        assert '/boot/initramfs-${kver}.img' in content
        assert '/boot/initrd-${kver}' in content

    def test_fix_script_enumerates_installed_kernels(self) -> None:
        """Kernel versions come from /lib/modules, newest via sort -V."""
        content = FIX_PATH.read_text()
        assert 'lib/modules' in content
        assert 'sort -V' in content

    def test_fix_script_reports_skipped_older_kernels(self) -> None:
        """Only the newest kernel is rebuilt; older ones get a repair line."""
        content = FIX_PATH.read_text()
        assert 'older kernel(s) not rebuilt' in content

    def test_fix_script_mounts_separate_boot_partitions(self) -> None:
        """Separate /boot and /boot/efi must be mounted from the target
        fstab before the chroot rebuild."""
        content = FIX_PATH.read_text()
        assert 'mount_sysroot_boot_entry /boot' in content
        assert 'mount_sysroot_boot_entry /boot/efi' in content
        assert '/etc/fstab' in content

    def test_fix_script_remaps_dev_specs_to_rescued_disk(self) -> None:
        """Bare /dev/* fstab specs must be remapped via the by-id links -
        the target's /dev/sdX names point at other disks on the rescue VM."""
        content = FIX_PATH.read_text()
        assert '/dev/disk/by-id/google-${disk}-part' in content

    def test_fix_script_binds_run(self) -> None:
        """/run should be bind-mounted for tools that expect it."""
        content = FIX_PATH.read_text()
        assert 'mount -o bind /run' in content

    def test_fix_script_unmounts_in_reverse_order(self) -> None:
        """Mounts are tracked newest-first and unwound after the repair."""
        content = FIX_PATH.read_text()
        assert 'INITRAMFS_UNWIND_MOUNTS' in content
        assert 'unwind_mounts' in content
        # cleanup must run before the final result is reported
        assert content.rindex('unwind_mounts') < content.rindex('repair_result')

    def test_fix_script_creates_backup_before_rebuild(self) -> None:
        """Existing initrd is backed up before the rebuild overwrites it."""
        content = FIX_PATH.read_text()
        assert '.gce-repair-backup' in content
        backup_pos = content.index('.gce-repair-backup')
        rebuild_pos = content.index('tool_output=$(chroot')
        assert backup_pos < rebuild_pos, 'backup must happen before rebuild'

    def test_fix_script_has_free_space_guard(self) -> None:
        """df-based guard: skip the backup on a nearly-full /boot and name
        disk_full when the rebuild itself hits ENOSPC."""
        content = FIX_PATH.read_text()
        assert 'df -Pk' in content
        assert '-gt 90' in content
        assert 'disk_full' in content
        assert 'no space left on device' in content.lower()

    def test_fix_script_regenerates_grub_config(self) -> None:
        """After a rebuild, the GRUB config is regenerated (update-grub or
        grub2-mkconfig) so entries reference the new image."""
        content = FIX_PATH.read_text()
        assert 'update-grub' in content
        assert 'grub2-mkconfig' in content

    def test_fix_script_guards_read_only_sysroot(self) -> None:
        """The base script can fall back to a read-only mount - the fix
        must detect that and fail cleanly instead of half-writing."""
        content = FIX_PATH.read_text()
        assert 'read-only' in content

    def test_fix_script_does_not_source_target_files(self) -> None:
        """os-release is parsed with grep, never sourced (no code execution
        from the broken disk)."""
        content = FIX_PATH.read_text()
        assert not re.search(r'(?:^|\s)(?:source|\.)\s+"?\$SYSROOT', content, re.M)

    def test_fix_script_copies_log_to_sysroot(self) -> None:
        """Full log is copied to the affected disk so it survives restore."""
        content = FIX_PATH.read_text()
        assert 'gce-repair.log' in content


class TestInitramfsAutoRepair:
    """auto_repair wiring: YAML flag flipped with the script in place."""

    def test_yaml_auto_repair_is_true(self) -> None:
        """initramfs.yaml must declare auto_repair: true."""
        data = yaml.safe_load(YAML_PATH.read_text())
        assert data['auto_repair'] is True

    def test_yaml_stale_comment_rewritten(self) -> None:
        """The old 'MUST stay false' comment no longer applies."""
        assert 'MUST stay false' not in YAML_PATH.read_text()

    def test_initramfs_in_supported_fix_categories(self) -> None:
        """fix_catalog derives SUPPORTED_FIX_CATEGORIES from auto_repair."""
        from gce_rescue_v2.core.fix_catalog import SUPPORTED_FIX_CATEGORIES

        assert 'initramfs' in SUPPORTED_FIX_CATEGORIES

    def test_fix_script_exists_for_category(self) -> None:
        """Preflight requires the script for every auto_repair category."""
        assert FIX_PATH.exists(), (
            'initramfs is auto_repair:true but initramfs_fix.sh is missing - '
            'RepairOrchestrator.validate() would fail preflight for ALL '
            'categories'
        )

    def test_os_prober_suppressed_in_grub_regen(self) -> None:
        """The rescue VM's boot disk is attached during repair; os-prober
        would bake dead menu entries for it into the target's config
        (observed live on Rocky 9)."""
        content = FIX_PATH.read_text()
        assert content.count('GRUB_DISABLE_OS_PROBER=true') >= 2
