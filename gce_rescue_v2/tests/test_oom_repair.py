"""Unit tests for the oom machine type repair (issue #204)."""

import logging
from unittest.mock import Mock, patch

import pytest

from gce_rescue_v2.core.diagnosis import latest_boot_output, oom_kills_only_memcg
from gce_rescue_v2.operations.base import OperationResult
from gce_rescue_v2.orchestration.repair import RepairOrchestrator

KERNEL = '6.1.0-28-cloud-amd64'
GLOBAL_KILL = (
    '[  12.345678] java invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE)\n'
    '[  12.345900] oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,'
    'mems_allowed=0,global_oom,task_memcg=/system.slice/app.service,task=java,pid=812\n'
    '[  12.346000] Out of memory: Killed process 812 (java) total-vm:2097152kB\n'
)
MEMCG_KILL = (
    '[  40.100000] python3 invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL)\n'
    '[  40.100200] oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/,'
    'mems_allowed=0,oom_memcg=/kubepods/pod1,task_memcg=/kubepods/pod1,task=python3,pid=901\n'
    '[  40.100300] Memory cgroup out of memory: Killed process 901 (python3)\n'
)


class TestLatestBootOutput:

    def test_keeps_only_the_last_boot(self):
        serial = (f'Linux version {KERNEL} (x) #1\n{GLOBAL_KILL}'
                  f'Linux version {KERNEL} (x) #1\nStartup finished in 5s\n')

        latest = latest_boot_output(serial)

        assert latest.startswith('Linux version')
        assert 'Out of memory' not in latest
        assert 'Startup finished' in latest

    def test_no_banner_returns_everything(self):
        assert latest_boot_output(GLOBAL_KILL) == GLOBAL_KILL

    def test_empty(self):
        assert latest_boot_output('') == ''
        assert latest_boot_output(None) == ''


class TestOomKillsOnlyMemcg:

    def test_global_kill(self):
        assert oom_kills_only_memcg(GLOBAL_KILL) is False

    def test_memcg_kill(self):
        assert oom_kills_only_memcg(MEMCG_KILL) is True

    def test_mixed_kills(self):
        assert oom_kills_only_memcg(MEMCG_KILL + GLOBAL_KILL) is False

    def test_old_kernel_wording(self):
        memcg = 'Memory cgroup out of memory: Kill process 901 (python3) score 900\n'
        global_kill = 'Out of memory: Kill process 812 (java) score 900 or sacrifice child\n'
        assert oom_kills_only_memcg(memcg) is True
        assert oom_kills_only_memcg(memcg + global_kill) is False

    def test_memcg_summary_line_only(self):
        serial = 'oom-kill:constraint=CONSTRAINT_MEMCG,nodemask=(null),cpuset=/\n'
        assert oom_kills_only_memcg(serial) is True

    def test_no_kill(self):
        assert oom_kills_only_memcg('Startup finished in 5s\n') is False
        assert oom_kills_only_memcg('') is False


OOM_DIAGNOSIS = {'boot_errors': [{'category': 'oom', 'name': 'oom_killed_process'}]}
MIG_URL = 'projects/123/zones/zone-a/instanceGroupManagers/web'


def _orchestrator(vm_info=None, serial=''):
    logger = logging.getLogger('test_oom_repair')
    logger.console_level = logging.WARNING
    orch = RepairOrchestrator(Mock(), 'proj', 'zone-a', 'vm-1', logger=logger)
    orch._vm_info = vm_info if vm_info is not None else {
        'machineType': 'https://www.googleapis.com/compute/v1/projects/proj/'
                       'zones/zone-a/machineTypes/e2-small',
    }
    orch._os_type = 'linux'
    compute = Mock()
    compute.instances.return_value.getSerialPortOutput.return_value \
        .execute.return_value = {'contents': serial}
    orch._create_tracked_client = lambda label: compute
    orch.tracked_compute = compute
    return orch


class TestOomResizeBlocker:

    def test_applies_to_vm_wide_kill(self):
        assert _orchestrator(serial=GLOBAL_KILL).oom_resize_blocker(OOM_DIAGNOSIS) is None

    def test_no_oom_finding(self):
        diagnosis = {'boot_errors': [{'category': 'fstab'}]}
        reason = _orchestrator(serial=GLOBAL_KILL).oom_resize_blocker(diagnosis)
        assert 'no out-of-memory' in reason

    def test_memcg_kills_only(self):
        reason = _orchestrator(serial=MEMCG_KILL).oom_resize_blocker(OOM_DIAGNOSIS)
        assert 'memory cgroup' in reason

    def test_mig_member(self):
        vm = {'machineType': 'e2-small',
              'metadata': {'items': [{'key': 'created-by', 'value': MIG_URL}]}}
        reason = _orchestrator(vm, GLOBAL_KILL).oom_resize_blocker(OOM_DIAGNOSIS)
        assert 'instance template' in reason


