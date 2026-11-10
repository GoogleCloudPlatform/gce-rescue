"""Tests for the API-only startup_script repair (orchestration/startup_script_repair.py)."""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock, patch

import pytest

from gce_rescue_v2.operations.base import OperationResult
from gce_rescue_v2.operations.reset_vm import ResetVMOperation
from gce_rescue_v2.operations.set_metadata import SetMetadataOperation
from gce_rescue_v2.orchestration import startup_script_repair as ssr
from gce_rescue_v2.orchestration.startup_script_repair import (
    BOOT_WAIT_SECONDS,
    POLL_SECONDS,
    DisablePlan,
    StartupScriptRepair,
    disable_keys,
    disabled_key,
    failing_keys_in_latest_boot,
    failing_startup_script_keys,
    managing_instance_group,
    plan_disable,
    restore_instructions,
    script_output_in_latest_boot,
)


def _finding(pattern: str, category: str = 'startup_script') -> Dict[str, Any]:
    return {'category': category, 'severity': 'warning',
            'description': 'GCE startup script exited with a non-zero status',
            'detected_pattern': pattern}


def _items(**kv: str) -> List[Dict[str, str]]:
    return [{'key': k.replace('_', '-'), 'value': v} for k, v in kv.items()]


# A boot that completed and whose script runner had nothing left to run.
CLEAN_BOOT = (
    "[    0.000000] Linux version 6.1.0-54-cloud-amd64 (debian-kernel) #1 SMP\n"
    "systemd[1]: Started ssh.service - OpenBSD Secure Shell server.\n"
    "google_metadata_script_runner[901]: No startup scripts to run\n"
    "google_metadata_script_runner[901]: Finished running startup scripts\n"
    "systemd[1]: Reached target multi-user.target - Multi-User System.\n"
)
# Booted, but another startup script still fails.
FAILING_BOOT = CLEAN_BOOT.replace(
    "No startup scripts to run",
    'Script "startup-script-url" failed with error: exit status 2')
