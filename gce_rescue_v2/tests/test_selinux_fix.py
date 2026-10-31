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

    @pytest.mark.parametrize('name', [
        'selinux_policy_load_failure', 'selinux_relabel_required'])
    def test_each_selinux_finding_is_fixable(self, name):
        d = {'boot_errors': [_err(name)]}
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

    def test_auto_repair_offered(self):
        err = _err('selinux_policy_load_failure', pattern=POLICY_FAIL)
        err.update(suggested_fixes=[], context_lines=[], matched_line_index=-1)
        diagnosis = {'vm_name': 'vm', 'zone': 'z', 'status': 'RUNNING',
                     'os_type': 'linux', 'os_flavor': 'rocky-9',
                     'architecture': 'x86_64', 'license_type': 'free',
                     'diagnosis_status': 'boot_errors_detected',
                     'boot_errors': [err], 'recommendations': []}
        report = DiagnosisReportFormatter().format_report(diagnosis)
        assert 'Auto-repair (recommended):' in report
        assert 'gce-rescue repair vm --zone=z' in report


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

HARNESS = r'''
export LOGFILE="$WORK/log"
GCE_SELINUX_FIX_LIB_ONLY=1
source "$SCRIPT" 2>/dev/null
SYSROOT="$ROOT"
mountpoint() { [ "$2" = "$SYSROOT" ]; }
sync() { :; }
selinux_repair
'''


class FakeRoot:
    """A fake RHEL-family root filesystem with an SELinux configuration."""

    def __init__(self, tmp_path, mode='enforcing', policy_type='targeted',
                 policy='valid', autorelabel=False, cmdline='console=ttyS0',
                 config=True):
        self.root = tmp_path / 'root'
        self.work = tmp_path / 'work'
        self.work.mkdir()
        r = self.root
        (r / 'etc/selinux').mkdir(parents=True)
        (r / 'etc/default').mkdir()
        (r / 'var/log').mkdir(parents=True)
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
        if autorelabel:
            (r / '.autorelabel').write_text('')

    @property
    def config(self) -> Path:
        return self.root / 'etc/selinux/config'

    @property
    def autorelabel(self) -> Path:
        return self.root / '.autorelabel'

    def backups(self):
        base = self.root / 'var/backups/gce-rescue'
        return sorted(p for p in base.rglob('*') if p.is_file()) \
            if base.exists() else []

    def run(self, findings=''):
        env = dict(os.environ)
        env.update({'SCRIPT': SCRIPT.as_posix(), 'ROOT': self.root.as_posix(),
                    'WORK': self.work.as_posix(),
                    'GCE_SELINUX_FINDINGS': findings})
        proc = subprocess.run([shutil.which('bash'), '-c', HARNESS], env=env,
                              capture_output=True, text=True, timeout=60)
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

    def __repr__(self):
        return f'Result({self.results!r})\n{self.stderr}'


