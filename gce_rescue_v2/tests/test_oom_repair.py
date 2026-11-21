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