class TestProposeMachineType:

    def test_lists_every_page(self):
        orch = _orchestrator()
        machine_types = orch.tracked_compute.machineTypes.return_value
        page1, page2 = Mock(), Mock()
        page1.execute.return_value = {'items': [{'name': 'e2-small', 'memoryMb': 2048}]}
        page2.execute.return_value = {'items': [{'name': 'e2-medium', 'memoryMb': 4096}]}
        machine_types.list.return_value = page1
        machine_types.list_next.side_effect = [page2, None]

        proposal = orch.propose_machine_type()

        assert proposal['current'] == 'e2-small'
        assert proposal['proposed']['name'] == 'e2-medium'
        assert set(proposal['machine_types']) == {'e2-small', 'e2-medium'}

    def test_no_larger_type(self):
        orch = _orchestrator()
        machine_types = orch.tracked_compute.machineTypes.return_value
        machine_types.list.return_value.execute.return_value = {
            'items': [{'name': 'e2-small', 'memoryMb': 2048}]}
        machine_types.list_next.return_value = None

        assert orch.propose_machine_type()['proposed'] is None


def _ok(name, rollback_data=None):
    return OperationResult(operation_name=name, success=True, message='ok',
                           rollback_data=rollback_data)


def _failed(name, error):
    return OperationResult(operation_name=name, success=False, message=error,
                           error=error)


class TestExecuteMachineTypeChange:

    @pytest.fixture(autouse=True)
    def _ops(self):
        with patch('gce_rescue_v2.orchestration.repair.StopVMOperation') as stop, \
                patch('gce_rescue_v2.orchestration.repair.SetMachineTypeOperation') as set_type, \
                patch('gce_rescue_v2.orchestration.repair.StartVMOperation') as start:
            self.stop = stop.return_value
            self.set_type = set_type.return_value
            self.start = start.return_value
            self.stop.execute.return_value = _ok(
                'Stop VM', {'vm_name': 'vm-1', 'original_status': 'RUNNING'})
            self.set_type.execute.return_value = _ok('Set Machine Type', {
                'vm_name': 'vm-1', 'previous_machine_type': 'e2-small',
                'new_machine_type': 'e2-medium'})
            self.start.execute.return_value = _ok('Start VM')
            self.set_type.rollback.return_value = True
            self.orch = _orchestrator()
            self.orch._verify_boot_after_repair = Mock(
                return_value={'verified': True, 'errors': []})
            yield

    def test_stop_change_start_verify(self):
        result = self.orch.execute_machine_type_change('e2-medium')

        assert result['status'] == 'success'
        assert result['previous_machine_type'] == 'e2-small'
        assert result['new_machine_type'] == 'e2-medium'
        assert result['boot_verified'] is True
        assert result['fix_lines'] == ['Machine type changed e2-small -> e2-medium']
        self.set_type.execute.assert_called_once()
        assert self.set_type.execute.call_args.kwargs['machine_type'] == 'e2-medium'
        self.orch._verify_boot_after_repair.assert_called_once_with(latest_boot_only=True)

    @pytest.mark.parametrize('force', [True, False])
    def test_local_ssd_discard_follows_force(self, force):
        self.orch.config.force = force

        self.orch.execute_machine_type_change('e2-medium')

        assert self.stop.execute.call_args.kwargs['discard_local_ssd'] is force

    def test_stop_failure_changes_nothing(self):
        self.stop.execute.return_value = _failed('Stop VM', 'stop timeout')

        result = self.orch.execute_machine_type_change('e2-medium')

        assert result['status'] == 'failed'
        assert result['error'] == 'stop timeout'
        self.set_type.execute.assert_not_called()

    def test_type_change_failure_restarts_vm(self):
        self.set_type.execute.return_value = _failed('Set Machine Type', 'CPU quota')

        result = self.orch.execute_machine_type_change('e2-medium')

        assert result['status'] == 'failed'
        assert result['error'] == 'CPU quota'
        self.stop.rollback.assert_called_once()
        self.start.execute.assert_not_called()

    def test_start_failure_restores_original_type(self):
        self.start.execute.side_effect = [
            _failed('Start VM', 'ZONE_RESOURCE_POOL_EXHAUSTED'), _ok('Start VM')]

        result = self.orch.execute_machine_type_change('e2-medium')

        assert result['status'] == 'failed'
        assert 'ZONE_RESOURCE_POOL_EXHAUSTED' in result['error']
        self.set_type.rollback.assert_called_once()
        assert self.start.execute.call_count == 2
        self.orch._verify_boot_after_repair.assert_not_called()

    def test_start_failure_without_restore_skips_restart(self):
        self.start.execute.return_value = _failed('Start VM', 'no capacity')
        self.set_type.rollback.return_value = False

        result = self.orch.execute_machine_type_change('e2-medium')

        assert result['status'] == 'failed'
        assert self.start.execute.call_count == 1