@needs_bash
class TestScriptBehaviour:

    @pytest.mark.parametrize('policy', ['missing', 'empty_dir', 'corrupt'])
    def test_missing_policy_sets_permissive(self, tmp_path, policy):
        root = FakeRoot(tmp_path, policy=policy)
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'SUCCESS:1', res
        text = root.config.read_text()
        assert 'SELINUX=permissive\n' in text
        assert 'SELINUX=enforcing' not in text
        assert 'SELINUXTYPE=targeted\n' in text
        assert text.startswith('# This file controls')
        assert not root.autorelabel.exists()
        backups = root.backups()
        assert len(backups) == 1
        assert backups[0].read_text() == root.config_text
        assert any('selinux-policy-targeted' in line for line in res.lines), res

    def test_policy_installed_schedules_relabel(self, tmp_path):
        root = FakeRoot(tmp_path)
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'SUCCESS:1', res
        assert root.autorelabel.is_file()
        assert root.autorelabel.stat().st_size == 0
        assert root.config.read_text() == root.config_text
        assert root.backups() == []
        assert not (root.root / 'var/backups').exists()
        assert any('/.autorelabel' in line for line in res.lines), res

    def test_existing_autorelabel_with_failed_relabel_falls_back(self, tmp_path):
        root = FakeRoot(tmp_path, autorelabel=True)
        res = root.run('selinux_relabel_required')
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUX=permissive\n' in root.config.read_text()
        assert root.autorelabel.exists()
        assert len(root.backups()) == 1
        assert any('never finished booting' in line for line in res.lines), res

    def test_existing_autorelabel_permissive_already_fails(self, tmp_path):
        root = FakeRoot(tmp_path, mode='permissive', autorelabel=True)
        res = root.run('selinux_relabel_required')
        assert res.result.startswith('FAILED:'), res
        assert root.config.read_text() == root.config_text

    def test_missing_policy_already_permissive_no_change(self, tmp_path):
        root = FakeRoot(tmp_path, mode='permissive', policy='missing')
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'NO_ISSUES:0', res
        assert root.config.read_text() == root.config_text
        assert root.backups() == []

    def test_no_selinux_config_no_issues(self, tmp_path):
        root = FakeRoot(tmp_path, config=False)
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'NO_ISSUES:0', res
        assert not root.autorelabel.exists()

    def test_disabled_no_issues(self, tmp_path):
        root = FakeRoot(tmp_path, mode='disabled', policy='missing')
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'NO_ISSUES:0', res
        assert root.config.read_text() == root.config_text

    def test_cmdline_selinux_0_reported_not_changed(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 selinux=0')
        grub = (root.root / 'etc/default/grub').read_text()
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'SUCCESS:1', res
        assert any('selinux=0' in line and '[INFO]' in line
                   for line in res.lines), res
        assert (root.root / 'etc/default/grub').read_text() == grub

    def test_commented_cmdline_ignored(self, tmp_path):
        root = FakeRoot(tmp_path)
        grub = root.root / 'etc/default/grub'
        grub.write_text(grub.read_text() + '#GRUB_CMDLINE_LINUX="selinux=0"\n')
        res = root.run('selinux_policy_load_failure')
        assert not any('selinux=0' in line for line in res.lines), res

    def test_cmdline_autorelabel_loop_fails_without_change(self, tmp_path):
        root = FakeRoot(tmp_path, cmdline='console=ttyS0 autorelabel=1')
        res = root.run('selinux_relabel_required')
        assert res.result.startswith('FAILED:'), res
        assert 'remove-args=autorelabel' in res.result
        assert not root.autorelabel.exists()
        assert root.config.read_text() == root.config_text

    def test_unsafe_policy_type_treated_as_missing(self, tmp_path):
        root = FakeRoot(tmp_path, policy_type='../../tmp', policy='missing')
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUX=permissive\n' in root.config.read_text()

    def test_bash_syntax(self):
        subprocess.run([shutil.which('bash'), '-n', SCRIPT.as_posix()],
                       check=True)

    def test_absolute_config_symlink_resolved_inside_target(self, tmp_path):
        root = FakeRoot(tmp_path, policy='missing', config=False)
        real = root.root / 'etc/sysconfig/selinux'
        real.parent.mkdir(parents=True)
        real.write_text(root.config_text)
        try:
            os.symlink('/etc/sysconfig/selinux', root.config)
        except (OSError, NotImplementedError):
            pytest.skip('symlinks not available')
        res = root.run('selinux_policy_load_failure')
        assert res.result == 'SUCCESS:1', res
        assert 'SELINUX=permissive\n' in real.read_text()
        assert root.config.is_symlink()

    def test_result_emitted_exactly_once_per_path(self, tmp_path):
        for i, kwargs in enumerate([{}, {'policy': 'missing'},
                                    {'config': False}]):
            sub = tmp_path / str(i)
            sub.mkdir()
            res = FakeRoot(sub, **kwargs).run('selinux_policy_load_failure')
            assert len(res.results) == 1, res
