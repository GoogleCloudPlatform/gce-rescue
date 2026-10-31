"""Tests for User-Agent usage tracking: repair fix/ field and rollback steps.

The User-Agent is how sessions are counted on the usage dashboard, so these
tests pin the exact field layout: fix/<categories> on repair calls once the
fix set is chosen, and step/rollback-<step> on rollback API calls.
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import Mock, patch

import pytest

from gce_rescue_v2.core.config import (
    MAX_UA_FIX_LENGTH,
    RescueConfig,
    build_user_agent,
)
from gce_rescue_v2.operations import (
    AttachDiskOperation,
    CreateDiskOperation,
    CreateSnapshotOperation,
    DetachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    StopVMOperation,
)
from gce_rescue_v2.orchestration.repair import RepairOrchestrator
from gce_rescue_v2.orchestration.rescue import (
    RESCUE_ROLLBACK_STEPS,
    RescueOrchestrator,
)
from gce_rescue_v2.orchestration.restore import (
    RESTORE_ROLLBACK_STEPS,
    RestoreOrchestrator,
)
from gce_rescue_v2.orchestration.rollback import RollbackHandler
from gce_rescue_v2.orchestration.state import StateTracker


ORCHESTRATION_DIR = Path(__file__).parent.parent / 'orchestration'


# ---------------------------------------------------------------------------
# build_user_agent fix/ field
# ---------------------------------------------------------------------------

class TestBuildUserAgentFix:
    """The optional fix/ field sits after mode/ and before step/."""

    def test_absent_by_default(self) -> None:
        ua = build_user_agent(session_id='abc', command='repair',
                              mode='auto', step='boot-verify')
        assert 'fix/' not in ua

    def test_none_and_empty_omitted(self) -> None:
        assert 'fix/' not in build_user_agent(command='repair', fix=None)
        assert 'fix/' not in build_user_agent(command='repair', fix='')

    def test_present_between_mode_and_step(self) -> None:
        ua = build_user_agent(session_id='abc', command='repair',
                              mode='auto', step='boot-verify',
                              fix='fstab+grub')
        assert ua.endswith(' mode/auto fix/fstab+grub step/boot-verify')

    def test_present_without_mode(self) -> None:
        ua = build_user_agent(command='repair', step='serial-parse',
                              fix='custom')
        assert ua.endswith(' command/repair fix/custom step/serial-parse')

    def test_sanitised(self) -> None:
        ua = build_user_agent(command='repair', fix='fstab grub/x,y')
        assert ua.endswith(' fix/fstab-grub-xy')
        assert len(ua.split(' ')) == 3  # no extra tokens injected

    def test_capped(self) -> None:
        long_fix = '+'.join(['category'] * 20)
        ua = build_user_agent(command='repair', fix=long_fix)
        value = ua.split('fix/', 1)[1]
        assert len(value) == MAX_UA_FIX_LENGTH == 64
        assert long_fix.startswith(value)

    def test_value_at_cap_unchanged(self) -> None:
        exact = 'a' * MAX_UA_FIX_LENGTH
        assert build_user_agent(fix=exact).endswith(f'fix/{exact}')


# ---------------------------------------------------------------------------
# Repair orchestrator sets the fix label
# ---------------------------------------------------------------------------

def _repair(fix_script: Optional[str] = None) -> Tuple[RepairOrchestrator, List[str]]:
    """Repair orchestrator whose tracked-client labels are recorded."""
    vm_info = {
        'status': 'TERMINATED',
        'disks': [{
            'boot': True,
            'source': 'projects/p/zones/z/disks/test-boot-disk',
            'deviceName': 'test-boot-disk',
            'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
        }],
        'metadata': {'items': [], 'fingerprint': 'abc'},
    }
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = vm_info
    config = RescueConfig()
    config.fix_script = fix_script
    orch = RepairOrchestrator(compute, 'proj', 'zone-a', 'vm-1', config=config,
                              session_id='0123456789ab', mode='auto')
    labels: List[str] = []

    def _tracked(label: str) -> Mock:
        labels.append(label)
        return compute

    orch._create_tracked_client = _tracked
    return orch, labels


def _diagnosis(*categories: str) -> Dict[str, Any]:
    return {'boot_errors': [{'category': c, 'severity': 'critical'}
                            for c in categories]}


class TestRepairFixLabel:
    """fix/ appears on repair calls from the fix step onward only."""

    def test_absent_before_fix_chosen(self) -> None:
        orch, _ = _repair()
        assert 'fix/' not in orch._ua('val-iam')
        assert 'fix/' not in orch._ua('diagnose')

    def test_categories_in_execution_order(self) -> None:
        """Diagnosis order grub, fstab -> fix/fstab+grub."""
        orch, labels = _repair()
        captured: Dict[str, str] = {}

        def _flow(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            captured['ua'] = orch._ua('serial-parse')
            return {'status': 'success'}

        with patch.object(orch, '_run_repair_flow', side_effect=_flow):
            orch.execute(_diagnosis('grub', 'fstab'))

        assert ' fix/fstab+grub step/serial-parse' in captured['ua']
        # The script-generation instance lookup already carries it
        script_labels = [label for label in labels
                         if 'step/script-vm-info' in label]
        assert script_labels
        assert all('fix/fstab+grub' in label for label in script_labels)

    def test_single_category(self) -> None:
        orch, _ = _repair()
        with patch.object(orch, '_run_repair_flow',
                          return_value={'status': 'success'}):
            orch.execute(_diagnosis('fstab'))
        assert ' fix/fstab ' in orch._ua('boot-verify')

    def test_no_fixable_categories_no_label(self) -> None:
        orch, _ = _repair()
        result = orch.execute(_diagnosis('kernel_panic'))
        assert result['status'] == 'no_fix'
        assert 'fix/' not in orch._ua('boot-verify')

    def test_custom_fix_script(self) -> None:
        orch, _ = _repair(fix_script='echo custom')
        captured: Dict[str, str] = {}

        def _flow(*args: Any, **kwargs: Any) -> Dict[str, Any]:
            captured['ua'] = orch._ua('boot-verify')
            return {'status': 'success'}

        with patch.object(orch, '_run_repair_flow', side_effect=_flow):
            orch.execute_custom()
        assert ' fix/custom step/boot-verify' in captured['ua']

    def test_never_on_rescue_or_restore_user_agents(self) -> None:
        rescue = RescueOrchestrator(compute=Mock(), project='p', zone='z',
                                    vm_name='vm-1', session_id='s', mode='auto')
        restore = RestoreOrchestrator(compute=Mock(), project='p', zone='z',
                                      vm_name='vm-1', session_id='s',
                                      mode='auto')
        assert 'fix/' not in rescue._ua('vm-stop')
        assert 'fix/' not in restore._ua('vm-stop')
        assert 'fix/' not in rescue._rollback_label('Stop VM')


# ---------------------------------------------------------------------------
# RollbackHandler label threading
# ---------------------------------------------------------------------------

class _RecordingOperation:
    """Operation stub that records the tracking label it was given."""

    def __init__(self) -> None:
        self.calls: List[Tuple[Dict[str, Any], Optional[str]]] = []

    def rollback(self, rollback_data: Dict[str, Any],
                 tracking_label: Optional[str] = None) -> bool:
        self.calls.append((rollback_data, tracking_label))
        return True


class _LegacyOperation:
    """Operation stub with the pre-label rollback signature."""

    def __init__(self) -> None:
        self.called = False

    def rollback(self, rollback_data: Dict[str, Any]) -> bool:
        self.called = True
        return True


def _tracker(*names: str) -> StateTracker:
    tracker = StateTracker()
    for index, name in enumerate(names, 1):
        tracker.add_operation(name, success=True, message='ok',
                              rollback_data={'n': index}, step_number=index)
    return tracker


class TestRollbackHandlerLabels:

    def test_label_passed_to_each_operation(self) -> None:
        ops = {'Stop VM': _RecordingOperation(),
               'Attach Rescue Disk': _RecordingOperation()}
        asked: List[str] = []

        def _label(name: str) -> str:
            asked.append(name)
            return f'ua step/rollback-{name}'

        handler = RollbackHandler(Mock())
        assert handler.rollback(_tracker('Stop VM', 'Attach Rescue Disk'),
                                ops, label_for=_label) is True
        assert asked == ['Attach Rescue Disk', 'Stop VM']  # LIFO
        assert ops['Stop VM'].calls == [({'n': 1}, 'ua step/rollback-Stop VM')]
        assert ops['Attach Rescue Disk'].calls == [
            ({'n': 2}, 'ua step/rollback-Attach Rescue Disk')]

    def test_no_label_builder_keeps_legacy_signature(self) -> None:
        op = _LegacyOperation()
        assert RollbackHandler(Mock()).rollback(_tracker('Op'), {'Op': op})
        assert op.called

    def test_builder_returning_none_calls_without_label(self) -> None:
        op = _LegacyOperation()
        handler = RollbackHandler(Mock())
        assert handler.rollback(_tracker('Op'), {'Op': op},
                                label_for=lambda name: None)
        assert op.called


# ---------------------------------------------------------------------------
# Orchestrator rollback labels
# ---------------------------------------------------------------------------

def _registered_operation_names(source_file: str) -> List[str]:
    source = (ORCHESTRATION_DIR / source_file).read_text(encoding='utf-8')
    return re.findall(r'state_tracker\.add_operation\("([^"]+)"', source)


class TestOrchestratorRollbackLabels:

    def _rescue(self) -> RescueOrchestrator:
        orch = RescueOrchestrator(compute=Mock(), project='p', zone='z',
                                  vm_name='vm-1', session_id='0123456789ab',
                                  mode='interactive')
        orch.os_type, orch.architecture, orch.os_flavor = (
            'linux', 'x86_64', 'debian-12')
        return orch

    def test_rescue_label_uses_original_step(self) -> None:
        label = self._rescue()._rollback_label('Attach Rescue Disk')
        assert label.endswith(' step/rollback-disk-attach-rescue')
        assert ' command/rescue ' in label
        assert ' session/0123456789ab ' in label
        assert ' flavor/debian-12 ' in label

    def test_restore_label_uses_original_step(self) -> None:
        orch = RestoreOrchestrator(compute=Mock(), project='p', zone='z',
                                   vm_name='vm-1', session_id='0123456789ab',
                                   mode='auto')
        label = orch._rollback_label('Set Metadata')
        assert label.endswith(' step/rollback-meta-restore-orig')
        assert ' command/restore ' in label

    def test_unknown_operation_gets_slug(self) -> None:
        label = self._rescue()._rollback_label('Resize Original Disk')
        assert label.endswith(' step/rollback-resize-original-disk')

    @pytest.mark.parametrize('source_file,steps', [
        ('rescue.py', RESCUE_ROLLBACK_STEPS),
        ('restore.py', RESTORE_ROLLBACK_STEPS),
    ])
    def test_every_tracked_operation_has_a_step(
            self, source_file: str, steps: Dict[str, str]) -> None:
        names = _registered_operation_names(source_file)
        assert names
        assert sorted(set(names)) == sorted(steps)

    @pytest.mark.parametrize('source_file,steps', [
        ('rescue.py', RESCUE_ROLLBACK_STEPS),
        ('restore.py', RESTORE_ROLLBACK_STEPS),
    ])
    def test_steps_match_forward_tracking_labels(
            self, source_file: str, steps: Dict[str, str]) -> None:
        source = (ORCHESTRATION_DIR / source_file).read_text(encoding='utf-8')
        forward = set(re.findall(r"self\._ua\('([a-z0-9-]+)'\)", source))
        assert set(steps.values()) <= forward

    @pytest.mark.parametrize('make', ['rescue', 'restore'])
    @pytest.mark.parametrize('method', ['rollback', '_rollback'])
    def test_orchestrators_pass_label_builder(self, make: str,
                                              method: str) -> None:
        cls = RescueOrchestrator if make == 'rescue' else RestoreOrchestrator
        orch = cls(compute=Mock(), project='p', zone='z', vm_name='vm-1',
                   session_id='s', mode='auto')
        orch.checkpoint_manager = Mock()
        orch.rollback_handler = Mock()
        orch.rollback_handler.rollback.return_value = True
        getattr(orch, method)()
        kwargs = orch.rollback_handler.rollback.call_args.kwargs
        assert kwargs['label_for'] == orch._rollback_label


# ---------------------------------------------------------------------------
# Operation rollbacks use the tracked client
# ---------------------------------------------------------------------------

# (operation class, rollback_data, resource accessor, mutating method)
OPERATION_CASES = [
    (AttachDiskOperation, {'vm_name': 'vm', 'device_name': 'rescue'},
     'instances', 'detachDisk'),
    (DetachDiskOperation,
     {'vm_name': 'vm', 'disk_info': {'source': 's', 'boot': True,
                                     'autoDelete': False,
                                     'deviceName': 'd', 'mode': 'READ_WRITE'}},
     'instances', 'attachDisk'),
    (CreateDiskOperation, {'disk_name': 'rescue-disk-1'}, 'disks', 'delete'),
    (CreateSnapshotOperation,
     {'snapshot_name': 'pre-rescue-x', 'created_by_operation': True},
     'snapshots', 'delete'),
    (SetMetadataOperation,
     {'vm_name': 'vm', 'original_metadata': {'items': []}},
     'instances', 'setMetadata'),
    (StartVMOperation, {'vm_name': 'vm', 'original_status': 'TERMINATED'},
     'instances', 'stop'),
    (StopVMOperation, {'vm_name': 'vm', 'original_status': 'RUNNING'},
     'instances', 'start'),
]
CASE_IDS = [case[0].__name__ for case in OPERATION_CASES]


@pytest.fixture(autouse=True)
def _no_waits():
    """Rollbacks poll and sleep; make that instant."""
    with patch('time.sleep'), \
            patch('gce_rescue_v2.operations.base.BaseOperation._wait_for_operation',
                  return_value=True), \
            patch('gce_rescue_v2.operations.base.BaseOperation._wait_for_status',
                  return_value=True):
        yield


def _mutation(compute: Mock, resource: str, method: str) -> Mock:
    return getattr(getattr(compute, resource).return_value, method)


class TestOperationRollbackClient:

    @pytest.mark.parametrize('cls,data,resource,method', OPERATION_CASES,
                             ids=CASE_IDS)
    def test_label_uses_tracked_client(self, cls: type, data: Dict[str, Any],
                                       resource: str, method: str) -> None:
        compute, tracked = Mock(), Mock()
        op = cls(compute, 'p', 'z')
        with patch.object(op, '_create_tracked_client',
                          return_value=tracked) as factory:
            assert op.rollback(data, tracking_label='ua step/rollback-x') is True
        factory.assert_called_once_with('ua step/rollback-x')
        _mutation(tracked, resource, method).assert_called_once()
        _mutation(compute, resource, method).assert_not_called()

    @pytest.mark.parametrize('cls,data,resource,method', OPERATION_CASES,
                             ids=CASE_IDS)
    def test_no_label_uses_bare_client(self, cls: type, data: Dict[str, Any],
                                       resource: str, method: str) -> None:
        compute = Mock()
        op = cls(compute, 'p', 'z')
        with patch.object(op, '_create_tracked_client') as factory:
            assert op.rollback(data) is True
        factory.assert_not_called()
        _mutation(compute, resource, method).assert_called_once()

    @pytest.mark.parametrize('cls,data,resource,method', OPERATION_CASES,
                             ids=CASE_IDS)
    def test_tracked_client_failure_falls_back(self, cls: type,
                                               data: Dict[str, Any],
                                               resource: str,
                                               method: str) -> None:
        """Telemetry must never stop a rollback."""
        compute = Mock()
        op = cls(compute, 'p', 'z')
        with patch.object(op, '_create_tracked_client',
                          side_effect=AttributeError('no credentials')):
            assert op.rollback(data, tracking_label='ua') is True
        _mutation(compute, resource, method).assert_called_once()
