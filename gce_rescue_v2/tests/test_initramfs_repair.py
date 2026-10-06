"""Tests for automated initramfs repair.

Covers:
  - kernel version extraction from the serial console (core/diagnosis.py)
  - repair orchestration wiring (fixable gating, GCE_FAILING_KERNEL)
  - startup-script composition (compose.py)
  - diagnose report output
  - startup_scripts/fixes/initramfs_fix.sh, run in bash against fake root
    filesystems. chroot, timeout, mount and mountpoint are replaced by shell
    functions, and the target's tools (dracut, lsinitrd, grub2-mkconfig,
    grubby, ...) are simulated, so every decision path of the script runs
    without root privileges and without touching the host.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gce_rescue_v2.core.diagnosis import (
    KERNEL_VERSION_CATEGORIES,
    analyze_serial_output,
    extract_grub_initrd_kernel_version,
    extract_kernel_version,
    extract_root_device,
    initramfs_kernel_versions,
    is_safe_kernel_version,
    is_safe_root_device,
    latest_boot_completed,
)
from gce_rescue_v2.core.fix_catalog import (
    INITRAMFS_REBUILD_PATTERNS,
    SUPPORTED_FIX_CATEGORIES,
)
from gce_rescue_v2.orchestration.compose import compose_startup_script
from gce_rescue_v2.orchestration.repair import RepairOrchestrator
from gce_rescue_v2.utils.report_formatter import DiagnosisReportFormatter


SCRIPT = (Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
          / 'initramfs_fix.sh')

RHEL9 = '5.14.0-427.13.1.el9_4.x86_64'
RHEL9_OLD = '5.14.0-362.8.1.el9_3.x86_64'
DEB_NEW = '6.1.0-53-cloud-amd64'
DEB_OLD = '6.1.0-37-cloud-amd64'
SLES_NEW = '6.4.0-150700.53.81-default'
SLES_OLD = '6.4.0-150700.51.1-default'

PANIC = ('Kernel panic - not syncing: VFS: Unable to mount root fs on '
         'unknown-block(0,0)')

# Rocky Linux 9 with the default kernel's initramfs removed (lab, serial
# console, CR stripped). GRUB wraps the long file name; no kernel runs.
ROCKY9_GRUB = '5.14.0-687.44.1.el9_8.x86_64'
GRUB_WRAPPED_SERIAL = (
    "    Booting `Rocky Linux (5.14.0-687.44.1.el9_8.x86_64) 9.8 (Blue Onyx)'\n"
    "error: ../../grub-core/fs/fshelp.c:257:file\n"
    "`/boot/initramfs-5.14.0-687.44.1.el9_8.x86_6\n"
    "4.img' not found.\n"
    "error: ../../grub-core/fs/fshelp.c:257:file\n"
    "`/boot/initramfs-5.14.0-687.44.1.el9_8.x86_64.img' not found.\n"
    "  Failed to boot both default and fallback entries.\n"
    "Press any key to continue...\n"
)
GRUB_WRAPPED_MATCH = (
    "error: ../../grub-core/fs/fshelp.c:257:file\n"
    "`/boot/initramfs-5.14.0-687.44.1.el9_8.x86_6\n"
    "4.img' not found"
)


# ===========================================================================
# Kernel version extraction
# ===========================================================================

class TestIsSafeKernelVersion:

    @pytest.mark.parametrize('value', [
        RHEL9, DEB_NEW, SLES_NEW, '6.8.0-1015-gcp', '4.18.0-553.el8_10.x86_64',
        '5.15.0-1050-gcp+', '6.12.0~rc1', '3.10.0-1160.el7.x86_64', '6',
    ])
    def test_valid(self, value):
        assert is_safe_kernel_version(value)

    @pytest.mark.parametrize('value', [
        '', None, 123, 'abc', '-6.1', '6.1 0', '6.1;reboot', '6.1$(reboot)',
        '6.1`id`', '6.1"', "6.1'", '6.1/../../etc', '6.1\n', '6.1\\x',
        '6.1é', '6.' + 'a' * 200, '6.1|x', '6.1&', '6.1>x',
    ])
    def test_invalid(self, value):
        assert not is_safe_kernel_version(value)


class TestExtractKernelVersion:

    def test_banner_before_match(self):
        serial = (f'[0.0] Linux version {RHEL9} (mockbuild@x) #1 SMP\n'
                  f'[1.8] {PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_last_banner_before_match_wins(self):
        serial = (f'[0.0] Linux version {RHEL9_OLD} (x) #1\n'
                  f'[5.0] reboot: Restarting system\n'
                  f'[0.0] Linux version {RHEL9} (x) #1\n'
                  f'[1.8] {PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_banner_after_match_ignored(self):
        serial = (f'[0.0] Linux version {RHEL9_OLD} (x) #1\n'
                  f'[1.8] {PANIC}\n'
                  f'[0.0] Linux version {RHEL9} (x) #1\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9_OLD

    def test_boot_image_fallback_when_banner_rotated_out(self):
        serial = (f'[0.0] Command line: BOOT_IMAGE=(hd0,gpt2)/vmlinuz-{RHEL9} '
                  f'root=UUID=abc ro\n[1.8] {PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_boot_image_at_end_of_line(self):
        serial = f'BOOT_IMAGE=/boot/vmlinuz-{DEB_NEW}\n{PANIC}\n'
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == DEB_NEW

    def test_panic_comm_line_fallback(self):
        serial = (f'[1.8] {PANIC}\n'
                  f'[1.8] CPU: 0 PID: 1 Comm: swapper/0 Not tainted {DEB_NEW} '
                  f'#1 Debian 6.1.0\n')
        assert extract_kernel_version(serial, 0) == DEB_NEW

    def test_panic_comm_tainted_variant(self):
        serial = (f'{PANIC}\nCPU: 1 UID: 0 PID: 1 Comm: swapper/0 Tainted: G '
                  f'  W   {SLES_NEW} #1 SMP\n')
        assert extract_kernel_version(serial, 0) == SLES_NEW

    def test_panic_comm_of_next_boot_ignored(self):
        serial = (f'{PANIC}\n[0.0] Linux version {RHEL9} (x) #1\n'
                  f'CPU: 0 PID: 1 Comm: swapper/0 Not tainted {RHEL9} #1\n')
        assert extract_kernel_version(serial, 0) == ''

    def test_boot_image_of_same_boot_preferred_over_banner(self):
        """vmlinuz-<new> holding an older kernel binary: the banner shows
        the old version, BOOT_IMAGE names the entry GRUB loaded."""
        serial = (f'Linux version {RHEL9_OLD} (x) #1\n'
                  f'Command line: BOOT_IMAGE=(hd0,gpt2)/vmlinuz-{RHEL9} ro\n'
                  f'{PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_boot_image_of_earlier_boot_ignored(self):
        serial = (f'Command line: BOOT_IMAGE=/vmlinuz-{RHEL9_OLD} ro\n'
                  f'Linux version {RHEL9} (x) #1\n{PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_boot_image_without_version_keeps_banner(self):
        serial = (f'Linux version {DEB_NEW} (x) #1\n'
                  'Command line: BOOT_IMAGE=/vmlinuz root=UUID=x ro\n'
                  f'{PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == DEB_NEW

    def test_unsafe_boot_image_keeps_banner(self):
        serial = (f'Linux version {RHEL9} (x) #1\n'
                  'Command line: BOOT_IMAGE=/vmlinuz-6.1$(reboot) ro\n'
                  f'{PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_boot_image_after_match_ignored(self):
        serial = (f'Linux version {RHEL9} (x) #1\n{PANIC}\n'
                  f'Command line: BOOT_IMAGE=/vmlinuz-{RHEL9_OLD} ro\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_rescue_entry_boot_image_keeps_banner(self):
        serial = (f'Linux version {RHEL9} (x) #1\n'
                  'Command line: BOOT_IMAGE=(hd0,gpt2)/vmlinuz-0-rescue-'
                  '8d2855d06c374189a8113ca1842eabf6 ro\n'
                  f'{PANIC}\n')
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == RHEL9

    def test_nothing_found(self):
        assert extract_kernel_version(f'{PANIC}\n', 0) == ''

    @pytest.mark.parametrize('serial,pos', [('', 0), (None, 0), ('x', -1),
                                            ('x', None)])
    def test_degenerate_inputs(self, serial, pos):
        assert extract_kernel_version(serial, pos) == ''

    @pytest.mark.parametrize('version', [
        '6.1$(reboot)', '6.1`id`', '6.1;rm', "6.1'x", '6.1"x', '6.1|x',
    ])
    def test_injection_attempt_in_banner_rejected(self, version):
        serial = f'Linux version {version} (x) #1\n{PANIC}\n'
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == ''

    def test_injection_attempt_in_boot_image_rejected(self):
        serial = f'BOOT_IMAGE=/vmlinuz-6.1$(reboot) ro\n{PANIC}\n'
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == ''

    def test_overlong_version_rejected(self):
        serial = f'Linux version 6.{"1" * 300} (x)\n{PANIC}\n'
        assert extract_kernel_version(serial, serial.index('Kernel panic')) == ''



class TestIsSafeRootDevice:

    @pytest.mark.parametrize('value', [
        'UUID=0b3f1c2a-9d8e-4f00-8a11-2b3c4d5e6f70', 'PARTUUID=1234-abcd',
        'LABEL=cloudimg-rootfs', '/dev/sda1', '/dev/nvme0n1p1',
        '/dev/mapper/vg-root', '/dev/disk/by-uuid/abc', 'ZFS=rpool/ROOT',
    ])
    def test_valid(self, value):
        assert is_safe_root_device(value)

    @pytest.mark.parametrize('value', [
        '', None, 5, 'UUID=a b', 'UUID=a;reboot', 'UUID=$(id)', 'UUID=`id`',
        'UUID="x"', "UUID='x'", 'UUID=a\\b', 'UUID=a\n', 'x' * 201, 'UUID=é',
    ])
    def test_invalid(self, value):
        assert not is_safe_root_device(value)


class TestExtractRootDevice:
    CMD = ('[0.0] Kernel command line: BOOT_IMAGE=/boot/vmlinuz-6.1 '
           'root=UUID=11111111-2222-3333-4444-555555555555 ro console=ttyS0\n')

    def test_root_from_same_boot(self):
        serial = f'[0.0] Linux version {DEB_NEW} (x) #1\n{self.CMD}[1.8] {PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == \
            'UUID=11111111-2222-3333-4444-555555555555'

    def test_last_root_argument_wins(self):
        serial = ('Kernel command line: root=UUID=aaa ro root=UUID=bbb\n'
                  f'{PANIC}\n')
        assert extract_root_device(serial, serial.index('Kernel panic')) == 'UUID=bbb'

    def test_last_command_line_before_match(self):
        serial = ('Kernel command line: root=UUID=old ro\n'
                  'Kernel command line: root=UUID=new ro\n'
                  f'{PANIC}\n')
        assert extract_root_device(serial, serial.index('Kernel panic')) == 'UUID=new'

    def test_command_line_of_earlier_boot_ignored(self):
        """A 'Linux version' banner after the command line means a new boot
        started without printing (or rotating out) its own command line."""
        serial = ('Kernel command line: root=UUID=old ro\n'
                  f'[0.0] Linux version {DEB_NEW} (x) #1\n'
                  f'{PANIC}\n')
        assert extract_root_device(serial, serial.index('Kernel panic')) == ''

    def test_command_line_after_match_ignored(self):
        serial = f'{PANIC}\nKernel command line: root=UUID=later ro\n'
        assert extract_root_device(serial, 0) == ''

    def test_no_root_argument(self):
        serial = f'Kernel command line: BOOT_IMAGE=/vmlinuz ro quiet\n{PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == ''

    def test_quoted_value_unquoted(self):
        serial = f'Kernel command line: root="UUID=abc" ro\n{PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == 'UUID=abc'

    def test_device_path(self):
        serial = f'Kernel command line: root=/dev/sda1 ro\n{PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == '/dev/sda1'

    def test_sysroot_not_confused_with_root(self):
        serial = f'Kernel command line: rd.sysroot=/x ro\n{PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == ''

    @pytest.mark.parametrize('serial,pos', [('', 0), (None, 0), ('x', -1),
                                            ('x', None)])
    def test_degenerate_inputs(self, serial, pos):
        assert extract_root_device(serial, pos) == ''

    @pytest.mark.parametrize('bad', ['UUID=$(reboot)', 'UUID=`id`',
                                     'UUID=a;b', "UUID=a'b"])
    def test_injection_attempt_rejected(self, bad):
        serial = f'Kernel command line: root={bad} ro\n{PANIC}\n'
        assert extract_root_device(serial, serial.index('Kernel panic')) == ''


class TestAnalyzeSerialOutputKernelVersion:

    def test_initramfs_finding_carries_kernel_version(self):
        serial = (f'[0.0] Linux version {RHEL9} (x) #1 SMP\n'
                  '[1.6] VFS: Cannot open root device "sda1" or '
                  'unknown-block(0,0): error -6\n'
                  f'[1.8] {PANIC}\n')
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        initramfs = [e for e in result.boot_errors if e.category == 'initramfs']
        assert initramfs
        assert all(e.kernel_version == RHEL9 for e in initramfs)

    def test_failing_boot_version_used_with_multiple_boots(self):
        serial = (f'[0.0] Linux version {RHEL9_OLD} (x) #1 SMP\n'
                  '[9.0] systemd[1]: Started Journal Service.\n'
                  f'[0.0] Linux version {RHEL9} (x) #1 SMP\n'
                  f'[1.8] {PANIC}\n')
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        versions = {e.kernel_version for e in result.boot_errors
                    if e.category == 'initramfs'}
        assert versions == {RHEL9}

    def test_initramfs_finding_carries_root_device(self):
        serial = (f'[0.0] Linux version {RHEL9} (x) #1 SMP\n'
                  f'[0.0] Kernel command line: BOOT_IMAGE=/vmlinuz-{RHEL9} '
                  'root=UUID=11111111-2222-3333-4444-555555555555 ro\n'
                  f'[1.8] {PANIC}\n')
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        initramfs = [e for e in result.boot_errors if e.category == 'initramfs']
        assert initramfs
        assert all(e.root_device == 'UUID=11111111-2222-3333-4444-555555555555'
                   for e in initramfs)

    def test_non_initramfs_findings_have_no_kernel_version(self):
        serial = (f'[0.0] Linux version {RHEL9} (x) #1 SMP\n'
                  '[5.0] Kernel panic - not syncing: Attempted to kill init! '
                  'exitcode=0x00000009\n')
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        assert result.boot_errors
        for e in result.boot_errors:
            if e.category not in KERNEL_VERSION_CATEGORIES:
                assert e.kernel_version == ''
                assert e.root_device == ''

    def test_kernel_version_in_diagnose_dict(self):
        from gce_rescue_v2.operations.diagnose import DiagnoseOperation
        serial = f'[0.0] Linux version {DEB_NEW} (x) #1\n[1.8] {PANIC}\n'
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.return_value = {
            'status': 'RUNNING',
            'disks': [{'boot': True, 'deviceName': 'boot',
                       'source': 'projects/p/zones/z/disks/boot',
                       'licenses': [
                           'projects/debian-cloud/global/licenses/debian-12']}],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute.instances.return_value.getSerialPortOutput.return_value \
            .execute.return_value = {'contents': serial}
        op = DiagnoseOperation(compute, 'p', 'z',
                               logging.getLogger('test_initramfs_repair'))
        errors = op.execute('vm').rollback_data['boot_errors']
        assert any(e['category'] == 'initramfs'
                   and e['kernel_version'] == DEB_NEW for e in errors)
        assert all('root_device' in e for e in errors)


class TestGrubInitrdNotFound:

    def test_wrapped_rocky9_serial_detected_with_version(self):
        result = analyze_serial_output(GRUB_WRAPPED_SERIAL, 'vm', 'zone',
                                       'RUNNING', 'linux')
        grub = [e for e in result.boot_errors
                if e.name == 'grub_kernel_not_found']
        assert len(grub) == 1
        assert grub[0].kernel_version == ROCKY9_GRUB
        assert grub[0].root_device == ''

    def test_wrapped_serial_gives_initramfs_repair(self):
        result = analyze_serial_output(GRUB_WRAPPED_SERIAL, 'vm', 'zone',
                                       'RUNNING', 'linux')
        d = {'boot_errors': [
            {'name': e.name, 'category': e.category,
             'detected_pattern': e.detected_pattern,
             'kernel_version': e.kernel_version}
            for e in result.boot_errors]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['initramfs']
        assert orch._extract_failing_kernel(d) == ROCKY9_GRUB

    def test_version_ignores_banner_of_earlier_boot(self):
        """GRUB failed before any kernel ran; the last banner is stale."""
        serial = (f'[0.0] Linux version {RHEL9_OLD} (x) #1 SMP\n'
                  '[9.0] reboot: Restarting system\n' + GRUB_WRAPPED_SERIAL)
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        grub = [e for e in result.boot_errors
                if e.name == 'grub_kernel_not_found']
        assert grub and grub[0].kernel_version == ROCKY9_GRUB

    @pytest.mark.parametrize('text,expected', [
        (GRUB_WRAPPED_MATCH, ROCKY9_GRUB),
        (f"error: file `/boot/initramfs-{RHEL9}.img' not found", RHEL9),
        (f"error: file `/boot/initrd.img-{DEB_NEW}' not found", DEB_NEW),
        (f"error: file `/initrd.img-{DEB_NEW}' not found", DEB_NEW),
        (f"error: file `/boot/initrd-{SLES_NEW}' not found", SLES_NEW),
        (f"error: file '/boot/initramfs-{RHEL9}.img' not found", RHEL9),
        (f"error: file `/boot/vmlinuz-{RHEL9}' not found", ''),
        ("error: file `/boot/initrd.img' not found", ''),
        ("error: file `/boot/initrd.img-6.1$(reboot)' not found", ''),
        ('', ''),
        (None, ''),
    ])
    def test_extract_version_from_file_name(self, text, expected):
        assert extract_grub_initrd_kernel_version(text) == expected

    @pytest.mark.parametrize('line', [
        f"error: file `/boot/initrd.img-{DEB_NEW}' not found.",
        f"error: ../../grub-core/fs/fshelp.c:258:file "
        f"`/initramfs-{RHEL9}.img' not found.",
        "error: ../../grub-core/fs/fshelp.c:257:file\n"
        f"`/boot/initramfs-{RHEL9}.img' not found.",
    ])
    def test_unwrapped_and_split_variants_detected(self, line):
        result = analyze_serial_output(line + '\n', 'vm', 'zone', 'RUNNING',
                                       'linux')
        assert any(e.name == 'grub_kernel_not_found'
                   for e in result.boot_errors)

    def test_versions_combined_and_deduplicated(self):
        errors = [
            {'name': 'grub_kernel_not_found', 'category': 'grub',
             'kernel_version': RHEL9},
            {'name': 'initramfs_no_root_fs', 'category': 'initramfs',
             'kernel_version': RHEL9},
            {'name': 'initramfs_dracut_timeout', 'category': 'initramfs',
             'kernel_version': RHEL9_OLD},
            {'name': 'grub_rescue_prompt', 'category': 'grub',
             'kernel_version': DEB_NEW},
            {'name': 'fstab_x', 'category': 'fstab', 'kernel_version': DEB_OLD},
            {'name': 'initramfs_x', 'category': 'initramfs',
             'kernel_version': '6.1;reboot'},
        ]
        assert initramfs_kernel_versions(errors) == [RHEL9, RHEL9_OLD]

    @pytest.mark.parametrize('errors', [None, [], [{'category': 'initramfs'}]])
    def test_versions_degenerate(self, errors):
        assert initramfs_kernel_versions(errors) == []


class TestInitrdAssertFailed:
    # Rocky 9, lab: udev dracut modules omitted via /etc/dracut.conf.d.
    SERIAL = (
        f'[    0.000000] Linux version {RHEL9} (x) #1 SMP\n'
        '[  OK  ] Reached target Remote File Systems.\n'
        '[ASSERT] Assertion failed for Initrd Root File System.\n'
        '[DEPEND] Dependency failed for Mountpoints Configured in the Real Root.\n'
        '[ASSERT] Assertion failed for Initrd File Systems.\n'
        '[  OK  ] Started Emergency Shell.\n'
        '[  OK  ] Reached target Emergency Mode.\n'
        '/bin/dracut-emer/bin/dracut-emergency: line 7: //bin/dracut-emer\n'
    )

    def test_detected_as_initramfs_with_version(self):
        result = analyze_serial_output(self.SERIAL, 'vm', 'zone', 'RUNNING',
                                       'linux')
        found = [e for e in result.boot_errors
                 if e.name == 'initramfs_initrd_assert_failed']
        assert len(found) == 1
        assert found[0].category == 'initramfs'
        assert found[0].kernel_version == RHEL9

    def test_detect_only(self):
        result = analyze_serial_output(self.SERIAL, 'vm', 'zone', 'RUNNING',
                                       'linux')
        d = {'boot_errors': [
            {'name': e.name, 'category': e.category,
             'detected_pattern': e.detected_pattern,
             'kernel_version': e.kernel_version}
            for e in result.boot_errors]}
        orch = _orchestrator()
        assert 'initramfs' not in orch.get_fixable_categories(d)
        assert 'initramfs' in orch.get_unfixable_categories(d)

    def test_condition_skip_lines_not_matched(self):
        serial = ('[  OK  ] Reached target Multi-User System.\n'
                  'initrd-root-fs.target: Condition check resulted in Initrd '
                  'Root File System being skipped.\n')
        result = analyze_serial_output(serial, 'vm', 'zone', 'RUNNING', 'linux')
        assert not any(e.name == 'initramfs_initrd_assert_failed'
                       for e in result.boot_errors)


# ===========================================================================
# Orchestration and composition
# ===========================================================================

def _orchestrator():
    compute = Mock()
    logger = logging.getLogger('test_initramfs_repair')
    logger.console_level = logging.WARNING
    return RepairOrchestrator(compute, 'proj', 'zone-a', 'vm-1', logger=logger)


def _err(name, category='initramfs', kernel_version='', pattern='',
         root_device=''):
    return {'name': name, 'category': category, 'severity': 'critical',
            'description': name, 'detected_pattern': pattern,
            'kernel_version': kernel_version, 'root_device': root_device}


class TestCatalog:

    def test_initramfs_supported(self):
        assert 'initramfs' in SUPPORTED_FIX_CATEGORIES

    def test_rebuild_allowlist(self):
        assert INITRAMFS_REBUILD_PATTERNS == {
            'initramfs_no_root_fs', 'initramfs_load_failure',
            'initramfs_dracut_timeout', 'initramfs_dracut_fatal',
            'initramfs_busybox_shell'}

    @pytest.mark.parametrize('name', [
        'initramfs_nvme_device_rename', 'initramfs_sysroot_mount_failed',
        'initramfs_dracut_emergency', 'initramfs_initrd_assert_failed'])
    def test_symptom_only_patterns_excluded(self, name):
        assert name not in INITRAMFS_REBUILD_PATTERNS

    def test_allowlist_names_exist_in_rules(self):
        import yaml
        rules = Path(__file__).parent.parent / 'core/diagnose_rules/initramfs.yaml'
        names = {p['name'] for p in yaml.safe_load(rules.read_text())['patterns']}
        assert INITRAMFS_REBUILD_PATTERNS <= names

    def test_kernel_category_stays_detect_only(self):
        assert 'kernel' not in SUPPORTED_FIX_CATEGORIES


class TestFixableGating:

    def test_rebuildable_finding_is_fixable(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs')]}
        orch = _orchestrator()
        assert 'initramfs' in orch.get_fixable_categories(d)
        assert 'initramfs' not in orch.get_unfixable_categories(d)

    @pytest.mark.parametrize('name', [
        'initramfs_nvme_device_rename', 'initramfs_sysroot_mount_failed',
        'initramfs_dracut_emergency', 'initramfs_initrd_assert_failed'])
    def test_symptom_only_finding_is_unfixable(self, name):
        d = {'boot_errors': [_err(name)]}
        orch = _orchestrator()
        assert 'initramfs' not in orch.get_fixable_categories(d)
        assert 'initramfs' in orch.get_unfixable_categories(d)

    def test_sysroot_plus_emergency_is_unfixable(self):
        d = {'boot_errors': [_err('initramfs_sysroot_mount_failed'),
                             _err('initramfs_dracut_emergency')]}
        assert 'initramfs' not in _orchestrator().get_fixable_categories(d)

    @pytest.mark.parametrize('name', sorted(INITRAMFS_REBUILD_PATTERNS))
    def test_each_allowlisted_finding_is_fixable(self, name):
        d = {'boot_errors': [_err(name)]}
        assert 'initramfs' in _orchestrator().get_fixable_categories(d)

    def test_nvme_rename_plus_rebuildable_is_fixable(self):
        d = {'boot_errors': [_err('initramfs_nvme_device_rename'),
                             _err('initramfs_dracut_timeout')]}
        orch = _orchestrator()
        assert 'initramfs' in orch.get_fixable_categories(d)
        assert 'initramfs' not in orch.get_unfixable_categories(d)

    def test_kernel_panic_category_not_fixable(self):
        d = {'boot_errors': [_err('kernel_panic_generic', category='kernel')]}
        orch = _orchestrator()
        assert 'kernel' not in orch.get_fixable_categories(d)

    def test_initramfs_ordered_before_grub(self):
        d = {'boot_errors': [_err('grub_rescue', category='grub'),
                             _err('initramfs_no_root_fs')]}
        cats = _orchestrator().get_fixable_categories(d)
        assert cats.index('initramfs') < cats.index('grub')

    @pytest.mark.parametrize('pattern', [
        "error: file `/boot/initrd.img-6.1.0-53-cloud-amd64' not found.",
        "error: ../../grub-core/fs/fshelp.c:258:file "
        "`/initramfs-5.14.0-427.el9.x86_64.img' not found.",
        "error: file `/boot/initrd-6.4.0-150700.53.81-default' not found.",
    ])
    def test_grub_initrd_not_found_covered_by_initramfs(self, pattern):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub', pattern=pattern),
            _err('initramfs_no_root_fs')]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['initramfs']
        assert 'grub' not in orch.get_unfixable_categories(d)

    def test_grub_vmlinuz_not_found_still_fixed(self):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/vmlinuz-6.1.0-53' not found."),
            _err('initramfs_no_root_fs')]}
        assert _orchestrator().get_fixable_categories(d) == ['initramfs', 'grub']

    def test_other_grub_finding_keeps_grub(self):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/initrd.img-6.1' not found."),
            _err('grub_rescue_prompt', category='grub', pattern='grub rescue>'),
            _err('initramfs_no_root_fs')]}
        assert 'grub' in _orchestrator().get_fixable_categories(d)

    def test_grub_initrd_not_found_alone_triggers_initramfs(self):
        """RHEL 9 BLS: GRUB refuses the entry, no kernel runs, so the GRUB
        line is the only finding. The initramfs repair handles it."""
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/initrd.img-6.1' not found.")]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['initramfs']
        assert orch.get_unfixable_categories(d) == []

    def test_wrapped_grub_initrd_line_alone_triggers_initramfs(self):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern=GRUB_WRAPPED_MATCH)]}
        assert _orchestrator().get_fixable_categories(d) == ['initramfs']

    def test_grub_initrd_plus_symptom_only_initramfs_still_rebuilds(self):
        """The GRUB line proves the image is missing, so a symptom-only
        initramfs finding next to it does not cancel the rebuild."""
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/initrd.img-6.1' not found."),
            _err('initramfs_nvme_device_rename')]}
        assert _orchestrator().get_fixable_categories(d) == ['initramfs']

    def test_grub_vmlinuz_alone_keeps_grub(self):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/vmlinuz-6.1.0-53' not found.")]}
        assert _orchestrator().get_fixable_categories(d) == ['grub']

    def test_grub_initrd_only_with_initramfs_unsupported_keeps_grub(self):
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/initrd.img-6.1' not found.")]}
        with patch('gce_rescue_v2.orchestration.repair.SUPPORTED_FIX_CATEGORIES',
                   SUPPORTED_FIX_CATEGORIES - {'initramfs'}):
            assert _orchestrator().get_fixable_categories(d) == ['grub']

    # Device-mapper root (LVM / LUKS): the rescue flow mounts plain
    # partitions only, so the rebuild would end in a safe FAILED after a
    # full stop/rescue/restore cycle. Gate it out up front.

    @pytest.mark.parametrize('category', ['lvm', 'crypt'])
    def test_lvm_or_crypt_finding_blocks_rebuild(self, category):
        d = {'boot_errors': [_err('initramfs_dracut_timeout'),
                             _err('x', category=category)]}
        orch = _orchestrator()
        assert 'initramfs' not in orch.get_fixable_categories(d)
        unfixable = orch.get_unfixable_categories(d)
        assert 'initramfs' in unfixable and category in unfixable

    @pytest.mark.parametrize('root', ['/dev/mapper/rocky-root',
                                      '/dev/mapper/luks-1234', '/dev/dm-0'])
    def test_device_mapper_root_blocks_rebuild(self, root):
        d = {'boot_errors': [_err('initramfs_dracut_timeout',
                                  root_device=root)]}
        orch = _orchestrator()
        assert 'initramfs' not in orch.get_fixable_categories(d)
        assert 'initramfs' in orch.get_unfixable_categories(d)

    @pytest.mark.parametrize('root', ['/dev/sda1', '/dev/nvme0n1p1',
                                      'UUID=abcd-1234', 'LABEL=root',
                                      '/dev/disk/by-uuid/abcd', ''])
    def test_plain_root_not_affected(self, root):
        d = {'boot_errors': [_err('initramfs_dracut_timeout',
                                  root_device=root)]}
        orch = _orchestrator()
        assert 'initramfs' in orch.get_fixable_categories(d)
        assert 'initramfs' not in orch.get_unfixable_categories(d)

    def test_device_mapper_root_on_other_category_ignored(self):
        """Only initramfs findings carry the root= of the failing boot."""
        d = {'boot_errors': [
            _err('initramfs_no_root_fs'),
            _err('fs_x', category='filesystem',
                 root_device='/dev/mapper/vg-root')]}
        assert 'initramfs' in _orchestrator().get_fixable_categories(d)

    def test_grub_initrd_only_bypasses_device_mapper_gate(self):
        """GRUB refusing the image proves it is missing; no kernel ran, so
        the gate has nothing to judge. The script still refuses a
        non-mountable root safely."""
        d = {'boot_errors': [
            _err('grub_kernel_not_found', category='grub',
                 pattern="error: file `/boot/initrd.img-6.1' not found."),
            _err('initramfs_dracut_timeout',
                 root_device='/dev/mapper/vg-root')]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['initramfs']
        assert 'initramfs' not in orch.get_unfixable_categories(d)


class TestExtractFailingKernel:

    def test_single_version(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs', kernel_version=RHEL9),
                             _err('initramfs_dracut_timeout', kernel_version=RHEL9)]}
        assert _orchestrator()._extract_failing_kernel(d) == RHEL9

    def test_disagreeing_versions_give_empty(self):
        d = {'boot_errors': [_err('a', kernel_version=RHEL9),
                             _err('b', kernel_version=RHEL9_OLD)]}
        assert _orchestrator()._extract_failing_kernel(d) == ''

    def test_no_version(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs')]}
        assert _orchestrator()._extract_failing_kernel(d) == ''

    def test_unsafe_version_ignored(self):
        d = {'boot_errors': [_err('a', kernel_version='6.1$(reboot)')]}
        assert _orchestrator()._extract_failing_kernel(d) == ''

    def test_unsafe_ignored_safe_kept(self):
        d = {'boot_errors': [_err('a', kernel_version='6.1;x'),
                             _err('b', kernel_version=DEB_NEW)]}
        assert _orchestrator()._extract_failing_kernel(d) == DEB_NEW

    def test_other_categories_ignored(self):
        d = {'boot_errors': [_err('a', category='fstab', kernel_version=RHEL9_OLD),
                             _err('b', kernel_version=RHEL9)]}
        assert _orchestrator()._extract_failing_kernel(d) == RHEL9


class TestExtractRootDeviceFromDiagnosis:

    def test_single_value(self):
        d = {'boot_errors': [_err('a', root_device='UUID=x'),
                             _err('b', root_device='UUID=x')]}
        assert _orchestrator()._extract_root_device(d) == 'UUID=x'

    def test_disagreeing_values_give_empty(self):
        d = {'boot_errors': [_err('a', root_device='UUID=x'),
                             _err('b', root_device='UUID=y')]}
        assert _orchestrator()._extract_root_device(d) == ''

    def test_missing_gives_empty(self):
        d = {'boot_errors': [_err('a')]}
        assert _orchestrator()._extract_root_device(d) == ''

    def test_unsafe_ignored(self):
        d = {'boot_errors': [_err('a', root_device='UUID=$(id)'),
                             _err('b', root_device='/dev/sda1')]}
        assert _orchestrator()._extract_root_device(d) == '/dev/sda1'

    def test_other_categories_ignored(self):
        d = {'boot_errors': [_err('a', category='fstab', root_device='UUID=f'),
                             _err('b', root_device='UUID=i')]}
        assert _orchestrator()._extract_root_device(d) == 'UUID=i'


def _script(diagnosis):
    orch = _orchestrator()
    with patch.object(orch, '_load_base_script',
                      return_value='mount /dev/sdb1 /mnt/sysroot\nsignal_complete\n'):
        return orch._generate_repair_script(diagnosis)


class TestGenerateRepairScript:

    def test_initramfs_script_gets_failing_kernel(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs', kernel_version=RHEL9)]}
        script = _script(d)
        assert f'GCE_FAILING_KERNEL="{RHEL9}"' in script

    def test_initramfs_script_gets_boot_root(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs', kernel_version=RHEL9,
                                  root_device='UUID=abc')]}
        script = _script(d)
        assert 'GCE_BOOT_ROOT="UUID=abc"' in script
        assert script.index('GCE_BOOT_ROOT') < script.index('initramfs_repair')
        assert 'initramfs_repair' in script
        assert script.index('GCE_FAILING_KERNEL=') < script.index('initramfs_repair()')

    def test_unknown_kernel_injected_empty(self):
        d = {'boot_errors': [_err('initramfs_no_root_fs')]}
        script = _script(d)
        assert 'GCE_FAILING_KERNEL=""' in script

    def test_no_variable_without_initramfs(self):
        d = {'boot_errors': [_err('fstab_mount_failed', category='fstab',
                                  pattern='Failed to mount /data')]}
        script = _script(d)
        assert 'GCE_FAILING_KERNEL' not in script

    def test_no_variable_for_nvme_rename_only(self):
        d = {'boot_errors': [
            _err('initramfs_nvme_device_rename', kernel_version=RHEL9),
            _err('fstab_mount_failed', category='fstab',
                 pattern='Failed to mount /data')]}
        script = _script(d)
        assert 'GCE_FAILING_KERNEL' not in script
        assert 'initramfs_repair()' not in script


class TestComposeFailingKernel:

    BASE = 'mount /dev/sdb1 /mnt/sysroot\nsignal_complete\n'

    def test_none_omits_variable(self):
        out = compose_startup_script(self.BASE, ['echo fix'], [])
        assert 'GCE_FAILING_KERNEL' not in out

    def test_empty_string_injected(self):
        out = compose_startup_script(self.BASE, ['echo fix'], [],
                                     failing_kernel='')
        assert 'GCE_FAILING_KERNEL=""' in out

    def test_value_injected_before_fix_body(self):
        out = compose_startup_script(self.BASE, ['echo fixbody'], [],
                                     failing_kernel=DEB_NEW)
        assert f'GCE_FAILING_KERNEL="{DEB_NEW}"' in out
        assert out.index('GCE_FAILING_KERNEL') > out.index('REPAIR_TARGETS')
        assert out.index('GCE_FAILING_KERNEL') < out.index('echo fixbody')

    @pytest.mark.parametrize('bad', ['6.1"; reboot; "', '$(reboot)', '6.1\nreboot',
                                     '`id`', '../../x', 'abc'])
    def test_unsafe_value_injected_empty(self, bad):
        out = compose_startup_script(self.BASE, ['echo fix'], [],
                                     failing_kernel=bad)
        assert 'GCE_FAILING_KERNEL=""' in out
        assert 'reboot' not in out
        assert '`id`' not in out


class TestLatestBootCompleted:

    def test_marker_after_last_banner(self):
        serial = (f'Linux version {RHEL9} (x) #1\n{PANIC}\n'
                  f'Linux version {RHEL9} (x) #1\n'
                  '[  OK  ] Reached target Multi-User System multi-user.target\n')
        assert latest_boot_completed(serial)

    def test_marker_only_in_earlier_boot(self):
        serial = (f'Linux version {RHEL9} (x) #1\nStartup finished in 5s\n'
                  f'Linux version {RHEL9} (x) #1\n'
                  'dracut-initqueue[368]: Warning: dracut-initqueue: timeout\n')
        assert not latest_boot_completed(serial)

    def test_no_banner_searches_everything(self):
        assert latest_boot_completed('Startup finished in 5s\n')

    def test_empty(self):
        assert not latest_boot_completed('')
        assert not latest_boot_completed(None)


class TestBootVerificationWait:
    """_verify_boot_after_repair polls the serial console."""

    HANG = (f'[0.0] Linux version {RHEL9} (x) #1 SMP\n'
            '[5.0] systemd[1]: Starting dracut initqueue hook...\n')
    TIMEOUT = HANG + (
        '[141.7] dracut-initqueue[368]: Warning: dracut-initqueue: timeout, '
        'still waiting for following initqueue hooks:\n'
        '[141.8] dracut-initqueue[368]: Warning: dracut-initqueue: starting timeout scripts\n')
    DONE = HANG + '[12.0] systemd[1]: Startup finished in 12.0s (kernel) + 9s.\n'

    def _run(self, contents, categories):
        """Run the verifier with time.sleep stubbed; returns (result, sleeps,
        fetches). contents is the serial text returned by successive polls
        (the last one repeats)."""
        orch = _orchestrator()
        orch._os_type = 'linux'
        compute = Mock()
        it = iter(contents)
        last = {'v': contents[-1]}

        def _next():
            try:
                last['v'] = next(it)
            except StopIteration:
                pass
            return {'contents': last['v']}
        compute.instances.return_value.getSerialPortOutput.return_value \
            .execute.side_effect = lambda: _next()
        orch._create_tracked_client = lambda label: compute
        sleeps = []
        with patch('gce_rescue_v2.orchestration.repair.time.sleep',
                   side_effect=lambda s: sleeps.append(s)), \
                patch('gce_rescue_v2.orchestration.repair.sys.stdout'):
            result = orch._verify_boot_after_repair(categories)
        fetches = compute.instances.return_value.getSerialPortOutput \
            .return_value.execute.call_count
        return result, sum(sleeps), fetches

    def test_non_initramfs_waits_45s_once(self):
        result, waited, fetches = self._run([self.HANG], ['fstab'])
        assert waited == 45 and fetches == 1
        assert result['verified'] is True

    def test_no_categories_waits_45s_once(self):
        result, waited, fetches = self._run([self.DONE], None)
        assert waited == 45 and fetches == 1

    def test_initramfs_hang_waits_full_budget_then_reports_errors(self):
        from gce_rescue_v2.orchestration.repair import INITRAMFS_BOOT_WAIT_SECONDS
        # Hang for the first polls, dracut timeout printed later.
        result, waited, fetches = self._run(
            [self.HANG, self.HANG, self.HANG, self.TIMEOUT], ['initramfs'])
        assert waited == INITRAMFS_BOOT_WAIT_SECONDS
        assert result['verified'] is False
        assert any('initramfs' in e for e in result['errors'])

    def test_initramfs_quiet_boot_uses_full_budget(self):
        from gce_rescue_v2.orchestration.repair import INITRAMFS_BOOT_WAIT_SECONDS
        result, waited, fetches = self._run([self.HANG], ['initramfs'])
        assert waited == INITRAMFS_BOOT_WAIT_SECONDS
        assert result['verified'] is True

    def test_initramfs_stops_early_when_boot_completes(self):
        result, waited, fetches = self._run([self.HANG, self.DONE], ['initramfs'])
        assert waited == 60 and fetches == 2
        assert result['verified'] is True

    def test_initramfs_completed_at_first_look(self):
        result, waited, fetches = self._run([self.DONE], ['initramfs'])
        assert waited == 45 and fetches == 1
        assert result['verified'] is True

    def test_earlier_boot_marker_does_not_end_wait(self):
        old_ok = ('Linux version 1.0 (x) #1\nStartup finished in 5s\n')
        result, waited, fetches = self._run(
            [old_ok + self.HANG, old_ok + self.TIMEOUT], ['initramfs'])
        assert waited > 60
        assert result['verified'] is False


class TestComposeBootRoot:

    BASE = 'mount /dev/sdb1 /mnt/sysroot\nsignal_complete\n'

    def test_none_omits_variable(self):
        out = compose_startup_script(self.BASE, ['echo fix'], [], failing_kernel='')
        assert 'GCE_BOOT_ROOT' not in out

    def test_value_injected(self):
        out = compose_startup_script(self.BASE, ['echo fixbody'], [],
                                     failing_kernel='', root_device='UUID=abc')
        assert 'GCE_BOOT_ROOT="UUID=abc"' in out
        assert out.index('GCE_BOOT_ROOT') < out.index('echo fixbody')

    @pytest.mark.parametrize('bad', ['UUID="; reboot; "', '$(reboot)',
                                     'UUID=a\nreboot', '`id`', 'a b'])
    def test_unsafe_value_injected_empty(self, bad):
        out = compose_startup_script(self.BASE, ['echo fix'], [],
                                     failing_kernel='', root_device=bad)
        assert 'GCE_BOOT_ROOT=""' in out
        assert 'reboot' not in out and '`id`' not in out


class TestReportFormatter:

    def _diagnosis(self, errors):
        return {'vm_name': 'vm', 'zone': 'z', 'status': 'RUNNING',
                'os_type': 'linux', 'os_flavor': 'rocky-9',
                'architecture': 'x86_64', 'license_type': 'free',
                'diagnosis_status': 'boot_errors_detected',
                'boot_errors': errors, 'recommendations': []}

    def test_kernel_line_shown(self):
        err = _err('initramfs_no_root_fs', kernel_version=RHEL9, pattern=PANIC)
        err['suggested_fixes'] = []
        err['context_lines'] = [PANIC]
        err['matched_line_index'] = 0
        report = DiagnosisReportFormatter().format_report(self._diagnosis([err]))
        assert 'Kernel:' in report
        assert RHEL9 in report

    def test_auto_repair_offered_for_rebuildable(self):
        err = _err('initramfs_no_root_fs', pattern=PANIC)
        err.update(suggested_fixes=[], context_lines=[], matched_line_index=-1)
        report = DiagnosisReportFormatter().format_report(self._diagnosis([err]))
        assert 'Auto-repair (recommended):' in report

    @pytest.mark.parametrize('name', [
        'initramfs_nvme_device_rename', 'initramfs_sysroot_mount_failed',
        'initramfs_dracut_emergency', 'initramfs_initrd_assert_failed'])
    def test_auto_repair_not_offered_for_symptom_only(self, name):
        err = _err(name, pattern='/dev/sda1 does not exist')
        err.update(suggested_fixes=[], context_lines=[], matched_line_index=-1)
        report = DiagnosisReportFormatter().format_report(self._diagnosis([err]))
        assert 'Auto-repair' not in report


# ===========================================================================
# initramfs_fix.sh: static checks
# ===========================================================================

def _code_lines():
    for line in SCRIPT.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            yield stripped


class TestScriptStatic:

    def test_script_exists(self):
        assert SCRIPT.is_file()

    @pytest.mark.skipif(shutil.which('bash') is None, reason='bash required')
    def test_bash_syntax(self):
        subprocess.run(['bash', '-n', str(SCRIPT)], check=True)

    def test_never_calls_exit(self):
        code = '\n'.join(_code_lines())
        shell_only = re.sub(r"'[^']*'", "''", code)  # drop awk programs
        for line in shell_only.splitlines():
            assert not re.search(r'(^|[;&|{]\s*)exit\b', line), line

    def test_never_rebuilds_all_kernels(self):
        for line in _code_lines():
            assert '-k all' not in line, line
            assert '--regenerate-all' not in line, line

    def test_never_deletes_kernels_or_modules(self):
        for line in _code_lines():
            if line.startswith(('rm ', 'rm -')) or ' rm -' in line:
                assert 'vmlinuz' not in line, line
                assert 'modules' not in line, line

    def test_functions_are_namespaced(self):
        names = re.findall(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{',
                           SCRIPT.read_text(), re.MULTILINE)
        shared = {'log', 'repair_line', 'repair_result'}
        for name in names:
            assert name in shared or name.startswith('initramfs_'), name

    def test_uses_result_marker(self):
        text = SCRIPT.read_text()
        assert 'GCE-REPAIR-RESULT:' in text
        assert 'GCE-REPAIR-LINE:' in text

    def test_lib_only_hook(self):
        assert 'GCE_INITRAMFS_FIX_LIB_ONLY' in SCRIPT.read_text()


# ===========================================================================
# initramfs_fix.sh: behaviour against fake root filesystems
# ===========================================================================

# Sourced before each test body. Replaces everything that would touch the
# host with shell functions and simulates the target's tools. Inputs:
#   FAKE_TOOLS       space-separated commands "installed" in the target
#   FAKE_BUILD_FAIL  'all' or a kernel version whose build fails
#   FAKE_BUILD_BAD   build succeeds but writes an image without modules
#   FAKE_TMP_FAIL    build fails only when writing *.gce-rescue-new
#   FAKE_GRUB_FAIL   grub config generation writes garbage and fails
#   FAKE_GRUBBY_FAIL / FAKE_GRUBBY_LIE   grubby --set-default fails / no-ops
#   FAKE_DF_KB       free space reported for /boot
#   FAKE_TAGS        space-separated TAG=VALUE pairs "present" on some disk
#   FAKE_TYPES       filesystem TYPE values present (e.g. LVM2_member)
HARNESS = r'''
export LOGFILE="$WORK/log"
GCE_INITRAMFS_FIX_LIB_ONLY=1
source "$SCRIPT" 2>/dev/null
SYSROOT="$ROOT"

timeout() { shift; "$@"; }
mountpoint() { [ "$2" = "$SYSROOT" ]; }
mount() { return 1; }
sync() { :; }
df() {
    if [ -n "${FAKE_DF_KB:-}" ]; then
        printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n'
        printf 'fake 1 1 %s 1%% /\n' "$FAKE_DF_KB"
    else
        command df "$@"
    fi
}

fake_has() { case " $FAKE_TOOLS " in *" $1 "*) return 0 ;; esac; return 1; }

blkid() {
    echo "blkid $*" >> "$WORK/cmds"
    case "$*" in
        *"-s TYPE"*) [ -n "${FAKE_TYPES:-}" ] && printf '%s\n' $FAKE_TYPES ;;
        *" -t "*) case " ${FAKE_TAGS:-} " in *" ${!#} "*) echo /dev/fake1 ;; esac ;;
    esac
    return 0
}
lsblk() { echo "lsblk $*" >> "$WORK/cmds"; return 0; }

fake_build() {
    local out="$1" ver="$2"
    echo "BUILD $ver $out" >> "$WORK/builds"
    case "${FAKE_BUILD_FAIL:-}" in all|"$ver") return 1 ;; esac
    case "$out" in *.gce-rescue-new) [ -n "${FAKE_TMP_FAIL:-}" ] && return 1 ;; esac
    if [ -n "${FAKE_BUILD_BAD:-}" ]; then
        echo "garbage" > "$SYSROOT$out"
        return 0
    fi
    printf 'NEW\nusr/lib/modules/%s/kernel/fs/xfs.ko\n' "$ver" > "$SYSROOT$out"
}

fake_mkconfig() {
    local cfg="$1" v img
    if [ -n "${FAKE_GRUB_FAIL:-}" ]; then
        echo "garbage" > "$SYSROOT$cfg"
        return 1
    fi
    {
        echo "### BEGIN /etc/grub.d/10_linux ###"
        echo "menuentry 'Linux' \$menuentry_id_option 'gnulinux-simple-U1' {"
        echo "}"
        echo "submenu 'Advanced options' \$menuentry_id_option 'gnulinux-advanced-U1' {"
        for v in $(ls "$SYSROOT/boot" | sed -n 's/^vmlinuz-//p' | sort -rV); do
            echo "	menuentry 'Linux $v' \$menuentry_id_option 'gnulinux-$v-advanced-U1' {"
            echo "		linux /boot/vmlinuz-$v root=UUID=U1"
            for img in "initramfs-$v.img" "initrd.img-$v" "initrd-$v"; do
                [ -f "$SYSROOT/boot/$img" ] && echo "		initrd /boot/$img"
            done
            echo "	}"
        done
        echo "}"
        echo "### END /etc/grub.d/10_linux ###"
    } > "$SYSROOT$cfg"
}

chroot() {
    shift
    [ "$1" = /usr/bin/env ] && shift
    [ "$1" = -i ] && shift
    while [ $# -gt 0 ] && [[ "$1" == *=* ]]; do shift; done
    if [ "$1" = env ]; then
        shift
        while [ $# -gt 0 ] && [[ "$1" == *=* ]]; do shift; done
    fi
    echo "$*" >> "$WORK/cmds"
    case "$1" in
        /bin/sh) fake_has "$5" ;;
        dracut) fake_has dracut || return 127; fake_build "$3" "$4" ;;
        mkinitramfs) fake_has mkinitramfs || return 127; fake_build "$3" "$4" ;;
        lsinitrd|lsinitramfs)
            [ -f "$SYSROOT$2" ] || return 1
            [ "$(head -1 "$SYSROOT$2")" = "CORRUPT" ] && return 1
            cat "$SYSROOT$2" ;;
        grub2-mkconfig|grub-mkconfig) fake_mkconfig "$3" ;;
        update-grub) fake_mkconfig /boot/grub/grub.cfg ;;
        grubby)
            if [ "$2" = --set-default ]; then
                [ -n "${FAKE_GRUBBY_FAIL:-}" ] && return 1
                [ "${FAKE_GRUBBY_FAIL_FOR:-}" = "${3##*/vmlinuz-}" ] && return 1
                [ -n "${FAKE_GRUBBY_LIE:-}" ] && return 0
                echo "saved_entry=bls-${3##*/vmlinuz-}" > "$SYSROOT/boot/grub2/grubenv"
            elif [ "$2" = --default-kernel ]; then
                echo "/boot/vmlinuz-$(sed -n 's/^saved_entry=bls-//p' "$SYSROOT/boot/grub2/grubenv")"
            fi ;;
        grub2-set-default)
            echo "saved_entry=$2" > "$SYSROOT/boot/grub2/grubenv" ;;
        grub-set-default)
            echo "saved_entry=$2" > "$SYSROOT/boot/grub/grubenv" ;;
        grub2-editenv|grub-editenv) cat "$SYSROOT$2" ;;
        setfiles|run-parts) return 0 ;;
        *) return 127 ;;
    esac
}
'''

TOOLS = {
    'rhel': 'dracut lsinitrd grub2-mkconfig grubby grub2-set-default grub2-editenv',
    'rhel7': 'dracut lsinitrd grub2-mkconfig grub2-set-default grub2-editenv',
    'suse': 'dracut lsinitrd grub2-mkconfig grub2-set-default grub2-editenv',
    'debian': ('mkinitramfs lsinitramfs update-grub grub-mkconfig '
               'grub-set-default grub-editenv run-parts'),
}

OS_RELEASE = {
    'rhel': 'NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n',
    'rhel7': 'NAME="CentOS Linux"\nID="centos"\nID_LIKE="rhel fedora"\n',
    'suse': 'NAME="SLES"\nID="sles"\nID_LIKE="suse"\n',
    'debian': 'NAME="Debian GNU/Linux"\nID=debian\n',
}


def _image_name(family, ver):
    if family == 'debian':
        return f'initrd.img-{ver}'
    if family == 'suse':
        return f'initrd-{ver}'
    return f'initramfs-{ver}.img'


def valid_image(ver):
    return f'OLD\nusr/lib/modules/{ver}/kernel/fs/xfs.ko\n'


class FakeRoot:
    """A fake target root filesystem."""

    def __init__(self, tmp_path, family, kernels, images=None, bls=False,
                 modules=None, grub_default='0', bls_initrd=None):
        self.family = family
        self.root = tmp_path / 'root'
        self.work = tmp_path / 'work'
        self.work.mkdir()
        r = self.root
        (r / 'etc').mkdir(parents=True)
        (r / 'boot').mkdir()
        (r / 'var/log').mkdir(parents=True)
        (r / 'etc/os-release').write_text(OS_RELEASE[family])
        (r / 'etc/fstab').write_text('UUID=U1 / xfs defaults 0 0\n')
        (r / 'etc/default').mkdir()
        (r / 'etc/default/grub').write_text(
            f'GRUB_TIMEOUT=0\nGRUB_DEFAULT={grub_default}\n'
            'GRUB_CMDLINE_LINUX="console=ttyS0"\n')
        if family == 'debian':
            (r / 'boot/grub').mkdir()
            (r / 'usr/lib').mkdir(parents=True)
            os.symlink('usr/lib', r / 'lib')
        else:
            (r / 'boot/grub2').mkdir()
        modules = kernels if modules is None else modules
        for ver in kernels:
            (r / f'boot/vmlinuz-{ver}').write_text(f'kernel {ver}\n')
        for ver in modules:
            (r / f'usr/lib/modules/{ver}/kernel').mkdir(parents=True)
        images = {} if images is None else images
        for ver in kernels:
            state = images.get(ver, 'valid')
            path = r / 'boot' / _image_name(family, ver)
            if state == 'valid':
                path.write_text(valid_image(ver))
            elif state == 'corrupt':
                path.write_text('CORRUPT\n')
        if bls:
            entries = r / 'boot/loader/entries'
            entries.mkdir(parents=True)
            for ver in kernels:
                initrd = (bls_initrd or {}).get(
                    ver, f'initrd /{_image_name(family, ver)} $tuned_initrd')
                lines = [f'title Rocky ({ver})', f'version {ver}',
                         f'linux /vmlinuz-{ver}']
                if initrd:
                    lines.append(initrd)
                lines.append('options root=UUID=U1 ro')
                (entries / f'abc-{ver}.conf').write_text('\n'.join(lines) + '\n')
        self.kernels = list(kernels)
        self.modules = list(modules)

    def path(self, rel):
        return self.root / rel.lstrip('/')

    def image(self, ver):
        return self.root / 'boot' / _image_name(self.family, ver)

    def snapshot(self):
        """Map of every file/dir/link outside the backup and log dirs."""
        snap = {}
        for p in sorted(self.root.rglob('*')):
            rel = p.relative_to(self.root).as_posix()
            if rel.startswith(('var/backups', 'var/log')):
                continue
            if p.is_symlink():
                snap[rel] = ('link', os.readlink(p))
            elif p.is_dir():
                snap[rel] = ('dir',)
            else:
                snap[rel] = ('file', p.read_bytes())
        return snap

    def run(self, body='initramfs_repair', tools=None, **env):
        full_env = {
            'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
            'SCRIPT': str(SCRIPT), 'ROOT': str(self.root),
            'WORK': str(self.work),
            'FAKE_TOOLS': TOOLS[self.family] if tools is None else tools,
        }
        full_env.update({k: str(v) for k, v in env.items()})
        proc = subprocess.run(['bash', '-c', HARNESS + '\n' + body],
                              env=full_env, capture_output=True, text=True,
                              timeout=60)
        return Result(proc)

    def assert_kernels_and_modules_intact(self):
        for ver in self.kernels:
            assert self.path(f'boot/vmlinuz-{ver}').read_text() == f'kernel {ver}\n'
        for ver in self.modules:
            assert self.path(f'usr/lib/modules/{ver}/kernel').is_dir()

    def builds(self):
        f = self.work / 'builds'
        return f.read_text().splitlines() if f.exists() else []

    def backups(self):
        base = self.root / 'var/backups/gce-rescue'
        return sorted(p.name for p in base.rglob('*') if p.is_file()) \
            if base.exists() else []


class Result:

    def __init__(self, proc):
        self.stdout = proc.stdout
        self.stderr = proc.stderr
        self.returncode = proc.returncode
        results = re.findall(r'^GCE-REPAIR-RESULT:(.*)$', proc.stderr, re.M)
        self.results = results
        self.result = results[-1] if results else None
        self.lines = re.findall(r'^GCE-REPAIR-LINE:(.*)$', proc.stderr, re.M)

    @property
    def ok(self):
        return self.result is not None and self.result.startswith('SUCCESS:')

    def __repr__(self):
        return f'Result({self.result!r})\n{self.stderr}'


pytestmark_shell = pytest.mark.skipif(
    shutil.which('bash') is None, reason='bash required')


@pytestmark_shell
class TestScriptHelpers:

    @pytest.mark.parametrize('content,expected', [
        ('ID="rocky"\nID_LIKE="rhel centos fedora"\n', 'rhel'),
        ('ID="almalinux"\nID_LIKE="rhel centos fedora"\n', 'rhel'),
        ('ID="rhel"\n', 'rhel'),
        ('ID="centos"\nID_LIKE="rhel fedora"\n', 'rhel'),
        ('ID="ol"\nID_LIKE="fedora"\n', 'rhel'),
        ('ID=fedora\n', 'rhel'),
        ('ID=debian\n', 'debian'),
        ('ID=ubuntu\nID_LIKE=debian\n', 'debian'),
        ('ID="sles"\nID_LIKE="suse"\n', 'suse'),
        ('ID="opensuse-leap"\nID_LIKE="suse opensuse"\n', 'suse'),
        ('ID=arch\n', 'unknown'),
        ('ID=$(reboot)\n', 'unknown'),
    ])
    def test_os_family(self, tmp_path, content, expected):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path('etc/os-release').write_text(content)
        res = root.run('initramfs_os_family')
        assert res.stdout.strip() == expected

    def test_list_kernels_filters_and_sorts(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD, '5.14.0-70.el9.x86_64'])
        boot = root.path('boot')
        (boot / 'vmlinuz-0-rescue-abcdef').write_text('x')
        os.symlink(f'vmlinuz-{RHEL9}', boot / 'vmlinuz-link')
        (boot / 'vmlinuz-6.1;reboot').write_text('x')
        res = root.run('initramfs_list_kernels')
        assert res.stdout.split() == ['5.14.0-70.el9.x86_64', RHEL9_OLD, RHEL9]

    @pytest.mark.parametrize('family,expected', [
        ('rhel', f'/boot/initramfs-{RHEL9}.img'),
        ('debian', f'/boot/initrd.img-{RHEL9}'),
        ('suse', f'/boot/initrd-{RHEL9}'),
    ])
    def test_image_path_default(self, tmp_path, family, expected):
        root = FakeRoot(tmp_path, family, [RHEL9], images={RHEL9: 'missing'})
        res = root.run(f'initramfs_image_path {RHEL9} {family}')
        assert res.stdout.strip() == expected

    def test_image_path_existing_name_wins(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path(f'boot/initrd-{RHEL9}').write_text(valid_image(RHEL9))
        res = root.run(f'initramfs_image_path {RHEL9} rhel')
        assert res.stdout.strip() == f'/boot/initrd-{RHEL9}'

    def test_modules_dir_ignores_lib_symlink(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW], modules=[])
        res = root.run(f'initramfs_modules_dir {DEB_NEW}')
        assert res.stdout.strip() == ''

    def test_grub_cfg_path_full_esp_config(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        esp = root.path('boot/efi/EFI/rocky')
        esp.mkdir(parents=True)
        (esp / 'grub.cfg').write_text('### BEGIN /etc/grub.d/00_header ###\n')
        res = root.run('initramfs_grub_cfg_path rhel')
        assert res.stdout.strip() == '/boot/efi/EFI/rocky/grub.cfg'

    def test_grub_cfg_path_esp_stub_ignored(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        esp = root.path('boot/efi/EFI/rocky')
        esp.mkdir(parents=True)
        (esp / 'grub.cfg').write_text(
            "search --no-floppy --fs-uuid --set=dev U1\n"
            "set prefix=($dev)/boot/grub2\nconfigfile $prefix/grub.cfg\n")
        res = root.run('initramfs_grub_cfg_path rhel')
        assert res.stdout.strip() == '/boot/grub2/grub.cfg'

    def test_grub_cfg_path_debian(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW])
        assert root.run('initramfs_grub_cfg_path debian').stdout.strip() \
            == '/boot/grub/grub.cfg'

    def test_grub_entry_id_with_submenu(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW, DEB_OLD])
        res = root.run(f'fake_mkconfig /boot/grub/grub.cfg; '
                       f'initramfs_grub_entry_id /boot/grub/grub.cfg {DEB_OLD}')
        assert res.stdout.strip() == \
            f'gnulinux-advanced-U1>gnulinux-{DEB_OLD}-advanced-U1'

    def test_grub_entry_id_without_submenu(self, tmp_path):
        root = FakeRoot(tmp_path, 'suse', [SLES_NEW])
        root.path('boot/grub2/grub.cfg').write_text(
            f"menuentry 'SLES {SLES_NEW}' $menuentry_id_option "
            f"'gnulinux-{SLES_NEW}-advanced-U1' {{\n}}\n")
        res = root.run(f'initramfs_grub_entry_id /boot/grub2/grub.cfg {SLES_NEW}')
        assert res.stdout.strip() == f'gnulinux-{SLES_NEW}-advanced-U1'

    def test_grub_entry_id_missing(self, tmp_path):
        root = FakeRoot(tmp_path, 'suse', [SLES_NEW])
        root.path('boot/grub2/grub.cfg').write_text('### BEGIN /etc/grub.d\n')
        res = root.run(f'initramfs_grub_entry_id /boot/grub2/grub.cfg {SLES_NEW} '
                       f'|| echo NONE')
        assert res.stdout.strip() == 'NONE'

    @pytest.mark.parametrize('value,ok', [
        (RHEL9, True), ('6.1$(id)', False), ('', False), ('abc', False),
        ('6.1/x', False)])
    def test_safe_version(self, tmp_path, value, ok):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        res = root.run(f"initramfs_safe_version '{value}' && echo Y || echo N")
        assert res.stdout.strip() == ('Y' if ok else 'N')


@pytestmark_shell
class TestScriptGuards:

    def test_sysroot_not_mounted(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        before = root.snapshot()
        res = root.run('mountpoint() { return 1; }; initramfs_repair')
        assert res.result == f'FAILED:sysroot not mounted at {root.root}'
        assert root.snapshot() == before

    def test_not_a_root_filesystem(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        # A /boot partition mounted by mistake has no /etc and no modules.
        shutil.rmtree(root.path('etc'))
        res = root.run()
        assert res.result.startswith('FAILED:the mounted partition is not a Linux root')

    def test_missing_fstab_is_still_a_root(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/fstab').unlink()
        res = root.run()
        assert res.result.startswith('SUCCESS:')

    def test_unknown_distribution(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/os-release').write_text('ID=arch\n')
        before = root.snapshot()
        res = root.run()
        assert res.result == 'FAILED:unsupported Linux distribution'
        assert root.snapshot() == before

    def test_no_kernels(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [], modules=[RHEL9])
        res = root.run()
        assert res.result == 'FAILED:no installed kernel found in /boot'

    def test_separate_boot_not_mountable(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW], images={DEB_NEW: 'missing'})
        root.path('etc/fstab').write_text(
            'UUID=U1 / ext4 defaults 0 0\nLABEL=BOOT /boot ext4 defaults 0 2\n')
        before = root.snapshot()
        res = root.run()
        assert res.result == 'FAILED:could not mount the separate /boot partition'
        assert root.snapshot() == before
        assert root.builds() == []

    def test_exactly_one_result_marker(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True)
        res = root.run(FAKE_BUILD_FAIL='all')
        assert len(res.results) == 1


@pytestmark_shell
class TestScriptBootRootCheck:
    """GCE_BOOT_ROOT: root= of the failed boot must exist before anything
    is rebuilt."""

    def test_missing_uuid_fails_before_changing_anything(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'missing'}, bls=True)
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=dead',
                       FAKE_TAGS='UUID=U1 PARTUUID=P1')
        assert res.result.startswith('FAILED:the failed boot used root=UUID=dead, '
                                     'but no attached disk has that UUID')
        assert 'cannot fix this' in res.result
        assert root.snapshot() == before
        assert root.builds() == []
        assert root.backups() == []

    @pytest.mark.parametrize('spec', [
        'UUID=U1', 'uuid=U1', 'PARTUUID=P1', 'LABEL=root', 'PARTLABEL=rootfs',
        '/dev/disk/by-uuid/U1', '/dev/disk/by-partuuid/P1',
        '/dev/disk/by-label/root', '/dev/disk/by-partlabel/rootfs',
    ])
    def test_existing_tag_passes(self, tmp_path, spec):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT=spec,
                       FAKE_TAGS='UUID=U1 PARTUUID=P1 LABEL=root PARTLABEL=rootfs')
        assert res.ok, res

    @pytest.mark.parametrize('spec', [
        '/dev/sda1', '/dev/nvme0n1p1', '/dev/mapper/vg-root', 'ZFS=rpool/ROOT',
        '/dev/disk/by-id/google-disk-part1', '/dev/disk/by-path/pci-0000',
    ])
    def test_device_names_are_not_checked(self, tmp_path, spec):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT=spec, FAKE_TAGS='')
        assert res.ok, res
        assert 'not checked' in res.stderr

    def test_empty_spec_skips_check(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='', FAKE_TAGS='')
        assert res.ok, res
        assert 'blkid' not in (root.work / 'cmds').read_text()

    def test_unset_spec_skips_check(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_TAGS='')
        assert res.ok, res

    def test_missing_uuid_with_lvm_present_continues(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=inside-lv',
                       FAKE_TAGS='UUID=U1', FAKE_TYPES='xfs LVM2_member')
        assert res.ok, res
        assert 'LVM/LUKS/RAID devices exist; continuing' in res.stderr

    def test_missing_uuid_with_luks_present_continues(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=inside-luks',
                       FAKE_TAGS='UUID=U1', FAKE_TYPES='crypto_LUKS')
        assert res.ok, res

    def test_missing_uuid_with_raid_present_continues(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=inside-md',
                       FAKE_TAGS='UUID=U1', FAKE_TYPES='linux_raid_member')
        assert res.ok, res
        assert 'LVM/LUKS/RAID devices exist; continuing' in res.stderr

    def test_lsblk_second_opinion(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        body = 'lsblk() { echo ABCD-1234; }; initramfs_repair'
        res = root.run(body, GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=abcd-1234',
                       FAKE_TAGS='')
        assert res.ok, res

    def test_unsafe_spec_ignored(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=$(id)',
                       FAKE_TAGS='')
        assert res.ok, res
        assert 'Ignoring unexpected root= value' in res.stderr

    def test_missing_uuid_does_not_try_fallback(self, tmp_path):
        """A wrong root= breaks every kernel; the fallback would not boot."""
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True)
        res = root.run(GCE_FAILING_KERNEL=RHEL9, GCE_BOOT_ROOT='UUID=dead',
                       FAKE_TAGS='UUID=U1')
        assert res.result.startswith('FAILED:')
        assert not root.path('boot/grub2/grubenv').exists()
        assert not any('set the previous kernel' in l for l in res.lines)


@pytestmark_shell
class TestScriptConfigWarnings:

    def test_dracut_omit_drivers_reported(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/dracut.conf.d').mkdir()
        root.path('etc/dracut.conf.d/99-x.conf').write_text(
            '# comment\nomit_drivers+=" virtio_scsi sd_mod "\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok
        info = [l for l in res.lines if l.startswith('[INFO] initramfs: /etc/dracut.conf.d/99-x.conf')]
        assert len(info) == 1
        assert 'omit_drivers+=" virtio_scsi sd_mod "' in info[0]

    def test_dracut_omit_modules_reported(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/dracut.conf').write_text('omit_dracutmodules+=" lvm "\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert any('/etc/dracut.conf removes modules' in l for l in res.lines)

    def test_modprobe_install_false_reported(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/modprobe.d').mkdir()
        root.path('etc/modprobe.d/cis.conf').write_text(
            'install usb-storage /bin/false\ninstall vfat /bin/false\n'
            'blacklist virtio_blk\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        info = [l for l in res.lines if 'blocks a storage driver' in l]
        assert len(info) == 2
        assert all('usb-storage' not in l for l in info)

    def test_initramfs_tools_modules_dep_reported(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW], images={DEB_NEW: 'missing'})
        root.path('etc/initramfs-tools').mkdir()
        root.path('etc/initramfs-tools/initramfs.conf').write_text(
            'MODULES=dep\nCOMPRESS=zstd\n')
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW)
        assert any('limits the drivers' in l and 'MODULES=dep' in l for l in res.lines)

    def test_default_config_reports_nothing(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW], images={DEB_NEW: 'missing'})
        root.path('etc/initramfs-tools').mkdir()
        root.path('etc/initramfs-tools/initramfs.conf').write_text('MODULES=most\n')
        root.path('etc/modprobe.d').mkdir()
        root.path('etc/modprobe.d/x.conf').write_text('options kvm nested=1\n')
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW)
        assert res.ok
        assert not any(l.startswith('[INFO]') for l in res.lines)

    def test_warnings_do_not_count_as_fixes(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'})
        root.path('etc/dracut.conf').write_text('omit_drivers+=" sd_mod "\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        fixed = [l for l in res.lines if l.startswith('[FIXED]')]
        assert res.result == f'SUCCESS:{len(fixed)}'


@pytestmark_shell
class TestScriptRebuild:

    def test_rhel_missing_image_rebuilt(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'missing'}, bls=True)
        old_before = root.image(RHEL9_OLD).read_text()
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.image(RHEL9).read_text().startswith('NEW')
        assert root.image(RHEL9_OLD).read_text() == old_before
        assert root.builds() == [f'BUILD {RHEL9} /boot/initramfs-{RHEL9}.img']
        assert any('Rebuilt' in line for line in res.lines)
        assert root.path('boot/grub2/grub.cfg').exists()
        root.assert_kernels_and_modules_intact()

    def test_corrupt_image_replaced_and_kept_in_backup(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'corrupt'},
                        bls=True)
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.image(RHEL9).read_text().startswith('NEW')
        assert f'initramfs-{RHEL9}.img' in root.backups()

    def test_valid_image_built_aside_then_swapped(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], bls=True)
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.builds() == [
            f'BUILD {RHEL9} /boot/initramfs-{RHEL9}.img.gce-rescue-new']
        assert root.image(RHEL9).read_text().startswith('NEW')
        assert not root.path(f'boot/initramfs-{RHEL9}.img.gce-rescue-new').exists()
        assert f'initramfs-{RHEL9}.img' in root.backups()

    def test_only_failing_kernel_rebuilt(self, tmp_path):
        """The failing kernel is older than the newest: only it is rebuilt."""
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9_OLD: 'missing'}, bls=True)
        newest_before = root.image(RHEL9).read_text()
        res = root.run(GCE_FAILING_KERNEL=RHEL9_OLD)
        assert res.ok, res
        assert [b.split()[1] for b in root.builds()] == [RHEL9_OLD]
        assert root.image(RHEL9).read_text() == newest_before

    def test_no_failing_kernel_targets_newest(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'missing'}, bls=True)
        res = root.run(GCE_FAILING_KERNEL='')
        assert res.ok, res
        assert [b.split()[1] for b in root.builds()] == [RHEL9]

    def test_failing_kernel_not_installed_targets_newest(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'missing'}, bls=True)
        res = root.run(GCE_FAILING_KERNEL='5.14.0-1.el9.x86_64')
        assert res.ok, res
        assert [b.split()[1] for b in root.builds()] == [RHEL9]

    def test_unsafe_failing_kernel_ignored(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True)
        res = root.run(GCE_FAILING_KERNEL=f'{RHEL9};touch $ROOT/pwned')
        assert res.ok, res
        assert not root.path('pwned').exists()
        assert [b.split()[1] for b in root.builds()] == [RHEL9]

    def test_bls_missing_initrd_line_added(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True, bls_initrd={RHEL9: ''})
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        entry = root.path(f'boot/loader/entries/abc-{RHEL9}.conf').read_text()
        lines = entry.splitlines()
        assert lines[lines.index(f'linux /vmlinuz-{RHEL9}') + 1] == \
            f'initrd /initramfs-{RHEL9}.img'
        assert any('initrd line' in line for line in res.lines)

    def test_bls_lone_missing_image_replaced(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True, bls_initrd={RHEL9: 'initrd /initramfs-wrong.img'})
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        entry = root.path(f'boot/loader/entries/abc-{RHEL9}.conf').read_text()
        assert f'initrd /initramfs-{RHEL9}.img' in entry
        assert 'initramfs-wrong.img' not in entry

    def test_bls_multi_image_line_untouched(self, tmp_path):
        line = f'initrd /initramfs-{RHEL9}.img $tuned_initrd'
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True, bls_initrd={RHEL9: line})
        before = root.path(f'boot/loader/entries/abc-{RHEL9}.conf').read_text()
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.path(f'boot/loader/entries/abc-{RHEL9}.conf').read_text() == before

    def test_bls_prefix_preserved(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True, bls_initrd={RHEL9: ''})
        entry = root.path(f'boot/loader/entries/abc-{RHEL9}.conf')
        entry.write_text(entry.read_text().replace(
            f'linux /vmlinuz-{RHEL9}', f'linux /boot/vmlinuz-{RHEL9}'))
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert f'initrd /boot/initramfs-{RHEL9}.img' in entry.read_text()

    def test_debian_rebuild_with_mkinitramfs(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW, DEB_OLD],
                        images={DEB_NEW: 'corrupt'})
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW)
        assert res.ok, res
        assert root.builds() == [f'BUILD {DEB_NEW} /boot/initrd.img-{DEB_NEW}']
        assert 'mkinitramfs -o' in (root.work / 'cmds').read_text()
        cfg = root.path('boot/grub/grub.cfg').read_text()
        assert f'initrd /boot/initrd.img-{DEB_NEW}' in cfg
        assert not root.path('etc/default/grub.d/99-gce-rescue-fallback.cfg').exists()

    def test_sles_rebuild_uses_initrd_name(self, tmp_path):
        root = FakeRoot(tmp_path, 'suse', [SLES_NEW], images={SLES_NEW: 'missing'})
        res = root.run(GCE_FAILING_KERNEL=SLES_NEW)
        assert res.ok, res
        assert root.builds() == [f'BUILD {SLES_NEW} /boot/initrd-{SLES_NEW}']

    def test_rhel7_full_esp_config_regenerated(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel7', ['3.10.0-1160.el7.x86_64'],
                        images={'3.10.0-1160.el7.x86_64': 'missing'})
        esp = root.path('boot/efi/EFI/centos')
        esp.mkdir(parents=True)
        (esp / 'grub.cfg').write_text('### BEGIN /etc/grub.d/00_header ###\n')
        res = root.run()
        assert res.ok, res
        assert 'initramfs-3.10.0-1160.el7.x86_64.img' in (esp / 'grub.cfg').read_text()
        assert not root.path('boot/grub2/grub.cfg').exists()

    def test_grub_failure_restores_config(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True)
        root.path('boot/grub2/grub.cfg').write_text(
            '### BEGIN /etc/grub.d/10_linux ###\noriginal\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_GRUB_FAIL=1)
        # BLS entries reference the image, so the boot works without a new cfg.
        assert res.ok, res
        assert 'original' in root.path('boot/grub2/grub.cfg').read_text()

    def test_space_constrained_retry_in_place(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], bls=True)
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_TMP_FAIL=1, FAKE_DF_KB=0)
        assert res.ok, res
        assert [b.split()[2] for b in root.builds()] == [
            f'/boot/initramfs-{RHEL9}.img.gce-rescue-new',
            f'/boot/initramfs-{RHEL9}.img']
        assert root.image(RHEL9).read_text().startswith('NEW')

    def test_build_failure_with_space_does_not_retry(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], bls=True)
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_TMP_FAIL=1)
        assert not res.ok
        assert len(root.builds()) == 1
        assert root.snapshot() == before

    def test_selinux_relabel_runs_when_enforcing(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True)
        sel = root.path('etc/selinux/targeted/contexts/files')
        sel.mkdir(parents=True)
        (sel / 'file_contexts').write_text('')
        root.path('etc/selinux/config').write_text(
            'SELINUX=enforcing\nSELINUXTYPE=targeted\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9,
                       FAKE_TOOLS=TOOLS['rhel'] + ' setfiles')
        assert res.ok, res
        cmds = (root.work / 'cmds').read_text()
        assert 'setfiles -F /etc/selinux/targeted/contexts/files/file_contexts' in cmds
        assert f'/boot/initramfs-{RHEL9}.img' in cmds


@pytestmark_shell
class TestScriptFallback:

    def test_rhel_build_failure_falls_back_with_grubby(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        root.path('boot/grub2/grubenv').write_text(f'saved_entry=bls-{RHEL9}\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9)
        assert res.ok, res
        assert root.path('boot/grub2/grubenv').read_text() == \
            f'saved_entry=bls-{RHEL9_OLD}\n'
        # The failing kernel's original image is put back untouched.
        assert root.image(RHEL9).read_text() == 'CORRUPT\n'
        assert any(f'previous kernel {RHEL9_OLD}' in line for line in res.lines)
        root.assert_kernels_and_modules_intact()

    def test_rhel_fallback_sets_grub_default_saved_when_needed(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='0')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9)
        assert res.ok, res
        assert 'GRUB_DEFAULT=saved' in root.path('etc/default/grub').read_text()
        assert root.path('boot/grub2/grub.cfg').exists()

    def test_missing_modules_falls_back(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'missing'}, bls=True,
                        modules=[RHEL9_OLD], grub_default='saved')
        root.path('boot/grub2/grubenv').write_text('saved_entry=x\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.builds() == []
        assert 'kernel modules' in ' '.join(res.lines)

    def test_fallback_skips_kernel_with_invalid_image(self, tmp_path):
        mid = '5.14.0-400.el9.x86_64'
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, mid, RHEL9_OLD],
                        images={RHEL9: 'corrupt', mid: 'corrupt'}, bls=True,
                        grub_default='saved')
        root.path('boot/grub2/grubenv').write_text('saved_entry=x\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9)
        assert res.ok, res
        assert root.path('boot/grub2/grubenv').read_text() == \
            f'saved_entry=bls-{RHEL9_OLD}\n'

    def test_bad_build_output_restores_original_then_falls_back(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        root.path('boot/grub2/grubenv').write_text('saved_entry=x\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_BAD=1)
        assert res.ok, res
        assert root.image(RHEL9).read_text() == 'CORRUPT\n'

    def test_debian_fallback_uses_dropin_and_saved_entry(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW, DEB_OLD],
                        images={DEB_NEW: 'corrupt'})
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW, FAKE_BUILD_FAIL=DEB_NEW)
        assert res.ok, res
        dropin = root.path('etc/default/grub.d/99-gce-rescue-fallback.cfg')
        assert 'GRUB_DEFAULT=saved' in dropin.read_text()
        assert root.path('boot/grub/grubenv').read_text() == \
            f'saved_entry=gnulinux-advanced-U1>gnulinux-{DEB_OLD}-advanced-U1\n'
        # /etc/default/grub itself is not edited on Debian/Ubuntu.
        assert 'GRUB_DEFAULT=0' in root.path('etc/default/grub').read_text()
        assert any(line.startswith('[INFO]') for line in res.lines)

    def test_sles_fallback_edits_grub_default(self, tmp_path):
        root = FakeRoot(tmp_path, 'suse', [SLES_NEW, SLES_OLD],
                        images={SLES_NEW: 'corrupt'})
        res = root.run(GCE_FAILING_KERNEL=SLES_NEW, FAKE_BUILD_FAIL=SLES_NEW)
        assert res.ok, res
        assert 'GRUB_DEFAULT=saved' in root.path('etc/default/grub').read_text()
        assert 'GRUB_DEFAULT=0' not in root.path('etc/default/grub').read_text()
        assert root.path('boot/grub2/grubenv').read_text() == \
            f'saved_entry=gnulinux-advanced-U1>gnulinux-{SLES_OLD}-advanced-U1\n'

    def test_no_builder_falls_back(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        root.path('boot/grub2/grubenv').write_text('saved_entry=x\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9,
                       FAKE_TOOLS='lsinitrd grub2-mkconfig grubby')
        assert res.ok, res
        assert 'no initramfs builder' in ' '.join(res.lines)


@pytestmark_shell
class TestScriptFailedLeavesDiskUnchanged:

    def test_single_kernel_build_failure(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'corrupt'},
                        bls=True)
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL='all')
        assert res.result.startswith('FAILED:')
        assert 'no other installed kernel has a valid initramfs' in res.result
        assert root.snapshot() == before

    def test_single_kernel_missing_image_build_failure(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW], images={DEB_NEW: 'missing'})
        before = root.snapshot()
        res = root.run(FAKE_BUILD_FAIL='all')
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_all_images_invalid_no_fallback(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt', RHEL9_OLD: 'corrupt'}, bls=True)
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL='all')
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_grubby_failure_undoes_everything(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='0',
                        bls_initrd={RHEL9_OLD: ''})
        root.path('boot/grub2/grubenv').write_text('saved_entry=orig\n')
        root.path('boot/grub2/grub.cfg').write_text(
            '### BEGIN /etc/grub.d/10_linux ###\noriginal\n')
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9,
                       FAKE_GRUBBY_FAIL=1)
        assert res.result.startswith('FAILED:')
        assert 'no previous kernel could be set as the default' in res.result
        assert root.snapshot() == before

    def test_next_fallback_candidate_used(self, tmp_path):
        older = '5.14.0-284.11.1.el9_2.x86_64'
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD, older],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved',
                        bls_initrd={RHEL9_OLD: ''})
        root.path('boot/grub2/grubenv').write_text('saved_entry=orig\n')
        entry = root.path(f'boot/loader/entries/abc-{RHEL9_OLD}.conf')
        entry_before = entry.read_text()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9,
                       FAKE_GRUBBY_FAIL_FOR=RHEL9_OLD)
        assert res.result == 'SUCCESS:1'
        assert any(f'set the previous kernel {older}' in l for l in res.lines)
        assert root.path('boot/grub2/grubenv').read_text() == f'saved_entry=bls-{older}\n'
        # The skipped candidate's entry (initrd line was added, then undone)
        # is back to its original content.
        assert entry.read_text() == entry_before

    def test_grubby_silent_noop_detected(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        root.path('boot/grub2/grubenv').write_text(f'saved_entry=bls-{RHEL9}\n')
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9,
                       FAKE_GRUBBY_LIE=1)
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_debian_grub_failure_undoes_dropin(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW, DEB_OLD],
                        images={DEB_NEW: 'corrupt'})
        root.path('boot/grub/grub.cfg').write_text(
            '### BEGIN /etc/grub.d/10_linux ###\noriginal\n')
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW, FAKE_BUILD_FAIL=DEB_NEW,
                       FAKE_GRUB_FAIL=1)
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_debian_existing_dropin_restored(self, tmp_path):
        root = FakeRoot(tmp_path, 'debian', [DEB_NEW, DEB_OLD],
                        images={DEB_NEW: 'corrupt'})
        d = root.path('etc/default/grub.d')
        d.mkdir()
        (d / '99-gce-rescue-fallback.cfg').write_text('GRUB_DEFAULT=saved\n# old\n')
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=DEB_NEW, FAKE_BUILD_FAIL=DEB_NEW,
                       FAKE_GRUB_FAIL=1)
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_sles_missing_menu_entry_undoes_grub_default(self, tmp_path):
        root = FakeRoot(tmp_path, 'suse', [SLES_NEW, SLES_OLD],
                        images={SLES_NEW: 'corrupt'})
        root.path('boot/grub2/grub.cfg').write_text(
            '### BEGIN /etc/grub.d/10_linux ###\noriginal\n')
        before = root.snapshot()
        # grub2-mkconfig output without per-kernel entries.
        body = ('fake_mkconfig() { echo "### BEGIN /etc/grub.d/10_linux ###" '
                '> "$SYSROOT$1"; }; initramfs_repair')
        res = root.run(body, GCE_FAILING_KERNEL=SLES_NEW, FAKE_BUILD_FAIL=SLES_NEW)
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before


@pytestmark_shell
class TestScriptBackups:

    def test_restore_removes_file_that_did_not_exist(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path('var/backups/b').mkdir(parents=True)
        res = root.run('INITRAMFS_BACKUP_DIR=/var/backups/b; '
                       'initramfs_backup_copy /etc/new.cfg; '
                       'echo x > "$SYSROOT/etc/new.cfg"; '
                       'initramfs_restore_copy /etc/new.cfg')
        assert res.returncode == 0
        assert not root.path('etc/new.cfg').exists()

    def test_first_backup_wins(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path('var/backups/b').mkdir(parents=True)
        root.run('INITRAMFS_BACKUP_DIR=/var/backups/b; '
                 'initramfs_backup_copy /etc/default/grub; '
                 'echo changed > "$SYSROOT/etc/default/grub"; '
                 'initramfs_backup_copy /etc/default/grub; '
                 'initramfs_restore_copy /etc/default/grub')
        assert 'GRUB_DEFAULT=0' in root.path('etc/default/grub').read_text()

    def test_symlinked_grubenv_restored_through_link(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        esp = root.path('boot/efi/EFI/rocky')
        esp.mkdir(parents=True)
        (esp / 'grubenv').write_text('saved_entry=orig\n')
        os.symlink('../efi/EFI/rocky/grubenv', root.path('boot/grub2/grubenv'))
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9,
                       FAKE_GRUBBY_LIE=1)
        assert res.result.startswith('FAILED:')
        assert root.path('boot/grub2/grubenv').is_symlink()
        assert root.snapshot() == before

    def test_dangling_grubenv_symlink_not_touched(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        os.symlink('../efi/EFI/rocky/grubenv', root.path('boot/grub2/grubenv'))
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9)
        assert res.result.startswith('FAILED:')
        assert root.snapshot() == before

    def test_success_leaves_no_markers(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'missing'},
                        bls=True)
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert not any(name.endswith('.absent') for name in root.backups())

    def test_backup_dir_removed_when_empty(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path('var/backups/gce-rescue/initramfs-x').mkdir(parents=True)
        root.run('INITRAMFS_BACKUP_DIR=/var/backups/gce-rescue/initramfs-x; '
                 'INITRAMFS_TOUCHED=; initramfs_finish')
        assert not root.path('var/backups/gce-rescue').exists()


class TestUntargetedFstabDoesNotBlock:
    """An fstab finding without an fstab identifier (e.g. a shutdown-time
    'mount: ...' message) must not block the initramfs repair."""

    UNTARGETED = _err('fstab_mount_failed', category='fstab',
                      pattern='mount: Mount disappeared even though umount '
                              'process failed')

    def test_untargeted_fstab_kept_without_initramfs(self):
        d = {'boot_errors': [self.UNTARGETED,
                             _err('grub_rescue_prompt', category='grub')]}
        assert _orchestrator().get_fixable_categories(d) == ['fstab', 'grub']

    def test_fstab_dropped_when_other_category_fixable(self):
        d = {'boot_errors': [self.UNTARGETED, _err('initramfs_no_root_fs')]}
        assert _orchestrator().get_fixable_categories(d) == ['initramfs']

    def test_fstab_alone_kept_for_cli_message(self):
        d = {'boot_errors': [self.UNTARGETED]}
        assert _orchestrator().get_fixable_categories(d) == ['fstab']

    def test_targeted_fstab_kept(self):
        d = {'boot_errors': [
            _err('fstab_mount_failed', category='fstab',
                 pattern='UUID=1234-abcd does not exist'),
            _err('initramfs_no_root_fs')]}
        assert _orchestrator().get_fixable_categories(d) == ['fstab', 'initramfs']


# ===========================================================================
# Kernel binary release check, FIPS hmac, early microcode images
# ===========================================================================

def bzimage(release, off=0x100):
    """Minimal x86 bzImage header carrying a kernel release string."""
    data = bytearray(0x400)
    data[0x202:0x206] = b'HdrS'
    data[0x20E:0x210] = off.to_bytes(2, 'little')
    text = f'{release} (mockbuild@x) #1 SMP PREEMPT_DYNAMIC\0'.encode()
    data[0x200 + off:0x200 + off + len(text)] = text
    return bytes(data)


@pytestmark_shell
class TestScriptKernelImageCheck:

    def test_release_read_from_bzimage(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(bzimage(RHEL9))
        res = root.run(f'initramfs_vmlinuz_release {RHEL9}')
        assert res.stdout.strip() == RHEL9

    @pytest.mark.parametrize('content', [
        b'kernel text\n',                       # not a bzImage
        b'\0' * 0x204,                          # truncated header
        bzimage(RHEL9)[:0x210],                 # string beyond end of file
        bzimage('6.1$(reboot)'),                # unsafe release string
        bzimage(RHEL9, off=0),                  # no version pointer
    ])
    def test_unreadable_release_is_empty(self, tmp_path, content):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(content)
        res = root.run(f'initramfs_vmlinuz_release {RHEL9}; '
                       f'initramfs_kernel_image_ok {RHEL9} && echo OK')
        assert res.stdout.strip() == 'OK'

    def test_mismatch_detected(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9])
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(bzimage(RHEL9_OLD))
        res = root.run(f'initramfs_kernel_image_ok {RHEL9} || '
                       'echo "BAD $INITRAMFS_IMAGE_RELEASE"')
        assert res.stdout.strip() == f'BAD {RHEL9_OLD}'

    def test_wrong_binary_falls_back_without_rebuild(self, tmp_path):
        """Lab scenario: an older vmlinuz copied over the newest kernel."""
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, RHEL9_OLD], bls=True,
                        grub_default='saved')
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(bzimage(RHEL9_OLD))
        root.path(f'boot/vmlinuz-{RHEL9_OLD}').write_bytes(bzimage(RHEL9_OLD))
        root.path('boot/grub2/grubenv').write_text(f'saved_entry=bls-{RHEL9}\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.builds() == []
        assert root.path('boot/grub2/grubenv').read_text() == \
            f'saved_entry=bls-{RHEL9_OLD}\n'
        assert any(f'contains kernel {RHEL9_OLD}' in line
                   and f'previous kernel {RHEL9_OLD}' in line
                   for line in res.lines)
        # Kernel binaries and images are untouched.
        assert root.path(f'boot/vmlinuz-{RHEL9}').read_bytes() == bzimage(RHEL9_OLD)
        assert root.image(RHEL9).read_text() == valid_image(RHEL9)

    def test_fallback_skips_kernel_with_wrong_binary(self, tmp_path):
        mid = '5.14.0-400.el9.x86_64'
        root = FakeRoot(tmp_path, 'rhel', [RHEL9, mid, RHEL9_OLD],
                        images={RHEL9: 'corrupt'}, bls=True, grub_default='saved')
        root.path(f'boot/vmlinuz-{mid}').write_bytes(bzimage(RHEL9))
        root.path('boot/grub2/grubenv').write_text('saved_entry=x\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9, FAKE_BUILD_FAIL=RHEL9)
        assert res.ok, res
        assert root.path('boot/grub2/grubenv').read_text() == \
            f'saved_entry=bls-{RHEL9_OLD}\n'
        assert f'Skipping kernel {mid}' in res.stderr

    def test_wrong_binary_without_fallback_leaves_disk_unchanged(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], bls=True, grub_default='saved')
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(bzimage(RHEL9_OLD))
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.result.startswith('FAILED:'), res
        assert f'contains kernel {RHEL9_OLD}' in res.result
        assert root.snapshot() == before
        assert root.builds() == []

    def test_matching_binary_rebuilds_normally(self, tmp_path):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], bls=True)
        root.path(f'boot/vmlinuz-{RHEL9}').write_bytes(bzimage(RHEL9))
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert len(root.builds()) == 1


@pytestmark_shell
class TestScriptFipsHmac:

    def _root(self, tmp_path, pkg_hmac=True):
        root = FakeRoot(tmp_path, 'rhel', [RHEL9], images={RHEL9: 'corrupt'},
                        bls=True)
        if pkg_hmac:
            root.path(f'usr/lib/modules/{RHEL9}/.vmlinuz.hmac').write_text(
                f'abc123  /boot/vmlinuz-{RHEL9}\n')
        return root

    def test_missing_hmac_restored_from_package(self, tmp_path):
        root = self._root(tmp_path)
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.path(f'boot/.vmlinuz-{RHEL9}.hmac').read_text() == \
            f'abc123  /boot/vmlinuz-{RHEL9}\n'
        assert any('FIPS checksum' in line for line in res.lines)

    def test_existing_hmac_never_changed(self, tmp_path):
        root = self._root(tmp_path)
        root.path(f'boot/.vmlinuz-{RHEL9}.hmac').write_text('customer\n')
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert root.path(f'boot/.vmlinuz-{RHEL9}.hmac').read_text() == 'customer\n'
        assert not any('FIPS' in line for line in res.lines)

    def test_no_package_hmac_nothing_done(self, tmp_path):
        root = self._root(tmp_path, pkg_hmac=False)
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert not root.path(f'boot/.vmlinuz-{RHEL9}.hmac').exists()
        assert not any('FIPS' in line for line in res.lines)

    def test_symlinked_package_hmac_ignored(self, tmp_path):
        root = self._root(tmp_path, pkg_hmac=False)
        os.symlink('/etc/shadow',
                   root.path(f'usr/lib/modules/{RHEL9}/.vmlinuz.hmac'))
        res = root.run(GCE_FAILING_KERNEL=RHEL9)
        assert res.ok, res
        assert not root.path(f'boot/.vmlinuz-{RHEL9}.hmac').exists()


@pytestmark_shell
class TestScriptEarlyImages:

    def _root(self, tmp_path, family='debian', bls=False):
        ver = DEB_NEW if family == 'debian' else RHEL9
        root = FakeRoot(tmp_path, family, [ver], images={ver: 'corrupt'},
                        bls=bls)
        return root, ver

    def test_damaged_early_image_moved_out(self, tmp_path):
        root, ver = self._root(tmp_path)
        root.path('boot/intel-ucode.img').write_bytes(b'\x8a\x11junk' * 100)
        res = root.run(GCE_FAILING_KERNEL=ver)
        assert res.ok, res
        assert not root.path('boot/intel-ucode.img').exists()
        backups = list((root.root / 'var/backups/gce-rescue').rglob('intel-ucode.img'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == b'\x8a\x11junk' * 100
        assert any('damaged early microcode image /boot/intel-ucode.img' in line
                   for line in res.lines)

    @pytest.mark.parametrize('content', [
        b'070701000000000000000000',          # cpio newc
        b'070702000000000000000000',          # cpio crc
        b'070707000000000000000000',          # cpio odc
        b'\x1f\x8b\x08\x00rest',              # gzip
        b'\xfd7zXZ\x00rest',                  # xz
        b'\x28\xb5\x2f\xfdrest',              # zstd
        b'',                                  # empty: harmless
    ])
    def test_valid_early_images_kept(self, tmp_path, content):
        root, ver = self._root(tmp_path)
        root.path('boot/amd-ucode.img').write_bytes(content)
        res = root.run(GCE_FAILING_KERNEL=ver)
        assert res.ok, res
        assert root.path('boot/amd-ucode.img').read_bytes() == content
        assert not any('early microcode' in line for line in res.lines)

    def test_moved_back_when_grub_regeneration_fails(self, tmp_path):
        root, ver = self._root(tmp_path)
        root.path('boot/intel-ucode.img').write_bytes(b'junkjunk')
        root.path('boot/grub/grub.cfg').write_text(
            '### BEGIN /etc/grub.d/10_linux ###\nold\n')
        res = root.run(GCE_FAILING_KERNEL=ver, FAKE_GRUB_FAIL=1)
        assert root.path('boot/intel-ucode.img').read_bytes() == b'junkjunk'
        assert not any('early microcode' in line for line in res.lines)

    def test_bls_system_left_alone(self, tmp_path):
        root, ver = self._root(tmp_path, family='rhel', bls=True)
        root.path('boot/intel-ucode.img').write_bytes(b'junkjunk')
        res = root.run(GCE_FAILING_KERNEL=ver)
        assert res.ok, res
        assert root.path('boot/intel-ucode.img').read_bytes() == b'junkjunk'

    def test_symlinked_early_image_left_alone(self, tmp_path):
        root, ver = self._root(tmp_path)
        root.path('boot/real.bin').write_bytes(b'junkjunk')
        os.symlink('real.bin', root.path('boot/intel-ucode.img'))
        res = root.run(GCE_FAILING_KERNEL=ver)
        assert res.ok, res
        assert root.path('boot/intel-ucode.img').is_symlink()

    def test_not_touched_when_rebuild_fails(self, tmp_path):
        """No rebuild, no fallback: FAILED leaves the disk unchanged."""
        root, ver = self._root(tmp_path)
        root.path('boot/intel-ucode.img').write_bytes(b'junkjunk')
        before = root.snapshot()
        res = root.run(GCE_FAILING_KERNEL=ver, FAKE_BUILD_FAIL='all')
        assert res.result.startswith('FAILED:'), res
        assert root.snapshot() == before