class TestVerifyLatestBootOnly:
    """A kill from a boot before the repair must not fail the verification."""

    OLD_BOOT = f'[0.0] Linux version {KERNEL} (x) #1 SMP\n' + GLOBAL_KILL
    NEW_BOOT = (f'[0.0] Linux version {KERNEL} (x) #1 SMP\n'
                '[12.0] systemd[1]: Startup finished in 12.0s (kernel) + 9s.\n')

    def _verify(self, serial, latest_boot_only):
        orch = _orchestrator(serial=serial)
        with patch('gce_rescue_v2.orchestration.repair.time.sleep'), \
                patch('gce_rescue_v2.orchestration.repair.sys.stdout'):
            return orch._verify_boot_after_repair(latest_boot_only=latest_boot_only)

    def test_kill_in_earlier_boot_is_ignored(self):
        result = self._verify(self.OLD_BOOT + self.NEW_BOOT, latest_boot_only=True)
        assert result['verified'] is True

    def test_whole_buffer_still_reports_it(self):
        result = self._verify(self.OLD_BOOT + self.NEW_BOOT, latest_boot_only=False)
        assert result['verified'] is False

    def test_kill_in_latest_boot_is_reported(self):
        result = self._verify(self.NEW_BOOT + GLOBAL_KILL, latest_boot_only=True)
        assert result['verified'] is False
        assert any(e.startswith('oom:') for e in result['errors'])


MICRO, SMALL, MEDIUM, STANDARD_2 = (
    {'name': 'e2-micro', 'memoryMb': 1024},
    {'name': 'e2-small', 'memoryMb': 2048},
    {'name': 'e2-medium', 'memoryMb': 4096},
    {'name': 'e2-standard-2', 'memoryMb': 8192},
)
OOM_ERROR = {
    'category': 'oom', 'severity': 'warning', 'name': 'oom_killed_process',
    'description': 'The kernel out-of-memory killer terminated a process',
    'detected_pattern': 'Out of memory: Killed process 812', 'suggested_fixes': [],
}
GRUB_ERROR = {
    'category': 'grub', 'severity': 'critical', 'name': 'grub_rescue',
    'description': 'GRUB rescue prompt', 'detected_pattern': 'grub rescue>',
    'suggested_fixes': [],
}
SUCCESS = {'status': 'success', 'fixed_count': 1, 'fix_lines': [], 'error': None,
           'snapshot_name': None, 'duration_seconds': 60, 'boot_verified': True,
           'boot_errors_after': []}


def _cli_args(quiet=False, machine_type=None):
    import argparse
    return argparse.Namespace(
        instance_name='vm-1', zone='us-central1-a', project='proj',
        verbosity='info', format='disable', quiet=quiet, force=False,
        snapshot=True, rescue_image=None, fix_script=None,
        resize_disk_gb=None, verification_timeout=None,
        machine_type=machine_type,
    )


