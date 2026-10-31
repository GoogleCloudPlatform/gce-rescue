"""Tests for automated SELinux repair.

Covers:
  - catalog and orchestration wiring (fixable gating, execution order,
    GCE_SELINUX_FINDINGS, post-restore boot wait)
  - startup-script composition (compose.py)
  - CLI help and diagnose report output
  - startup_scripts/fixes/selinux_fix.sh: static checks, and runs in bash
    against fake root filesystems (mountpoint and sync are replaced by
    shell functions, so no root privileges are needed and the host is
    never touched)
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gce_rescue_v2 import cli
from gce_rescue_v2.core.fix_catalog import SUPPORTED_FIX_CATEGORIES
from gce_rescue_v2.orchestration.compose import compose_startup_script
from gce_rescue_v2.orchestration.repair import (
    FIX_EXECUTION_ORDER,
    INITRAMFS_BOOT_WAIT_SECONDS,
    SELINUX_BOOT_WAIT_SECONDS,
    RepairOrchestrator,
)
from gce_rescue_v2.utils.report_formatter import DiagnosisReportFormatter


SCRIPT = (Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
          / 'selinux_fix.sh')

POLICY_FAIL = 'Failed to load SELinux policy, freezing.'
RELABEL = '*** Warning -- SELinux targeted policy relabel is required. ***'

# Binary policy header: SELinux magic 0xf97cff8c, little-endian.
POLICY_MAGIC = b'\x8c\xff\x7c\xf9'


def _orchestrator():
    compute = Mock()
    logger = logging.getLogger('test_selinux_fix')
    logger.console_level = logging.WARNING
    return RepairOrchestrator(compute, 'proj', 'zone-a', 'vm-1', logger=logger)


def _err(name, category='selinux', pattern=''):
    return {'name': name, 'category': category, 'severity': 'critical',
            'description': name, 'detected_pattern': pattern}


def _script(diagnosis):
    orch = _orchestrator()
    with patch.object(orch, '_load_base_script',
                      return_value='mount /dev/sdb1 /mnt/sysroot\nsignal_complete\n'):
        return orch._generate_repair_script(diagnosis)


# ===========================================================================
# Catalog and orchestration wiring
# ===========================================================================

class TestCatalog:

    def test_selinux_supported(self):
        assert 'selinux' in SUPPORTED_FIX_CATEGORIES

    def test_fix_script_registered_for_category(self):
        assert SCRIPT.name == 'selinux_fix.sh'
        assert SCRIPT.is_file()

    def test_execution_order_after_disk_fixes_before_grub(self):
        order = FIX_EXECUTION_ORDER
        assert 'selinux' in order
        for earlier in ('filesystem', 'disk_full', 'fstab', 'initramfs'):
            assert order.index(earlier) < order.index('selinux')
        assert order.index('selinux') < order.index('grub')


class TestFixableGating:

    def test_policy_load_failure_is_fixable(self):
        d = {'boot_errors': [_err('selinux_policy_load_failure')]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['selinux']
        assert orch.get_unfixable_categories(d) == []

    def test_relabel_banner_alone_is_not_repaired(self):
        # A relabel in progress resolves itself; repairing it would stop the
        # VM mid-relabel and set permissive mode.
        d = {'boot_errors': [_err('selinux_relabel_required')]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == []
        assert orch.get_unfixable_categories(d) == ['selinux']

    def test_relabel_banner_with_policy_failure_is_fixable(self):
        d = {'boot_errors': [_err('selinux_relabel_required'),
                             _err('selinux_policy_load_failure')]}
        orch = _orchestrator()
        assert orch.get_fixable_categories(d) == ['selinux']
        assert orch.get_unfixable_categories(d) == []

    def test_ordered_between_fstab_and_grub(self):
        d = {'boot_errors': [
            _err('grub_rescue_prompt', category='grub'),
            _err('selinux_policy_load_failure'),
            _err('fstab_bad_uuid', category='fstab', pattern='UUID=abc-123'),
        ]}
        assert _orchestrator().get_fixable_categories(d) == [
            'fstab', 'selinux', 'grub']


class TestGenerateRepairScript:

    def test_selinux_script_composed(self):
        script = _script({'boot_errors': [_err('selinux_policy_load_failure')]})
        assert '=== selinux repair started ===' in script
        assert 'selinux_repair' in script
        assert script.rstrip().endswith(
            'log "=== Startup script completed successfully ==="')

    def test_findings_injected(self):
        script = _script({'boot_errors': [
            _err('selinux_relabel_required'),
            _err('selinux_policy_load_failure'),
            _err('selinux_relabel_required'),
        ]})
        assert ('GCE_SELINUX_FINDINGS="selinux_relabel_required '
                'selinux_policy_load_failure"') in script
        assert (script.index('GCE_SELINUX_FINDINGS=')
                < script.index('=== selinux repair started ==='))

    def test_no_variable_without_selinux(self):
        script = _script({'boot_errors': [
            _err('fstab_bad_uuid', category='fstab', pattern='UUID=abc-123')]})
        assert 'GCE_SELINUX_FINDINGS' not in script

    def test_kernel_variables_not_injected_for_selinux_only(self):
        script = _script({'boot_errors': [_err('selinux_policy_load_failure')]})
        assert 'GCE_FAILING_KERNEL' not in script
        assert 'GCE_BOOT_ROOT' not in script


class TestComposeSelinuxFindings:

    BASE = 'mount /dev/sdb1 /mnt/sysroot\nsignal_complete\n'

    def test_none_omits_variable(self):
        assert 'GCE_SELINUX_FINDINGS' not in compose_startup_script(
            self.BASE, ['echo fix'])

    def test_empty_list_injected_empty(self):
        out = compose_startup_script(self.BASE, ['echo fix'],
                                     selinux_findings=[])
        assert 'GCE_SELINUX_FINDINGS=""' in out

    def test_injected_before_fix_body(self):
        out = compose_startup_script(self.BASE, ['echo fix'],
                                     selinux_findings=['selinux_relabel_required'])
        assert (out.index('GCE_SELINUX_FINDINGS="selinux_relabel_required"')
                < out.index('echo fix'))

    @pytest.mark.parametrize('bad', [
        'selinux_x"; reboot; "', 'selinux_$(id)', 'fstab_bad_uuid',
        'SELINUX_UPPER', 'selinux_a b', '', None])
    def test_unsafe_names_dropped(self, bad):
        out = compose_startup_script(
            self.BASE, ['echo fix'],
            selinux_findings=[bad, 'selinux_policy_load_failure'])
        assert 'GCE_SELINUX_FINDINGS="selinux_policy_load_failure"' in out


class TestBootVerificationWait:
    """After a relabel the first boot reboots without a success marker."""

    RELABEL_BOOT = ('[0.0] Linux version 5.14.0-427.el9.x86_64 (x) #1 SMP\n'
                    f'[9.0] {RELABEL}\n')
    DONE = RELABEL_BOOT + (
        '[0.0] Linux version 5.14.0-427.el9.x86_64 (x) #1 SMP\n'
        '[12.0] systemd[1]: Startup finished in 12.0s (kernel) + 9s.\n')

    def _run(self, contents, categories):
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
        return result, sum(sleeps)

    def test_budget_is_420s(self):
        assert SELINUX_BOOT_WAIT_SECONDS == 420
        assert SELINUX_BOOT_WAIT_SECONDS > INITRAMFS_BOOT_WAIT_SECONDS

    def test_relabel_boot_waits_full_budget(self):
        _, waited = self._run([self.RELABEL_BOOT], ['selinux'])
        assert waited == SELINUX_BOOT_WAIT_SECONDS

    def test_longest_budget_wins(self):
        _, waited = self._run([self.RELABEL_BOOT], ['initramfs', 'selinux'])
        assert waited == SELINUX_BOOT_WAIT_SECONDS

    def test_stops_early_when_boot_after_relabel_completes(self):
        result, waited = self._run(
            [self.RELABEL_BOOT, self.RELABEL_BOOT, self.DONE], ['selinux'])
        assert waited < SELINUX_BOOT_WAIT_SECONDS
        assert result['verified'] is True


# ===========================================================================
# CLI help and diagnose report
# ===========================================================================

class TestCliAndReport:

    def test_repair_help_lists_selinux(self, capsys):
        with pytest.raises(SystemExit):
            cli.create_parser().parse_args(['repair', '--help'])
        out = capsys.readouterr().out
        assert '- selinux:' in out
        assert '/.autorelabel' in out

    @staticmethod
    def _report(*errs):
        errors = []
        for name, category, pattern in errs:
            err = _err(name, category=category, pattern=pattern)
            err.update(suggested_fixes=[], context_lines=[],
                       matched_line_index=-1)
            errors.append(err)
        diagnosis = {'vm_name': 'vm', 'zone': 'z', 'status': 'RUNNING',
                     'os_type': 'linux', 'os_flavor': 'rocky-9',
                     'architecture': 'x86_64', 'license_type': 'free',
                     'diagnosis_status': 'boot_errors_detected',
                     'boot_errors': errors, 'recommendations': []}
        report = DiagnosisReportFormatter().format_report(diagnosis)
        return report, ' '.join(report.split())

    def test_auto_repair_offered(self):
        report, _ = self._report(
            ('selinux_policy_load_failure', 'selinux', POLICY_FAIL))
        assert 'Auto-repair (recommended):' in report
        assert 'gce-rescue repair vm --zone=z' in report
        assert 'A relabel is in progress' not in report

    def test_relabel_banner_alone_advises_waiting(self):
        report, flat = self._report(
            ('selinux_relabel_required', 'selinux', RELABEL))
        assert 'Auto-repair (recommended):' not in report
        assert 'gce-rescue rescue' not in report
        assert ('A relabel is in progress and reboots the VM when done. '
                'Wait for it; run diagnose again if the banner repeats '
                'across reboots.') in flat

    def test_relabel_banner_with_policy_failure_offers_repair(self):
        report, flat = self._report(
            ('selinux_relabel_required', 'selinux', RELABEL),
            ('selinux_policy_load_failure', 'selinux', POLICY_FAIL))
        assert 'Auto-repair (recommended):' in report
        assert 'A relabel is in progress' not in flat

    def test_relabel_banner_beside_fstab_repair(self):
        report, flat = self._report(
            ('selinux_relabel_required', 'selinux', RELABEL),
            ('fstab_bad_uuid', 'fstab', 'UUID=abc-123'))
        assert 'Auto-repair (recommended):' in report
        assert 'A relabel is in progress' in flat

    def test_report_and_repair_agree(self):
        """diagnose offers auto-repair exactly when repair would act."""
        orch = _orchestrator()
        for names in (['selinux_relabel_required'],
                      ['selinux_policy_load_failure'],
                      ['selinux_relabel_required',
                       'selinux_policy_load_failure']):
            report, _ = self._report(*[(n, 'selinux', '') for n in names])
            fixable = orch.get_fixable_categories(
                {'boot_errors': [_err(n) for n in names]})
            assert ('Auto-repair (recommended):' in report) == bool(fixable)


# ===========================================================================
# selinux_fix.sh: static checks
# ===========================================================================

def _code_lines():
    for line in SCRIPT.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith('#'):
            yield stripped


class TestScriptStatic:

    def test_never_calls_exit(self):
        code = '\n'.join(_code_lines())
        shell_only = re.sub(r"'[^']*'", "''", code)  # drop awk programs
        for line in shell_only.splitlines():
            assert not re.search(r'(^|[;&|{]\s*)exit\b', line), line

    def test_functions_are_namespaced(self):
        names = re.findall(r'^([A-Za-z_][A-Za-z0-9_]*)\(\)\s*\{',
                           SCRIPT.read_text(), re.MULTILINE)
        assert names
        shared = {'log', 'repair_line', 'repair_result'}
        for name in names:
            assert name in shared or name.startswith('selinux_'), name

    def test_uses_contract_markers(self):
        text = SCRIPT.read_text()
        assert 'echo "GCE-REPAIR-LINE:$1" >&2' in text
        assert 'echo "GCE-REPAIR-RESULT:$1" >&2' in text
        for result in ('SUCCESS:1', 'NO_ISSUES:0', 'FAILED:'):
            assert f'repair_result "{result}' in text

    def test_backs_up_to_var_backups(self):
        assert '/var/backups/gce-rescue/selinux-' in SCRIPT.read_text()

    def test_never_disables_selinux(self):
        for line in _code_lines():
            assert 'SELINUX=disabled/' not in line, line
            assert not re.search(r'SELINUX=disabled["\s]*>', line), line

    def test_never_deletes_recursively(self):
        for line in _code_lines():
            assert 'rm -r' not in line and 'rm -fr' not in line, line

    def test_lib_only_hook(self):
        assert 'GCE_SELINUX_FIX_LIB_ONLY' in SCRIPT.read_text()

    def test_no_os_flavor_dependency(self):
        assert 'OS_FLAVOR' not in SCRIPT.read_text()


# ===========================================================================
# selinux_fix.sh: behaviour against fake root filesystems
# ===========================================================================

def _bash_usable() -> bool:
    """Whether a bash that understands this platform's paths is available."""
    bash = shutil.which('bash')
    if bash is None:
        return False
    try:
        proc = subprocess.run(
            [bash, '-c', 'test -f "$1"', '_', SCRIPT.as_posix()],
            capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


needs_bash = pytest.mark.skipif(
    not _bash_usable(),
    reason='runs the fix script against a fake root; needs bash')

# chroot, mount, umount and timeout are shell functions: nothing touches the
# host. The fake package manager writes a valid binary policy when FAKE_PM
# allows the verb it was called with, and records the resolv.conf the chroot
# saw; the fake grubby records its call and, with FAKE_GRUBBY=edit, appends
# its argument to the BLS options lines like the real one.
HARNESS = r'''
export LOGFILE="$WORK/log"
GCE_SELINUX_FIX_LIB_ONLY=1
source "$SCRIPT" 2>/dev/null
SYSROOT="$ROOT"
SELINUX_HOST_RESOLV="$WORK/host-resolv.conf"
mountpoint() { [ "$2" = "$SYSROOT" ]; }
sync() { :; }
timeout() { shift; "$@"; }
mount() { echo "mount $*" >> "$WORK/mounts"; }
umount() { echo "umount $*" >> "$WORK/mounts"; }
fake_pm() {
    local verb pkg t
    cat "$ROOT/etc/resolv.conf" > "$WORK/resolv-during-pm" 2>/dev/null
    for verb in "$@"; do
        case "$verb" in reinstall|upgrade|install|update) break ;; esac
    done
    [ "$verb" = "update" ] && return 0
    case "${FAKE_PM:-fail}" in
        timeout) return 124 ;;
        ok) ;;
        "$verb") ;;
        *) return 1 ;;
    esac
    for pkg in "$@"; do :; done
    t="${pkg#selinux-policy-}"
    mkdir -p "$ROOT/etc/selinux/$t/policy"
    printf '\x8c\xff\x7c\xf9AAAAAAAA' > "$ROOT/etc/selinux/$t/policy/policy.33"
}
fake_grubby() {
    local f
    [ "${FAKE_GRUBBY:-noop}" = "fail" ] && return 1
    [ "${FAKE_GRUBBY:-noop}" = "edit" ] || return 0
    for f in "$ROOT"/boot/loader/entries/*.conf; do
        case "$2" in
            --args=*) sed -i "s/^options .*/& ${2#--args=}/" "$f" ;;
        esac
    done
}
chroot() {
    shift
    if [ "$1" = "/usr/bin/env" ]; then
        shift 2
        while [ $# -gt 0 ] && [[ "$1" == *=* ]]; do shift; done
    fi
    echo "$*" >> "$WORK/chroot.log"
    case "$1" in
        dnf|yum|zypper|apt-get) fake_pm "$@" ;;
        grubby) fake_grubby "$@" ;;
        *) return 0 ;;
    esac
}
selinux_repair
'''

HOST_RESOLV = 'nameserver 169.254.169.254\n'
TARGET_RESOLV = 'nameserver 10.0.0.2\n'


class FakeRoot:
    """A fake RHEL-family root filesystem with an SELinux configuration."""

    def __init__(self, tmp_path, mode='enforcing', policy_type='targeted',
                 policy='valid', autorelabel=False, cmdline='console=ttyS0',
                 config=True, extra_policies=(), tools=(), bls=False,
                 grub_cfg=False, resolv='file'):
        self.root = tmp_path / 'root'
        self.work = tmp_path / 'work'
        self.work.mkdir()
        (self.work / 'host-resolv.conf').write_text(HOST_RESOLV)
        r = self.root
        (r / 'etc/selinux').mkdir(parents=True)
        (r / 'etc/default').mkdir()
        (r / 'var/log').mkdir(parents=True)
        self.cmdline = cmdline
        (r / 'etc/default/grub').write_text(
            f'GRUB_TIMEOUT=0\nGRUB_CMDLINE_LINUX="{cmdline}"\n')
        self.config_text = (
            '# This file controls the state of SELinux on the system.\n'
            '# SELINUX= can take one of these three values:\n'
            f'SELINUX={mode}\n'
            f'SELINUXTYPE={policy_type}\n')
        if config:
            self.config.write_text(self.config_text)
        policy_dir = r / 'etc/selinux' / policy_type / 'policy'
        if policy == 'valid':
            policy_dir.mkdir(parents=True)
            (policy_dir / 'policy.33').write_bytes(POLICY_MAGIC + b'\0' * 64)
        elif policy == 'corrupt':
            policy_dir.mkdir(parents=True)
            (policy_dir / 'policy.33').write_bytes(b'garbage' * 8)
        elif policy == 'empty_dir':
            policy_dir.mkdir(parents=True)
        for extra in extra_policies:
            d = r / 'etc/selinux' / extra / 'policy'
            d.mkdir(parents=True)
            (d / 'policy.33').write_bytes(POLICY_MAGIC + b'\0' * 64)
        if autorelabel:
            (r / '.autorelabel').write_text('')
        for tool in tools:
            sub = 'usr/sbin' if tool == 'grubby' else 'usr/bin'
            (r / sub).mkdir(parents=True, exist_ok=True)
            (r / sub / tool).write_text('#!/bin/sh\n')
            (r / sub / tool).chmod(0o755)
        if bls:
            entries = r / 'boot/loader/entries'
            entries.mkdir(parents=True)
            for ver in ('5.14.0-427.el9.x86_64', '5.14.0-362.el9.x86_64'):
                (entries / f'abc-{ver}.conf').write_text(
                    f'title Rocky Linux ({ver})\n'
                    f'linux /boot/vmlinuz-{ver}\n'
                    f'initrd /boot/initramfs-{ver}.img\n'
                    f'options root=UUID=1234 ro {cmdline}\n'
                    'grub_users $grub_users\n')
        if grub_cfg:
            cfg = r / 'boot/grub2'
            cfg.mkdir(parents=True)
            (cfg / 'grub.cfg').write_text(
                "menuentry 'Linux' {\n"
                f'\tlinux /vmlinuz-5.14 root=/dev/sda2 ro {cmdline}\n'
                '\tinitrd /initramfs-5.14.img\n'
                '}\n')
        if resolv == 'file':
            (r / 'etc/resolv.conf').write_text(TARGET_RESOLV)
        # A Linux disk has LF line endings; write_text uses CRLF on Windows.
        for f in r.rglob('*'):
            if f.is_file() and not f.is_symlink():
                f.write_bytes(f.read_bytes().replace(b'\r\n', b'\n'))

    @property
    def config(self) -> Path:
        return self.root / 'etc/selinux/config'

    @property
    def autorelabel(self) -> Path:
        return self.root / '.autorelabel'

    @property
    def grub_default(self) -> str:
        return (self.root / 'etc/default/grub').read_text()

    def bls_options(self):
        entries = sorted((self.root / 'boot/loader/entries').glob('*.conf'))
        return [line for e in entries for line in e.read_text().splitlines()
                if line.startswith('options ')]

    def policy_valid(self, ptype='targeted') -> bool:
        f = self.root / 'etc/selinux' / ptype / 'policy/policy.33'
        return f.is_file() and f.read_bytes()[:4] == POLICY_MAGIC

    def backups(self):
        base = self.root / 'var/backups/gce-rescue'
        return sorted(p for p in base.rglob('*') if p.is_file()) \
            if base.exists() else []

    def backup(self, name) -> str:
        found = [p for p in self.backups() if p.name == name]
        assert len(found) == 1, self.backups()
        return found[0].read_text()

    def work_lines(self, name):
        f = self.work / name
        return f.read_text().splitlines() if f.exists() else []

    def run(self, findings='', pm='fail', grubby='noop'):
        env = dict(os.environ)
        env.update({'SCRIPT': SCRIPT.as_posix(), 'ROOT': self.root.as_posix(),
                    'WORK': self.work.as_posix(),
                    'GCE_SELINUX_FINDINGS': findings,
                    'FAKE_PM': pm, 'FAKE_GRUBBY': grubby})
        proc = subprocess.run([shutil.which('bash'), '-c', HARNESS], env=env,
                              capture_output=True, text=True, timeout=120)
        return Result(proc)


class Result:

    def __init__(self, proc):
        self.stderr = proc.stderr
        self.results = re.findall(r'^GCE-REPAIR-RESULT:(.*)$', proc.stderr, re.M)
        self.lines = re.findall(r'^GCE-REPAIR-LINE:(.*)$', proc.stderr, re.M)

    @property
    def result(self):
        assert len(self.results) == 1, self.stderr
        return self.results[0]

    def line(self, text):
        found = [line for line in self.lines if text in line]
        assert found, self
        return found[0]

    def __repr__(self):
        return f'Result({self.results!r})\n{self.stderr}'


LOAD_FAIL = 'selinux_policy_load_failure'
LOOP = 'selinux_relabel_required selinux_policy_load_failure'


@needs_bash
class TestScriptBehaviour:

    def test_policy_installed_schedules_relabel(self, tmp_path):
        root = FakeRoot(tmp_path)
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert root.autorelabel.is_file()
        assert root.autorelabel.stat().st_size == 0
        assert root.config.read_text() == root.config_text
        assert root.backups() == []
        assert not (root.root / 'var/backups').exists()
        assert any('/.autorelabel' in line for line in res.lines), res
        assert root.work_lines('chroot.log') == []
        assert root.work_lines('mounts') == []

    def test_existing_autorelabel_with_failed_relabel_falls_back(self, tmp_path):
        # The policy is valid, so this is a labelling problem: permissive
        # mode boots.
        root = FakeRoot(tmp_path, autorelabel=True)
        res = root.run('selinux_relabel_required')
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUX=permissive\n' in root.config.read_text()
        assert root.autorelabel.exists()
        assert root.backup('etc_selinux_config') == root.config_text
        assert any('never finished booting' in line for line in res.lines), res

    def test_existing_autorelabel_permissive_already_fails(self, tmp_path):
        root = FakeRoot(tmp_path, mode='permissive', autorelabel=True)
        res = root.run('selinux_relabel_required')
        assert res.result.startswith('FAILED:'), res
        assert 'gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT' \
            in res.result
        assert root.config.read_text() == root.config_text

    def test_no_selinux_config_no_issues(self, tmp_path):
        root = FakeRoot(tmp_path, config=False)
        res = root.run(LOAD_FAIL)
        assert res.result == 'NO_ISSUES:0', res
        assert not root.autorelabel.exists()

    def test_disabled_no_issues(self, tmp_path):
        root = FakeRoot(tmp_path, mode='disabled', policy='missing')
        res = root.run(LOAD_FAIL)
        assert res.result == 'NO_ISSUES:0', res
        assert root.config.read_text() == root.config_text

    def test_cmdline_selinux_0_reported_not_changed(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 selinux=0')
        grub = root.grub_default
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert any('selinux=0' in line and '[INFO]' in line
                   for line in res.lines), res
        assert root.grub_default == grub

    def test_commented_cmdline_ignored(self, tmp_path):
        root = FakeRoot(tmp_path)
        grub = root.root / 'etc/default/grub'
        grub.write_text(grub.read_text() + '#GRUB_CMDLINE_LINUX="selinux=0"\n')
        res = root.run(LOAD_FAIL)
        assert not any('selinux=0' in line for line in res.lines), res

    def test_bash_syntax(self):
        subprocess.run([shutil.which('bash'), '-n', SCRIPT.as_posix()],
                       check=True)

    def test_result_emitted_exactly_once_per_path(self, tmp_path):
        for i, kwargs in enumerate([
                {}, {'policy': 'missing'}, {'config': False},
                {'policy': 'missing', 'tools': ('dnf',)},
                {'policy': 'missing', 'tools': ('dnf', 'grubby'), 'bls': True},
                {'policy_type': 'mls', 'policy': 'missing',
                 'extra_policies': ('targeted',)}]):
            sub = tmp_path / str(i)
            sub.mkdir()
            res = FakeRoot(sub, **kwargs).run(LOAD_FAIL)
            assert len(res.results) == 1, res


@needs_bash
class TestWrongPolicyType:
    """A: SELINUXTYPE= names a policy that is not installed."""

    def test_switched_to_targeted(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='mls', policy='missing',
                        extra_policies=('targeted',))
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        text = root.config.read_text()
        assert 'SELINUXTYPE=targeted\n' in text
        assert 'SELINUX=enforcing\n' in text
        assert text.startswith('# This file controls')
        assert root.autorelabel.is_file()
        assert root.backup('etc_selinux_config') == root.config_text
        assert 'SELINUXTYPE=mls has no installed policy; switched to targeted' \
            in res.line('[FIXED]')
        # Nothing reinstalled, nothing mounted, kernel command line unchanged.
        assert root.work_lines('chroot.log') == []
        assert root.work_lines('mounts') == []
        assert 'selinux=0' not in root.grub_default

    def test_permissive_mode_kept(self, tmp_path):
        root = FakeRoot(tmp_path, mode='permissive', policy_type='mls',
                        policy='missing', extra_policies=('targeted',))
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUX=permissive\n' in root.config.read_text()

    def test_targeted_preferred(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='mls', policy='missing',
                        extra_policies=('minimum', 'targeted'))
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUXTYPE=targeted\n' in root.config.read_text()

    def test_any_other_type_when_targeted_missing(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', extra_policies=('minimum',))
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUXTYPE=minimum\n' in root.config.read_text()

    def test_corrupt_other_type_not_chosen(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='mls', policy='missing')
        d = root.root / 'etc/selinux/minimum/policy'
        d.mkdir(parents=True)
        (d / 'policy.33').write_bytes(b'garbage' * 8)
        res = root.run(LOAD_FAIL)
        assert 'SELINUXTYPE=mls\n' in root.config.read_text()
        assert res.result.startswith('FAILED:'), res

    def test_unsafe_policy_type_switched(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='../../tmp', policy='missing',
                        extra_policies=('targeted',))
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUXTYPE=targeted\n' in root.config.read_text()

    def test_absolute_config_symlink_resolved_inside_target(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='mls', policy='missing',
                        extra_policies=('targeted',), config=False)
        real = root.root / 'etc/sysconfig/selinux'
        real.parent.mkdir(parents=True)
        real.write_text(root.config_text)
        try:
            os.symlink('/etc/sysconfig/selinux', root.config)
        except (OSError, NotImplementedError):
            pytest.skip('symlinks not available')
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUXTYPE=targeted\n' in real.read_text()
        assert root.config.is_symlink()


@needs_bash
class TestPolicyReinstall:
    """B: no valid policy installed; reinstalled inside a chroot."""

    @pytest.mark.parametrize('policy', ['missing', 'empty_dir', 'corrupt'])
    def test_reinstalled_and_relabel_scheduled(self, tmp_path, policy):
        root = FakeRoot(tmp_path, policy=policy, tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert root.policy_valid()
        assert root.autorelabel.is_file()
        assert root.config.read_text() == root.config_text
        assert 'reinstalled selinux-policy-targeted with dnf' \
            in res.line('[FIXED]')
        assert root.work_lines('chroot.log') == [
            'dnf -y reinstall selinux-policy-targeted']
        assert 'selinux=0' not in root.grub_default

    def test_never_permissive_for_missing_policy(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        root.run(LOAD_FAIL, pm='ok')
        assert 'SELINUX=enforcing\n' in root.config.read_text()

    def test_chroot_dns_lent_and_restored(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert (root.work / 'resolv-during-pm').read_text() == HOST_RESOLV
        assert (root.root / 'etc/resolv.conf').read_text() == TARGET_RESOLV
        # The resolv.conf backup is dropped once it is restored.
        assert not any(p.name == 'etc_resolv.conf' for p in root.backups())

    def test_missing_target_resolv_removed_again(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',),
                        resolv=None)
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert (root.work / 'resolv-during-pm').read_text() == HOST_RESOLV
        assert not (root.root / 'etc/resolv.conf').exists()

    def test_symlinked_target_resolv_restored(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',),
                        resolv=None)
        link = root.root / 'etc/resolv.conf'
        try:
            os.symlink('/run/systemd/resolve/stub-resolv.conf', link)
        except (OSError, NotImplementedError):
            pytest.skip('symlinks not available')
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert (root.work / 'resolv-during-pm').read_text() == HOST_RESOLV
        assert link.is_symlink()
        assert os.readlink(link) == '/run/systemd/resolve/stub-resolv.conf'
        assert not (root.root / 'etc/resolv.conf.gce-rescue-orig').exists()

    def test_bind_mounts_undone_in_reverse(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        for d in ('dev', 'proc', 'sys', 'run'):
            (root.root / d).mkdir()
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        base = root.root.as_posix()
        assert root.work_lines('mounts') == [
            f'mount -o bind /dev {base}/dev',
            f'mount -o bind /proc {base}/proc',
            f'mount -o bind /sys {base}/sys',
            f'mount -o bind /run {base}/run',
            f'umount {base}/run',
            f'umount {base}/sys',
            f'umount {base}/proc',
            f'umount {base}/dev',
        ]

    def test_mounts_and_dns_undone_on_failure(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        for d in ('dev', 'proc'):
            (root.root / d).mkdir()
        res = root.run(LOAD_FAIL, pm='fail')
        assert res.result.startswith('FAILED:'), res
        mounts = root.work_lines('mounts')
        assert sum(m.startswith('umount') for m in mounts) == 2, mounts
        assert (root.root / 'etc/resolv.conf').read_text() == TARGET_RESOLV

    def test_upgrade_when_reinstall_unavailable(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='upgrade')
        assert res.result == 'SUCCESS:1', res
        assert root.work_lines('chroot.log') == [
            'dnf -y reinstall selinux-policy-targeted',
            'dnf -y upgrade selinux-policy-targeted']

    def test_exit_status_not_trusted(self, tmp_path):
        # 'install' of an installed package succeeds without restoring
        # files: success is judged by the policy file alone.
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='fail')
        assert len(root.work_lines('chroot.log')) == 3
        assert not root.policy_valid()
        assert res.result.startswith('FAILED:'), res

    def test_zypper(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('zypper',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert root.work_lines('chroot.log') == [
            'zypper --non-interactive install --force selinux-policy-targeted']

    def test_apt_debian_default_policy(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='default', policy='missing',
                        tools=('apt-get',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert root.work_lines('chroot.log') == [
            'apt-get update',
            'apt-get -y install --reinstall selinux-policy-default']
        assert root.policy_valid('default')

    def test_unsafe_type_reinstalls_targeted(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='../../tmp', policy='missing',
                        tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUXTYPE=targeted\n' in root.config.read_text()

    def test_permissive_mode_reinstalled_too(self, tmp_path):
        # Permissive mode does not boot without a policy (systemd cannot
        # label its sockets), so it is no reason to leave it missing.
        root = FakeRoot(tmp_path, mode='permissive', policy='missing',
                        tools=('dnf',))
        res = root.run(LOAD_FAIL, pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert root.policy_valid()
        assert 'SELINUX=permissive\n' in root.config.read_text()

    def test_existing_autorelabel_with_missing_policy_reinstalls(self, tmp_path):
        # D: the permissive fallback is only for a policy that loads.
        root = FakeRoot(tmp_path, policy='missing', autorelabel=True,
                        tools=('dnf',))
        res = root.run('selinux_relabel_required selinux_policy_load_failure',
                       pm='ok')
        assert res.result == 'SUCCESS:1', res
        assert root.policy_valid()
        assert 'SELINUX=enforcing\n' in root.config.read_text()


@needs_bash
class TestDisableOnCmdline:
    """C: the policy cannot be reinstalled; selinux=0 lets the VM boot."""

    def test_bls_and_default_grub(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf', 'grubby'),
                        bls=True)
        before_grub = root.grub_default
        res = root.run(LOAD_FAIL, pm='fail')
        assert res.result == 'SUCCESS:1', res
        for line in root.bls_options():
            assert line == 'options root=UUID=1234 ro console=ttyS0 selinux=0'
        assert 'GRUB_CMDLINE_LINUX="console=ttyS0 selinux=0"\n' \
            in root.grub_default
        # SELinux is not made permissive and no relabel is scheduled.
        assert root.config.read_text() == root.config_text
        assert not root.autorelabel.exists()
        line = res.line('[FIXED]')
        assert 'SELinux is DISABLED until the policy package is reinstalled' \
            in line
        assert ('sudo dnf -y reinstall selinux-policy-targeted && sudo grubby '
                '--update-kernel=ALL --remove-args=selinux=0') in line
        assert line.endswith('sudo touch /.autorelabel && sudo reboot')
        assert 'grubby --update-kernel=ALL --args=selinux=0' \
            in root.work_lines('chroot.log')
        assert root.backup('etc_default_grub') == before_grub
        assert len([p for p in root.backups()
                    if p.name.startswith('boot_loader_entries_')]) == 2

    def test_grubby_edit_not_duplicated(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf', 'grubby'),
                        bls=True)
        res = root.run(LOAD_FAIL, pm='fail', grubby='edit')
        assert res.result == 'SUCCESS:1', res
        for line in root.bls_options():
            assert line.split().count('selinux=0') == 1, line

    def test_grubby_failure_edits_directly(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf', 'grubby'),
                        bls=True)
        res = root.run(LOAD_FAIL, pm='fail', grubby='fail')
        assert res.result == 'SUCCESS:1', res
        assert all(line.endswith(' selinux=0') for line in root.bls_options())

    def test_package_manager_timeout_stops_retries(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', tools=('dnf',), bls=True)
        res = root.run(LOAD_FAIL, pm='timeout')
        assert res.result == 'SUCCESS:1', res
        assert root.work_lines('chroot.log') == [
            'dnf -y reinstall selinux-policy-targeted']

    def test_grub_cfg_without_grubby(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', grub_cfg=True)
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        cfg = (root.root / 'boot/grub2/grub.cfg').read_text()
        assert '\tlinux /vmlinuz-5.14 root=/dev/sda2 ro console=ttyS0 selinux=0\n' \
            in cfg
        assert '\tinitrd /initramfs-5.14.img\n' in cfg
        assert 'regenerate the GRUB config' in res.line('[FIXED]')

    def test_no_boot_entries_fails_with_rescue_guidance(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing')
        res = root.run(LOAD_FAIL)
        assert res.result.startswith('FAILED:'), res
        assert 'gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT' \
            in res.result
        assert root.config.read_text() == root.config_text

    @pytest.mark.parametrize('policy', ['missing', 'empty_dir', 'corrupt'])
    def test_never_sets_permissive(self, tmp_path, policy):
        root = FakeRoot(tmp_path, policy=policy, tools=('dnf',), bls=True)
        res = root.run(LOAD_FAIL, pm='fail')
        assert res.result == 'SUCCESS:1', res
        assert 'permissive' not in root.config.read_text()


@needs_bash
class TestCmdlineAutorelabelLoop:
    """E: autorelabel on the kernel command line relabels every boot."""

    @pytest.mark.parametrize('arg', ['autorelabel', 'autorelabel=1'])
    def test_argument_removed(self, tmp_path, arg):
        root = FakeRoot(tmp_path, cmdline=f'console=ttyS0 {arg} quiet',
                        tools=('grubby',), bls=True)
        res = root.run(LOOP)
        assert res.result == 'SUCCESS:1', res
        assert 'GRUB_CMDLINE_LINUX="console=ttyS0 quiet"\n' in root.grub_default
        for line in root.bls_options():
            assert line == 'options root=UUID=1234 ro console=ttyS0 quiet'
        assert 'grubby --update-kernel=ALL --remove-args=autorelabel' \
            in root.work_lines('chroot.log')
        line = res.line('[FIXED]')
        assert 'removed autorelabel from the kernel command line' in line
        assert 'relabel the whole filesystem and reboot' in line
        assert not root.autorelabel.exists()
        assert root.config.read_text() == root.config_text
        assert root.backup('etc_default_grub') \
            == f'GRUB_TIMEOUT=0\nGRUB_CMDLINE_LINUX="console=ttyS0 {arg} quiet"\n'

    def test_default_grub_only(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 autorelabel=1')
        res = root.run(LOOP)
        assert res.result == 'SUCCESS:1', res
        assert 'GRUB_CMDLINE_LINUX="console=ttyS0"\n' in root.grub_default

    def test_takes_priority_over_existing_autorelabel(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 autorelabel',
                        autorelabel=True, bls=True)
        res = root.run(LOOP)
        assert res.result == 'SUCCESS:1', res
        assert 'permissive' not in root.config.read_text()

    def test_grubenv_without_editenv_fails_with_rescue_guidance(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 autorelabel')
        env = root.root / 'boot/grub2'
        env.mkdir(parents=True)
        (env / 'grubenv').write_bytes(
            b'# GRUB Environment Block\nkernelopts=root=/dev/sda2 autorelabel\n')
        res = root.run(LOOP)
        assert res.result.startswith('FAILED:'), res
        assert res.result.count(
            'gce-rescue rescue VM_NAME --zone=ZONE --project=PROJECT first') == 1
        assert 'remove-args=autorelabel' in res.result
        assert not root.autorelabel.exists()

    def test_left_alone_without_relabel_finding(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 autorelabel')
        grub = root.grub_default
        res = root.run(LOAD_FAIL)
        assert res.result == 'SUCCESS:1', res
        assert root.grub_default == grub
        assert 'left unchanged' in res.line('autorelabel on the kernel')
        assert root.autorelabel.exists()