# Reached a boot-success marker before the script runner reported.
BOOT_WITHOUT_RUNNER = (
    "[    0.000000] Linux version 6.1.0-54-cloud-amd64 (debian-kernel) #1 SMP\n"
    "systemd[1]: Started ssh.service - OpenBSD Secure Shell server.\n"
    "systemd[1]: Starting google-startup-scripts.service ...\n"
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestFailingStartupScriptKeys:

    @pytest.mark.parametrize('pattern,expected', [
        ('startup-script exit status 1', ['startup-script']),
        ('startup-script-url exit status 127', ['startup-script-url']),
        ('Script "startup-script" failed', ['startup-script']),
        ('Script "startup-script-url" failed', ['startup-script-url']),
    ])
    def test_names_the_linux_key(self, pattern: str, expected: List[str]) -> None:
        assert failing_startup_script_keys([_finding(pattern)]) == expected

    @pytest.mark.parametrize('pattern', [
        'windows-startup-script-ps1 exit status 1',
        'windows-startup-script-url exit status 1',
        'sysprep-specialize-script-cmd exit status 1',
        'Script "windows-startup-script-ps1" failed',
    ])
    def test_ignores_windows_and_sysprep_keys(self, pattern: str) -> None:
        assert failing_startup_script_keys([_finding(pattern)]) == []

    def test_dedupes_and_keeps_order(self) -> None:
        findings = [_finding('startup-script-url exit status 1'),
                    _finding('Script "startup-script" failed'),
                    _finding('startup-script-url exit status 1')]
        assert failing_startup_script_keys(findings) == [
            'startup-script-url', 'startup-script']

    def test_ignores_other_categories_and_missing_pattern(self) -> None:
        findings = [_finding('startup-script exit status 1', category='fstab'),
                    {'category': 'startup_script'}]
        assert failing_startup_script_keys(findings) == []


class TestPlanDisable:

    def test_all_four_buckets(self) -> None:
        items = _items(**{
            'startup_script': 'a',                                  # disable
            'gce_rescue_disabled_startup_script_url': 'b',          # already
        })
        # Keys are normalised by _items: underscores become dashes.
        plan = plan_disable(items, ['startup-script', 'startup-script-url'])
        assert plan == DisablePlan(disable=['startup-script'],
                                   already_disabled=['startup-script-url'])

        conflict = items + [{'key': 'gce-rescue-disabled-startup-script', 'value': 'old'}]
        assert plan_disable(conflict, ['startup-script']).conflicts == ['startup-script']
        assert plan_disable([], ['startup-script']).not_on_instance == ['startup-script']


class TestDisableKeys:

    def test_moves_keys_to_saved_copies_and_keeps_order(self) -> None:
        items = [{'key': 'a', 'value': '1'},
                 {'key': 'startup-script', 'value': 'echo hi; exit 1'},
                 {'key': 'b', 'value': '2'}]
        assert disable_keys(items, ['startup-script']) == [
            {'key': 'a', 'value': '1'},
            {'key': 'b', 'value': '2'},
            {'key': 'gce-rescue-disabled-startup-script', 'value': 'echo hi; exit 1'},
        ]

    def test_saved_prefix_is_not_the_rescue_backup_prefix(self) -> None:
        """restore turns rescue-backup-<key> back into <key>; the saved copy
        must not use that prefix or a later restore re-enables the script."""
        assert not disabled_key('startup-script').startswith('rescue-backup-')


BANNER = "[    0.000000] Linux version 6.1.0-54-cloud-amd64 (debian-kernel) #1 SMP\n"
RUNNER = "google_metadata_script_runner[9]: "
# Layered bootstrap as seen live: the inline script fails, then the URL one.
LAYERED_BOOT = (
    BANNER
    + RUNNER + 'Metadata key("startup-script"), command("/bin/bash"): app: configuring\r\n'
    + RUNNER + 'Script "startup-script" failed with error: exit status 4\n'
    + RUNNER + 'Metadata key("startup-script-url"), command("/bin/bash"): base: agent\n'
    + RUNNER + 'Script "startup-script-url" failed with error: exit status 3\n'
)


class TestLatestBootHelpers:

    def test_all_failing_keys_in_the_order_they_failed(self) -> None:
        assert failing_keys_in_latest_boot(LAYERED_BOOT) == [
            'startup-script', 'startup-script-url']

    def test_keys_from_an_older_boot_are_ignored(self) -> None:
        serial = LAYERED_BOOT + BANNER + RUNNER + "No startup scripts to run\n"
        assert failing_keys_in_latest_boot(serial) == []

    def test_windows_keys_are_ignored(self) -> None:
        serial = BANNER + 'Script "windows-startup-script-ps1" failed with error\n'
        assert failing_keys_in_latest_boot(serial) == []

    def test_script_output_per_key_last_lines(self) -> None:
        lines = ''.join(
            RUNNER + f'Metadata key("startup-script"), command("/bin/bash"): line {i}\n'
            for i in range(8))
        output = script_output_in_latest_boot(BANNER + lines, ['startup-script'], limit=3)
        assert output == {'startup-script': ['line 5', 'line 6', 'line 7']}
        assert script_output_in_latest_boot(LAYERED_BOOT, ['startup-script-url']) == {
            'startup-script-url': ['base: agent']}

    def test_managing_instance_group(self) -> None:
        zonal = managing_instance_group([{
            'key': 'created-by',
            'value': 'projects/1/zones/us-central1-a/instanceGroupManagers/web-mig'}])
        assert (zonal.name, zonal.location_flag) == ('web-mig', '--zone=us-central1-a')
        regional = managing_instance_group([{
            'key': 'created-by',
            'value': 'projects/1/regions/us-central1/instanceGroupManagers/api'}])
        assert (regional.name, regional.location_flag) == ('api', '--region=us-central1')
        assert managing_instance_group(
            [{'key': 'created-by', 'value': 'projects/1/zones/z/instances/x'}]) is None
        assert managing_instance_group([]) is None


class TestRestoreInstructions:

    def test_inline_script(self) -> None:
        lines = restore_instructions(['startup-script'], 'vm-1', 'z', 'p')
        text = '\n'.join(lines)
        assert ('--format="value(metadata.items.extract('
                'gce-rescue-disabled-startup-script))"') in text
        assert '> startup-script.sh' in text
        assert '--metadata-from-file=startup-script=startup-script.sh' in text
        assert '--keys=gce-rescue-disabled-startup-script' in text

    def test_url_script(self) -> None:
        text = '\n'.join(restore_instructions(['startup-script-url'], 'vm-1', 'z', 'p'))
        assert '--metadata=startup-script-url=FIXED_URL' in text
        assert '--keys=gce-rescue-disabled-startup-script-url' in text


# ---------------------------------------------------------------------------
# StartupScriptRepair.run
# ---------------------------------------------------------------------------

class FakeCompute:
    """Minimal Compute client: instances().get / getSerialPortOutput."""

    def __init__(self, items: List[Dict[str, str]], serial: List[str],
                 get_error: Optional[Exception] = None,
                 serial_error: Optional[Exception] = None,
                 current_serial: str = 'old boot',
                 disks: Optional[List[Dict[str, Any]]] = None) -> None:
        self.items = items
        self.current_serial = current_serial
        self.disks = disks or []
        self.serial = list(serial)     # successive post-reset reads
        self.get_error = get_error
        self.serial_error = serial_error
        self.serial_starts: List[int] = []

    def instances(self) -> 'FakeCompute':
        return self

    def get(self, **_: Any) -> Mock:
        if self.get_error:
            return Mock(execute=Mock(side_effect=self.get_error))
        return Mock(execute=Mock(return_value={
            'metadata': {'fingerprint': 'fp-1', 'items': self.items},
            'disks': self.disks}))

    def getSerialPortOutput(self, start: int, **_: Any) -> Mock:
        self.serial_starts.append(start)
        if self.serial_error:
            return Mock(execute=Mock(side_effect=self.serial_error))
        if start == 0:
            return Mock(execute=Mock(return_value={'contents': self.current_serial,
                                                   'next': 4096}))
        contents = self.serial.pop(0) if len(self.serial) > 1 else self.serial[0]
        return Mock(execute=Mock(return_value={'contents': contents, 'next': 9999}))


def _ok(name: str) -> OperationResult:
    return OperationResult(operation_name=name, success=True, message='ok')


def _fail(name: str, error: str) -> OperationResult:
    return OperationResult(operation_name=name, success=False, message='x', error=error)


@pytest.fixture
def ops(monkeypatch):
    """Replace the two mutating operations with recorders."""
    calls: Dict[str, Any] = {'set': [], 'reset': []}
    results = {'set': _ok('Set Metadata'), 'reset': _ok('Reset VM')}

    class FakeSet:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def execute(self, vm_name: str, items: List[Dict[str, str]], **kw: Any):
            calls['set'].append({'vm': vm_name, 'items': items, **kw})
            return results['set']

    class FakeReset:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def execute(self, vm_name: str, **kw: Any):
            calls['reset'].append(vm_name)
            return results['reset']

    monkeypatch.setattr(ssr, 'SetMetadataOperation', FakeSet)
    monkeypatch.setattr(ssr, 'ResetVMOperation', FakeReset)
    monkeypatch.setattr(ssr.sys, 'stdout', Mock())
    return calls, results


def _repair(compute: FakeCompute, sleeps: Optional[List[float]] = None) -> StartupScriptRepair:
    sleeps = sleeps if sleeps is not None else []
    return StartupScriptRepair(compute, 'p', 'z', 'vm-1', sleep=sleeps.append)


FAILING = [_finding('Script "startup-script" failed')]


class TestStartupScriptRepairRun:

    def test_success_disables_resets_and_verifies(self, ops) -> None:
        calls, _ = ops
        compute = FakeCompute(_items(keep_me='yes', startup_script='exit 1'), [CLEAN_BOOT])
        result = _repair(compute).run(FAILING)

        assert result['status'] == 'success'
        assert result['boot_verified'] is True
        assert result['fixed_count'] == 1
        assert result['fix_lines'] == [
            '[FIXED] startup_script: disabled startup-script '
            '(saved as gce-rescue-disabled-startup-script)']
        (set_call,) = calls['set']
        assert set_call['items'] == [
            {'key': 'keep-me', 'value': 'yes'},
            {'key': 'gce-rescue-disabled-startup-script', 'value': 'exit 1'}]
        assert set_call['preserve_existing'] is False
        assert set_call['expected_fingerprint'] == 'fp-1'
        assert calls['reset'] == ['vm-1']
        # Offset recorded before the change; verification reads from it.
        assert compute.serial_starts[0] == 0
        assert set(compute.serial_starts[1:]) == {4096}
        assert any('remove-metadata' in line for line in result['next_steps'])

    def test_new_failure_after_reset_is_reported(self, ops) -> None:
        compute = FakeCompute(_items(startup_script='exit 1'), [FAILING_BOOT])
        result = _repair(compute).run(FAILING)
        assert result['status'] == 'success'
        assert result['boot_verified'] is False
        assert any(e.startswith('startup_script:') for e in result['boot_errors_after'])

    def test_waits_for_script_runner_before_judging(self, ops) -> None:
        """A boot-success marker alone does not end the wait: the runner's
        result (here a new failure) comes later and must be seen."""
        sleeps: List[float] = []
        compute = FakeCompute(_items(startup_script='exit 1'),
                              [BOOT_WITHOUT_RUNNER, FAILING_BOOT])
        result = _repair(compute, sleeps).run(FAILING)
        assert result['boot_verified'] is False
        assert len(sleeps) == 2

    def test_runner_never_reports_completed_boot_is_analyzed(self, ops) -> None:
        sleeps: List[float] = []
        compute = FakeCompute(_items(startup_script='exit 1'), [BOOT_WITHOUT_RUNNER])
        result = _repair(compute, sleeps).run(FAILING)
        assert result['boot_verified'] is True
        assert sum(sleeps) == BOOT_WAIT_SECONDS

    def test_boot_never_completes(self, ops) -> None:
        sleeps: List[float] = []
        compute = FakeCompute(_items(startup_script='exit 1'), ['still booting ...'])
        result = _repair(compute, sleeps).run(FAILING)
        assert result['status'] == 'success'
        assert result['boot_verified'] is None
        assert 'did not finish' in result['boot_errors_after'][0]
        assert sleeps == [POLL_SECONDS] * (BOOT_WAIT_SECONDS // POLL_SECONDS)

    def test_serial_read_errors_during_verify_are_retried(self, ops) -> None:
        compute = FakeCompute(_items(startup_script='exit 1'), [CLEAN_BOOT])
        reads = {'n': 0}
        original = compute.getSerialPortOutput

        def flaky(start: int, **kw: Any):
            if start and reads['n'] == 0:
                reads['n'] += 1
                return Mock(execute=Mock(side_effect=RuntimeError('503')))
            return original(start=start, **kw)

        compute.getSerialPortOutput = flaky
        assert _repair(compute).run(FAILING)['boot_verified'] is True

    def test_already_disabled_skips_metadata_but_resets(self, ops) -> None:
        calls, _ = ops
        compute = FakeCompute([{'key': 'gce-rescue-disabled-startup-script',
                                'value': 'exit 1'}], [CLEAN_BOOT])
        result = _repair(compute).run(FAILING)
        assert result['status'] == 'success'
        assert calls['set'] == []
        assert calls['reset'] == ['vm-1']
        assert 'already disabled' in result['fix_lines'][0]

    @pytest.mark.parametrize('items,boot_errors,needle', [
        # Saved copy exists next to a failing key: never overwritten.
        ([{'key': 'startup-script', 'value': 'new'},
          {'key': 'gce-rescue-disabled-startup-script', 'value': 'old'}],
         FAILING, 'not overwritten'),
        # Not on the instance: inherited from project metadata.
        ([], FAILING, 'project metadata'),
        # Finding names no Linux key.
        ([], [_finding('windows-startup-script-ps1 exit status 1')], 'nothing to disable'),
    ])
    def test_not_applied_changes_nothing(self, ops, items, boot_errors, needle) -> None:
        calls, _ = ops
        result = _repair(FakeCompute(items, [CLEAN_BOOT])).run(boot_errors)
        assert result['status'] == 'not_applied'
        assert needle in result['error']
        assert calls['set'] == [] and calls['reset'] == []

    def test_conflict_guidance_names_the_saved_copy(self, ops) -> None:
        items = [{'key': 'startup-script', 'value': 'new'},
                 {'key': 'gce-rescue-disabled-startup-script', 'value': 'old'}]
        result = _repair(FakeCompute(items, [CLEAN_BOOT])).run(FAILING)
        assert any('--keys=gce-rescue-disabled-startup-script' in line
                   for line in result['guidance'])

    def test_instance_read_failure_changes_nothing(self, ops) -> None:
        calls, _ = ops
        compute = FakeCompute([], [CLEAN_BOOT], get_error=RuntimeError('403 denied'))
        result = _repair(compute).run(FAILING)
        assert result['status'] == 'error'
        assert 'Nothing was changed' in result['error']
        assert calls['set'] == [] and calls['reset'] == []

    def test_serial_offset_failure_changes_nothing(self, ops) -> None:
        calls, _ = ops
        compute = FakeCompute(_items(startup_script='exit 1'), [CLEAN_BOOT],
                              serial_error=RuntimeError('serial unavailable'))
        result = _repair(compute).run(FAILING)
        assert result['status'] == 'error'
        assert calls['set'] == [] and calls['reset'] == []

    def test_metadata_failure_does_not_reset(self, ops) -> None:
        calls, results = ops
        results['set'] = _fail('Set Metadata', 'fingerprint mismatch')
        result = _repair(FakeCompute(_items(startup_script='exit 1'), [CLEAN_BOOT])).run(FAILING)
        assert result['status'] == 'error'
        assert 'fingerprint mismatch' in result['error']
        assert 'Nothing was changed' in result['error']
        assert calls['reset'] == []

    def test_reset_failure_reports_what_was_done(self, ops) -> None:
        _, results = ops
        results['reset'] = _fail('Reset VM', 'Failed to reset instance [vm-1]: 500')
        result = _repair(FakeCompute(_items(startup_script='exit 1'), [CLEAN_BOOT])).run(FAILING)
        assert result['status'] == 'error'
        assert 'already disabled' in result['error']
        assert result['fix_lines'] and result['next_steps']


EMERGENCY_BOOT = (
    BANNER
    + "systemd[1]: dev-disk-by\\x2duuid-0badc0de.device: Job timed out.\n"
    + "You are in emergency mode. After logging in, type \"journalctl -xb\"\n"
)


class TestStartupScriptRepairScenarios:
    """Behaviour found with real bootstrap scripts (see the PR's live tests)."""

    def test_layered_bootstrap_disables_every_failing_key_in_one_run(self, ops) -> None:
        calls, _ = ops
        compute = FakeCompute(
            _items(startup_script='exit 4', startup_script_url='gs://b/base.sh'),
            [CLEAN_BOOT], current_serial=LAYERED_BOOT)
        # Diagnosis names only the last failure (the URL script).
        result = _repair(compute).run([_finding('Script "startup-script-url" failed')])
        assert result['status'] == 'success'
        assert result['fixed_count'] == 2
        (set_call,) = calls['set']
        assert [i['key'] for i in set_call['items']] == [
            'gce-rescue-disabled-startup-script', 'gce-rescue-disabled-startup-script-url']
        assert len(calls['reset']) == 1

    def test_script_that_broke_the_next_boot_is_reported(self, ops) -> None:
        """The disabled script had written a bad fstab entry: the reset boot
        stops in emergency mode. Reported as a failed check (exit 1) as soon
        as it shows, not as 'could not confirm' after the full wait."""
        sleeps: List[float] = []
        compute = FakeCompute(_items(startup_script='exit 1'), [EMERGENCY_BOOT])
        result = _repair(compute, sleeps).run(FAILING)
        assert result['boot_verified'] is False
        assert any(e.startswith('fstab:') for e in result['boot_errors_after'])
        assert len(sleeps) == 1

    def test_finding_during_device_wait_does_not_end_the_wait(self, ops) -> None:
        """A bad fstab entry first shows as systemd's 90s device wait: keep
        waiting until the boot reaches emergency mode, so a re-run of repair
        finds a stuck VM, not a boot in progress."""
        waiting = (BANNER + "[ *** ] A start job is running for "
                   "dev-disk-by\\x2duuid-0badc0de.device (1min 2s / 1min 30s)\n")
        sleeps: List[float] = []
        compute = FakeCompute(_items(startup_script='exit 1'),
                              [waiting, waiting, EMERGENCY_BOOT])
        result = _repair(compute, sleeps).run(FAILING)
        assert result['boot_verified'] is False
        assert len(sleeps) == 3

    def test_mig_member_gets_template_guidance(self, ops) -> None:
        items = _items(startup_script='exit 5') + [{
            'key': 'created-by',
            'value': 'projects/1/zones/z/instanceGroupManagers/web-mig'}]
        result = _repair(FakeCompute(items, [CLEAN_BOOT])).run(FAILING)
        assert result['status'] == 'success'
        assert 'managed instance group web-mig' in result['next_steps'][0]
        assert any('rolling-action start-update web-mig' in line
                   and '--zone=z --project=p' in line
                   for line in result['next_steps'])

    def test_plan_reports_vm_specifics_without_changing_anything(self, ops) -> None:
        calls, _ = ops
        items = _items(startup_script='exit 1', shutdown_script='flush')
        compute = FakeCompute(items, [CLEAN_BOOT], current_serial=LAYERED_BOOT,
                              disks=[{'type': 'PERSISTENT'}, {'type': 'SCRATCH'}])
        plan, refusal = _repair(compute).plan(FAILING)
        assert refusal is None
        assert plan.has_local_ssd and plan.has_shutdown_script
        assert plan.script_output['startup-script'] == ['app: configuring']
        assert plan.serial_offset == 4096 and plan.fingerprint == 'fp-1'
        # startup-script-url failed too but is not set on the instance.
        assert plan.disable.not_on_instance == ['startup-script-url']
        assert calls['set'] == [] and calls['reset'] == []


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

class TestResetVMOperation:

    def setup_method(self) -> None:
        self.compute = Mock()
        self.op = ResetVMOperation(self.compute, 'p', 'z', Mock())

    def test_success(self) -> None:
        with patch.object(self.op, '_wait_for_operation', return_value=True):
            result = self.op.execute('vm-1')
        assert result.success
        self.compute.instances().reset.assert_called_with(
            project='p', zone='z', instance='vm-1')

    def test_timeout(self) -> None:
        with patch.object(self.op, '_wait_for_operation', return_value=False):
            result = self.op.execute('vm-1', timeout=7)
        assert not result.success
        assert 'Timed out after 7s' in result.error

    def test_api_error(self) -> None:
        self.compute.instances().reset().execute.side_effect = RuntimeError('boom')
        result = self.op.execute('vm-1')
        assert not result.success
        assert 'Failed to reset instance [vm-1]' in result.error

    def test_rollback_is_a_no_op(self) -> None:
        assert self.op.rollback({}) is True
        self.compute.instances().start.assert_not_called()


class TestSetMetadataExpectedFingerprint:

    def _op(self) -> SetMetadataOperation:
        compute = Mock()
        compute.instances().get().execute.return_value = {
            'metadata': {'fingerprint': 'current', 'items': []}}
        return SetMetadataOperation(compute, 'p', 'z', Mock())

    @pytest.mark.parametrize('expected,sent', [('planned', 'planned'), (None, 'current')])
    def test_fingerprint_sent(self, expected: Optional[str], sent: str) -> None:
        op = self._op()
        with patch.object(op, '_wait_for_operation', return_value=True):
            op.execute('vm-1', [], preserve_existing=False,
                       expected_fingerprint=expected)
        body = op.compute.instances().setMetadata.call_args.kwargs['body']
        assert body['fingerprint'] == sent