class TestRepairCliOom:
    """handle_repair with oom findings (issue #204)."""

    def _run(self, args, boot_errors=(OOM_ERROR,), fixable=(), inputs=(),
             blocker=None, proposed=MEDIUM, repair_result=SUCCESS):
        from gce_rescue_v2.cli.repair import handle_repair
        from gce_rescue_v2.core.config import RescueConfig

        vm_info = {
            'status': 'RUNNING',
            'machineType': 'zones/us-central1-a/machineTypes/e2-small',
            'disks': [{'boot': True, 'deviceName': 'boot-disk',
                       'source': 'projects/proj/zones/us-central1-a/disks/boot-disk'}],
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.return_value = vm_info

        orch = Mock()
        orch.config = RescueConfig()
        orch.validate.return_value = True
        orch.diagnose.return_value = {
            'vm_name': 'vm-1', 'zone': 'us-central1-a', 'status': 'RUNNING',
            'diagnosis_status': 'boot_errors_detected',
            'boot_errors': list(boot_errors),
        }
        orch.get_fixable_categories.return_value = list(fixable)
        orch.get_unfixable_categories.return_value = [
            e['category'] for e in boot_errors if e['category'] not in fixable]
        orch._extract_fstab_targets.return_value = []
        orch.oom_resize_blocker.return_value = blocker
        orch.propose_machine_type.return_value = {
            'current': 'e2-small', 'proposed': proposed,
            'machine_types': {
                mt['name']: mt for mt in (MICRO, SMALL, MEDIUM, STANDARD_2)},
        }
        orch.execute.return_value = repair_result
        orch.execute_machine_type_change.return_value = dict(
            SUCCESS, fix_lines=['Machine type changed e2-small -> e2-medium'],
            new_machine_type='e2-medium', previous_machine_type='e2-small')

        auth = Mock()
        auth.get_client.return_value = (compute, 'proj')
        self.input = Mock(side_effect=list(inputs))
        with patch('gce_rescue_v2.core.auth.AuthManager', return_value=auth), \
                patch('gce_rescue_v2.cli.repair._create_tracked_client',
                      return_value=compute), \
                patch('gce_rescue_v2.cli.preflight.check_image_org_policy',
                      return_value=None), \
                patch('gce_rescue_v2.orchestration.repair.RepairOrchestrator',
                      return_value=orch), \
                patch('gce_rescue_v2.cli.repair._Spinner'), \
                patch('builtins.input', self.input):
            rc = handle_repair(args)
        return rc, orch

    def test_flag_parsed_into_config(self):
        from gce_rescue_v2.cli import args_to_rescue_config, create_parser

        args = create_parser().parse_args([
            'repair', 'vm-1', '--zone=us-central1-a', '--machine-type=e2-medium'])

        assert args.machine_type == 'e2-medium'
        assert args_to_rescue_config(args).machine_type == 'e2-medium'

    def test_interactive_accepts_proposal(self):
        rc, orch = self._run(_cli_args(), inputs=['y', 'y'])

        assert rc == 0
        orch.execute_machine_type_change.assert_called_once_with('e2-medium')
        orch.execute.assert_not_called()

    def test_interactive_typed_type(self):
        rc, orch = self._run(_cli_args(), inputs=['e2-standard-2', 'y'])

        assert rc == 0
        orch.execute_machine_type_change.assert_called_once_with('e2-standard-2')

    def test_interactive_unknown_type_asks_again(self, capsys):
        rc, orch = self._run(_cli_args(), inputs=['e2-huge', '', 'y'])

        assert rc == 0
        assert 'not available in zone' in capsys.readouterr().out
        orch.execute_machine_type_change.assert_called_once_with('e2-medium')

    def test_interactive_no_proposal_requires_a_type(self, capsys):
        rc, orch = self._run(_cli_args(), proposed=None,
                             inputs=['', 'e2-medium', 'y'])

        assert rc == 0
        assert 'No larger machine type' in capsys.readouterr().out
        orch.execute_machine_type_change.assert_called_once_with('e2-medium')

    def test_interactive_skip(self):
        rc, orch = self._run(_cli_args(), inputs=['n'])

        assert rc == 0
        orch.execute_machine_type_change.assert_not_called()

    def test_interactive_declined_plan(self):
        rc, orch = self._run(_cli_args(), inputs=['y', 'n'])

        assert rc == 0
        orch.execute_machine_type_change.assert_not_called()

    def test_quiet_without_flag_exits_non_zero(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True))

        assert rc == 1
        out = capsys.readouterr().out
        assert 'requires explicit --machine-type' in out
        assert '--quiet --machine-type=e2-medium' in out
        orch.execute_machine_type_change.assert_not_called()
        self.input.assert_not_called()

    def test_quiet_with_flag(self):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-medium'))

        assert rc == 0
        orch.execute_machine_type_change.assert_called_once_with('e2-medium')
        self.input.assert_not_called()

    def test_unknown_flag_type_stops_before_any_change(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-huge'))

        assert rc == 1
        assert 'not available in zone' in capsys.readouterr().err
        orch.execute_machine_type_change.assert_not_called()

    def test_same_type_is_refused(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-small'))

        assert rc == 1
        assert 'already uses' in capsys.readouterr().err
        orch.execute_machine_type_change.assert_not_called()

    def test_smaller_type_warns_but_proceeds(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-micro'))

        assert rc == 0
        assert 'has no more memory' in capsys.readouterr().out
        orch.execute_machine_type_change.assert_called_once_with('e2-micro')

    def test_blocker_reason_shown(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-medium'),
                             blocker='the VM belongs to a managed instance group')

        assert 'managed instance group' in capsys.readouterr().out
        orch.propose_machine_type.assert_not_called()
        orch.execute_machine_type_change.assert_not_called()

    def test_blocked_oom_alone_does_not_suggest_rescue_mode(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True),
                             blocker='every kill hit a memory cgroup limit')

        out = capsys.readouterr().out
        assert rc == 0
        assert 'memory cgroup limit' in out
        assert 'not yet available' not in out
        assert 'gce-rescue rescue' not in out
        orch.execute.assert_not_called()

    def test_blocked_oom_with_other_repair_is_not_listed_as_manual(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True),
                             boot_errors=[GRUB_ERROR, OOM_ERROR], fixable=['grub'],
                             blocker='every kill hit a memory cgroup limit')

        out = capsys.readouterr().out
        assert rc == 0
        assert '[OOM] requires manual repair' not in out
        orch.execute.assert_called_once()
        orch.execute_machine_type_change.assert_not_called()

    def test_blocked_oom_keeps_other_manual_findings(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True),
                             boot_errors=[OOM_ERROR, GRUB_ERROR],
                             blocker='every kill hit a memory cgroup limit')

        out = capsys.readouterr().out
        assert rc == 0
        assert 'Detected [GRUB] issue but automated fix is not yet available' in out
        assert 'Detected [OOM]' not in out
        assert 'gce-rescue rescue' in out

    def test_flag_without_oom_is_ignored(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-medium'),
                             boot_errors=[GRUB_ERROR], fixable=['grub'])

        assert rc == 0
        assert '--machine-type ignored' in capsys.readouterr().out
        orch.execute.assert_called_once()
        orch.execute_machine_type_change.assert_not_called()

    def test_with_other_repair_runs_it_first(self):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-medium'),
                             boot_errors=[GRUB_ERROR, OOM_ERROR], fixable=['grub'])

        assert rc == 0
        calls = [c[0] for c in orch.mock_calls
                 if c[0] in ('execute', 'execute_machine_type_change')]
        assert calls == ['execute', 'execute_machine_type_change']

    def test_with_other_repair_interactive_plan(self, capsys):
        rc, orch = self._run(_cli_args(), boot_errors=[GRUB_ERROR, OOM_ERROR],
                             fixable=['grub'], inputs=['y', 'y'])

        assert rc == 0
        assert 'change machine type e2-small -> e2-medium' in capsys.readouterr().out
        orch.execute.assert_called_once()
        orch.execute_machine_type_change.assert_called_once_with('e2-medium')

    def test_failed_other_repair_skips_type_change(self, capsys):
        rc, orch = self._run(_cli_args(quiet=True, machine_type='e2-medium'),
                             boot_errors=[GRUB_ERROR, OOM_ERROR], fixable=['grub'],
                             repair_result=dict(SUCCESS, status='rescue_failed',
                                                error='Rescue phase failed'))

        assert rc == 1
        assert 'not attempted' in capsys.readouterr().err
        orch.execute_machine_type_change.assert_not_called()

    def test_quiet_skip_with_other_repair_exits_non_zero(self):
        rc, orch = self._run(_cli_args(quiet=True),
                             boot_errors=[GRUB_ERROR, OOM_ERROR], fixable=['grub'])

        assert rc == 1
        orch.execute.assert_called_once()
        orch.execute_machine_type_change.assert_not_called()


class TestShowMachineTypeResults:

    def _show(self, **result):
        from gce_rescue_v2.cli.repair import _show_machine_type_results
        base = {'new_machine_type': 'e2-medium', 'fix_lines': [],
                'error': 'no capacity'}
        return _show_machine_type_results(dict(base, **result), 'vm-1',
                                          zone='zone-a', project='proj')

    def test_success(self, capsys):
        assert self._show(status='success', boot_verified=True,
                          fix_lines=['Machine type changed e2-small -> e2-medium']) == 0
        assert 'Machine type changed e2-small -> e2-medium' in capsys.readouterr().out

    def test_oom_still_present(self, capsys):
        assert self._show(status='success', boot_verified=False,
                          boot_errors_after=['oom: killed']) == 0
        assert 'Run repair again' in capsys.readouterr().out

    def test_type_not_changed(self, capsys):
        assert self._show(status='failed') == 1
        assert 'was not changed' in capsys.readouterr().err

    def test_original_type_restored(self, capsys):
        assert self._show(status='failed', previous_machine_type='e2-small',
                          restored=True) == 1
        assert 'original machine type e2-small' in capsys.readouterr().err

    def test_not_restored(self, capsys):
        assert self._show(status='failed', previous_machine_type='e2-small',
                          restored=False) == 1
        assert 'gcloud compute instances describe vm-1' in capsys.readouterr().err
