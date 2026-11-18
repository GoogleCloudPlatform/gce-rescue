"""Tests for ownership checks on disks and snapshots named in a checkpoint.

Resume and rollback act on names from checkpoint metadata, which people
other than the operator can write. A disk or snapshot is only deleted or
attached when it is shown to belong to the rescue of this VM.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gce_rescue_v2.cli import checkpoint_ui
from gce_rescue_v2.operations import AttachDiskOperation, DeleteDiskOperation
from gce_rescue_v2.orchestration.checkpoint import (
    CheckpointData,
    CompletedOperation,
    CheckpointManager,
)
from gce_rescue_v2.orchestration.ownership import (
    OwnershipChecker,
    rescue_disk_description,
    snapshot_description,
)
from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.orchestration.restore import RestoreOrchestrator
from gce_rescue_v2.orchestration.rollback import RollbackHandler
from gce_rescue_v2.orchestration.state import StateTracker
from gce_rescue_v2.tests import test_rescue, test_restore
from gce_rescue_v2.tests.test_rescue import stub_rescue  # noqa: F401 (fixture)
from gce_rescue_v2.tests.test_restore import stub_restore  # noqa: F401 (fixture)

PROJECT = 'p'
ZONE = 'z'
VM = 'vm'
INSTANCE_ID = '1234567890'
OTHER_ID = '999'


def _disk_url(name):
    return f'https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/{ZONE}/disks/{name}'


def _compute(attached=(), disks=None, snapshots=None, instance_id=INSTANCE_ID):
    """A compute mock: an instance, plus disks and snapshots by name."""
    disks = disks or {}
    snapshots = snapshots or {}
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = {
        'id': instance_id,
        'disks': [{'source': _disk_url(name), 'deviceName': name} for name in attached],
    }

    def _getter(store):
        def get(**kwargs):
            name = kwargs.get('disk') or kwargs.get('snapshot')
            call = Mock()
            if name in store:
                call.execute.return_value = store[name]
            else:
                call.execute.side_effect = Exception('404 not found')
            return call
        return get

    compute.disks.return_value.get.side_effect = _getter(disks)
    compute.snapshots.return_value.get.side_effect = _getter(snapshots)
    compute.snapshots.return_value.list.return_value.execute.return_value = {
        'items': [{'name': n} for n in snapshots]}
    return compute


def _checker(**kwargs):
    return OwnershipChecker(_compute(**kwargs), PROJECT, ZONE, VM)


def _own_snapshot(source_disk, instance_id=INSTANCE_ID):
    return {'description': snapshot_description(VM, instance_id),
            'sourceDisk': _disk_url(source_disk)}


class TestOwnershipChecker:
    def test_attached_rescue_disk_is_owned(self):
        assert _checker(attached=['rescue-disk-1']).is_rescue_disk('rescue-disk-1')

    def test_marked_rescue_disk_is_owned(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}}
        assert _checker(disks=disks).is_rescue_disk('rescue-disk-1')

    def test_rescue_disk_marked_for_another_instance_is_not_owned(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(OTHER_ID, 'orig')}}
        assert not _checker(disks=disks).is_rescue_disk('rescue-disk-1')

    def test_unmarked_detached_disk_is_not_owned(self):
        disks = {'rescue-disk-1': {'description': 'Rescue disk created by gce-rescue.'}}
        assert not _checker(disks=disks).is_rescue_disk('rescue-disk-1')

    def test_disk_not_named_like_a_rescue_disk_is_never_owned(self):
        disks = {'prod-db': {'description': rescue_disk_description(INSTANCE_ID, 'x')}}
        checker = _checker(attached=['prod-db'], disks=disks)
        assert not checker.is_rescue_disk('prod-db')
        assert not checker.is_rescue_disk(None)

    def test_api_errors_never_count_as_proof(self):
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = Exception('boom')
        compute.disks.return_value.get.return_value.execute.side_effect = Exception('boom')
        checker = OwnershipChecker(compute, PROJECT, ZONE, VM)
        assert not checker.is_rescue_disk('rescue-disk-1')
        assert not checker.is_own_snapshot('pre-rescue-orig-1')
        assert not checker.is_original_disk('orig', search_snapshots=True)

    def test_snapshot_ownership(self):
        snapshots = {
            'pre-rescue-orig-1': _own_snapshot('orig'),
            'pre-rescue-orig-2': _own_snapshot('orig', OTHER_ID),
            'prod-backup': {'description': 'nightly backup'},
        }
        checker = _checker(snapshots=snapshots)
        assert checker.is_own_snapshot('pre-rescue-orig-1')
        assert not checker.is_own_snapshot('pre-rescue-orig-2')
        assert not checker.is_own_snapshot('prod-backup')
        assert not checker.is_own_snapshot('missing')

    def test_original_disk_attached(self):
        assert _checker(attached=['orig']).is_original_disk('orig')

    def test_original_disk_from_own_snapshot(self):
        snapshots = {'pre-rescue-orig-1': _own_snapshot('orig')}
        checker = _checker(snapshots=snapshots)
        assert checker.is_original_disk('orig', snapshot='pre-rescue-orig-1')
        assert checker.is_original_disk('orig', search_snapshots=True)
        assert not checker.is_original_disk('victim-disk', snapshot='pre-rescue-orig-1')

    def test_original_disk_not_proven_by_foreign_snapshot(self):
        snapshots = {'pre-rescue-victim-disk-1': {
            'description': 'Pre-rescue safety snapshot of vm',
            'sourceDisk': _disk_url('victim-disk')}}
        checker = _checker(snapshots=snapshots)
        assert not checker.is_original_disk('victim-disk', search_snapshots=True)

    def test_original_disk_from_attached_rescue_disk_record(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}}
        checker = _checker(attached=['rescue-disk-1'], disks=disks)
        assert checker.is_original_disk('orig', rescue_disk='rescue-disk-1')
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_rescue_disk_record_counts_after_detach(self):
        # Restore detaches the rescue disk before re-attaching the original;
        # the record on the (detached) rescue disk still proves it.
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}}
        checker = _checker(disks=disks)
        assert checker.is_original_disk('orig', rescue_disk='rescue-disk-1')
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_rescue_disk_record_for_another_instance_is_no_proof(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(OTHER_ID, 'victim-disk')}}
        checker = _checker(disks=disks)
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_attached_is_no_proof_when_not_allowed(self):
        checker = _checker(attached=['data-disk'])
        assert checker.is_original_disk('data-disk')
        assert not checker.is_original_disk('data-disk', allow_attached=False)

    def test_auto_delete_comes_from_the_marker(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig', True)}}
        proof = _checker(disks=disks).original_disk_proof(
            'orig', rescue_disk='rescue-disk-1', allow_attached=False)
        assert proof is not None and proof.auto_delete is True
        snapshots = {'pre-rescue-orig-1': {
            'description': snapshot_description(VM, INSTANCE_ID, False),
            'sourceDisk': _disk_url('orig')}}
        proof = _checker(snapshots=snapshots).original_disk_proof(
            'orig', search_snapshots=True, allow_attached=False)
        assert proof is not None and proof.auto_delete is False
        proof = _checker(attached=['orig']).original_disk_proof('orig')
        assert proof is not None and proof.auto_delete is None

    def test_regional_disk_does_not_pass_for_zonal(self):
        snapshots = {'pre-rescue-orig-1': {
            'description': snapshot_description(VM, INSTANCE_ID),
            'sourceDisk': ('https://www.googleapis.com/compute/v1/projects/'
                           f'{PROJECT}/regions/r/disks/orig')}}
        checker = _checker(snapshots=snapshots)
        assert not checker.is_original_disk('orig', search_snapshots=True)

    def test_trailing_newline_is_not_a_rescue_disk_name(self):
        checker = _checker(attached=['rescue-disk-1'])
        assert not checker.is_rescue_disk('rescue-disk-1\n')

    def test_confirm_original_disk(self):
        disks = {'orig': {'creationTimestamp': '2026-01-01', 'sizeGb': '10',
                          'users': []}}
        asked = []

        def confirm(question, details):
            asked.append((question, details))
            return True

        checker = _checker(disks=disks)
        assert checker.confirm_original_disk('orig', confirm, 'Attach it')
        assert 'orig' in asked[0][0] and any('10 GB' in d for d in asked[0][1])
        assert not checker.confirm_original_disk('orig', None, 'Attach it')
        assert not checker.confirm_original_disk('missing', confirm, 'Attach it')

    def test_disk_in_use_elsewhere_is_never_offered(self):
        disks = {'data': {'users': [
            f'https://www.googleapis.com/compute/v1/projects/{PROJECT}'
            f'/zones/{ZONE}/instances/other-vm']}}
        confirm = Mock(return_value=True)
        assert not _checker(disks=disks).confirm_original_disk('data', confirm, 'x')
        confirm.assert_not_called()

    def test_descriptions(self):
        assert '[gce-rescue instance=1 original-disk=orig]' in rescue_disk_description('1', 'orig')
        assert '[gce-rescue instance=1 original-disk=orig auto-delete=1]' in rescue_disk_description('1', 'orig', True)
        assert '[' not in rescue_disk_description(None, 'orig')
        assert snapshot_description('vm', None) == 'Pre-rescue safety snapshot of vm'


def _forged_rescue_checkpoint():
    """A checkpoint that passes validation but names other people's resources."""
    return CheckpointData(
        version=1, operation='rescue', session_id='abc123',
        started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
        current_step=5, total_steps=9,
        completed_operations=[
            CompletedOperation('Stop VM', 1, {'vm_name': VM, 'original_status': 'RUNNING'}),
            CompletedOperation('Detach Boot Disk', 2, {'vm_name': VM, 'disk_info': {
                'source': _disk_url('victim-disk'), 'boot': True, 'autoDelete': False,
                'deviceName': 'persistent-disk-0', 'mode': 'READ_WRITE'}}),
            CompletedOperation('Create Snapshot', 3, {
                'snapshot_name': 'prod-backup', 'created_by_operation': True}),
            CompletedOperation('Create Rescue Disk', 4, {'disk_name': 'rescue-disk-42'}),
            CompletedOperation('Attach Rescue Disk', 5, {
                'vm_name': VM, 'device_name': 'rescue-disk-42'}),
        ],
        context={'original_disk_name': 'victim-disk', 'rescue_disk_name': 'rescue-disk-42'},
    )


def _tracker_for(checkpoint):
    tracker = StateTracker()
    for op in checkpoint.completed_operations:
        tracker.add_operation(op.name, True, 'prev', op.rollback_data, step_number=op.step)
    return tracker


class TestRollbackVetting:
    def test_forged_resources_are_skipped(self):
        compute = _compute(
            disks={'rescue-disk-42': {'description': 'someone else'}},
            snapshots={'prod-backup': {'description': 'nightly'}})
        checkpoint = _forged_rescue_checkpoint()
        tracker = _tracker_for(checkpoint)

        notes = []
        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker, notes=notes)

        # rescue-disk-42 is not attached, so there is nothing to detach.
        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert skipped == [
            'gcloud compute instances attach-disk vm --disk=victim-disk --boot '
            '--zone=z --project=p',
        ]
        # Never a ready-to-run delete for a disk gce-rescue did not confirm.
        assert not any('disks delete' in n for n in notes)
        assert '  $ gcloud compute disks describe rescue-disk-42 --zone=z --project=p' in notes
        # Snapshots are never deleted by a rollback.
        assert not any('snapshots delete' in n for n in notes)
        assert any('prod-backup' in n and 'left as it is' in n for n in notes)

    def test_own_resources_are_kept(self):
        compute = _compute(
            attached=['rescue-disk-42'],
            disks={'rescue-disk-42': {
                'description': rescue_disk_description(INSTANCE_ID, 'victim-disk')}},
            snapshots={'prod-backup': _own_snapshot('victim-disk')})
        checkpoint = _forged_rescue_checkpoint()
        tracker = _tracker_for(checkpoint)

        notes = []
        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker, notes=notes)

        assert skipped == []
        assert [op.operation_name for op in tracker.operations] == [
            'Stop VM', 'Detach Boot Disk', 'Create Rescue Disk',
            'Attach Rescue Disk']
        assert any('prod-backup' in n for n in notes)
        info = tracker.operations[1].rollback_data['disk_info']
        assert info == {'source': 'projects/p/zones/z/disks/victim-disk',
                        'boot': True, 'autoDelete': False,
                        'deviceName': 'persistent-disk-0', 'mode': 'READ_WRITE'}

    def test_swapping_disks_on_the_vm_is_refused(self):
        """A forged rollback that would boot the VM from its data disk."""
        compute = _compute(attached=['boot1', 'data1'])
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=8, total_steps=9,
            completed_operations=[
                CompletedOperation('Detach Boot Disk', 2, {'vm_name': VM, 'disk_info': {
                    'source': _disk_url('data1'), 'boot': True, 'autoDelete': True,
                    'deviceName': 'persistent-disk-0', 'mode': 'READ_WRITE'}}),
                CompletedOperation('Attach Rescue Disk', 5, {
                    'vm_name': VM, 'device_name': 'boot1'}),
                CompletedOperation('Attach Original Disk', 8, {
                    'vm_name': VM, 'device_name': 'data1'}),
            ],
            context={})
        tracker = _tracker_for(checkpoint)

        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker)

        assert tracker.operations == []
        assert skipped == [
            'gcloud compute instances detach-disk vm --device-name=data1 '
            '--zone=z --project=p',
            'gcloud compute instances detach-disk vm --device-name=boot1 '
            '--zone=z --project=p',
        ]

    def test_unproven_original_disk_can_be_confirmed(self):
        """--no-snapshot or an older version: the operator confirms the disk."""
        compute = _compute(disks={'orig': {'sizeGb': '10', 'users': []}})
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=2, total_steps=9,
            completed_operations=[
                CompletedOperation('Stop VM', 1, {'vm_name': VM}),
                CompletedOperation('Detach Boot Disk', 2, {'vm_name': VM, 'disk_info': {
                    'source': _disk_url('orig'), 'boot': True, 'autoDelete': True,
                    'deviceName': 'persistent-disk-0', 'mode': 'READ_ONLY'}}),
            ],
            context={})
        asked = []
        tracker = _tracker_for(checkpoint)
        notes = []

        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker,
            confirm=lambda q, d: asked.append(q) or True, notes=notes)

        assert skipped == [] and len(asked) == 1
        info = tracker.operations[1].rollback_data['disk_info']
        assert info['autoDelete'] is False and info['mode'] == 'READ_WRITE'
        assert any('set-disk-auto-delete vm --disk=orig' in n for n in notes)

        tracker = _tracker_for(checkpoint)
        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker,
            confirm=lambda q, d: False)
        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert len(skipped) == 1

    def test_regional_or_foreign_disk_is_never_reattached(self):
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=2, total_steps=9,
            completed_operations=[
                CompletedOperation('Detach Boot Disk', 2, {'vm_name': VM, 'disk_info': {
                    'source': f'projects/{PROJECT}/regions/r/disks/orig',
                    'boot': True, 'deviceName': 'd', 'mode': 'READ_WRITE'}}),
            ],
            context={})
        tracker = _tracker_for(checkpoint)
        confirm = Mock(return_value=True)

        skipped = checkpoint_ui._vet_checkpoint_rollback(
            _compute(attached=['orig']), PROJECT, ZONE, VM, checkpoint,
            tracker, confirm=confirm)

        assert tracker.operations == [] and len(skipped) == 1
        confirm.assert_not_called()

    def test_restore_rollback_reattach_is_vetted(self):
        checkpoint = CheckpointData(
            version=1, operation='restore', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=3, total_steps=7,
            completed_operations=[
                CompletedOperation('Stop VM', 1, {'vm_name': VM}),
                CompletedOperation('Detach Rescue Disk', 2, {'vm_name': VM, 'disk_info': {
                    'source': _disk_url('rescue-disk-7'), 'boot': True}}),
                CompletedOperation('Detach Original Disk', 3, {'vm_name': VM, 'disk_info': {
                    'source': _disk_url('victim-disk'), 'boot': False}}),
            ],
            context={})
        tracker = _tracker_for(checkpoint)

        skipped = checkpoint_ui._vet_checkpoint_rollback(
            _compute(), PROJECT, ZONE, VM, checkpoint, tracker)

        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert skipped == [
            'gcloud compute instances attach-disk vm --disk=victim-disk '
            '--zone=z --project=p',
            'gcloud compute instances attach-disk vm --disk=rescue-disk-7 --boot '
            '--zone=z --project=p',
        ]

    def test_rollback_handler_never_sees_forged_steps(self, monkeypatch, capsys):
        compute = _compute(
            disks={'rescue-disk-42': {'description': 'someone else'}},
            snapshots={'prod-backup': {'description': 'nightly'}})
        seen = []
        monkeypatch.setattr(
            RollbackHandler, 'rollback',
            lambda self, tracker, ops: seen.extend(
                op.operation_name for op in tracker.get_rollback_operations()) or True)
        monkeypatch.setattr(CheckpointManager, 'clear_checkpoint', lambda self: True)
        monkeypatch.setattr(checkpoint_ui, '_reconcile_rescue_state', lambda *a, **k: None)

        result = checkpoint_ui._handle_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, _forged_rescue_checkpoint())

        assert result is False
        assert seen == ['Stop VM']
        compute.disks.return_value.delete.assert_not_called()
        compute.snapshots.return_value.delete.assert_not_called()
        compute.instances.return_value.attachDisk.assert_not_called()
        out = capsys.readouterr()
        assert 'Skipped 1 rollback step:' in out.err
        assert 'disks delete' not in out.err
        assert 'snapshots delete prod-backup' not in out.err
        assert 'finished except for the skipped steps' in out.out


def _rescue_checkpoint(step, original='victim-disk', rescue='rescue-disk-1'):
    return CheckpointData(
        version=1, operation='rescue', session_id='abc123',
        started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
        current_step=step, total_steps=9, completed_operations=[],
        context={'original_disk_name': original, 'original_device_name': 'sda',
                 'rescue_disk_name': rescue, 'os_type': 'linux',
                 'architecture': 'x86_64'})


def _record_attach(monkeypatch):
    calls = []

    def _attach(self, *args, **kwargs):
        calls.append(kwargs.get('disk_name'))
        return test_rescue._success_execute(self)

    monkeypatch.setattr(AttachDiskOperation, 'execute', _attach)
    return calls


class TestRescueResume:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr('time.sleep', lambda *a, **k: None)

    def test_resume_stops_when_original_disk_is_not_proven(self, stub_rescue, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(RescueOrchestrator, '_disk_exists', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: False)
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4))

        assert orch.execute() is False
        assert attached == []

    def test_resume_asks_about_an_unproven_original_disk(self, stub_rescue, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(RescueOrchestrator, '_disk_exists', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: False)
        monkeypatch.setattr(OwnershipChecker, 'describe_disk',
                            lambda self, n: ['Disk: ' + n])
        asked = []
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4))
        orch.confirm_disk = lambda q, d: asked.append(d) or True

        assert orch.execute() is True
        assert asked == [['Disk: victim-disk']]
        assert attached == ['rescue-disk-1', 'victim-disk']

    def test_resume_stops_when_rescue_disk_is_not_ours(self, stub_rescue, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(RescueOrchestrator, '_disk_exists', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: False)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: True)
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4))

        assert orch.execute() is False
        assert attached == []

    def test_resume_rejects_rescue_disk_with_another_name(self, stub_rescue, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: True)
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4, rescue='prod-db'))

        assert orch.execute() is False
        assert attached == []

    def test_resume_with_proven_disks_continues(self, stub_rescue, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(RescueOrchestrator, '_disk_exists', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: True)
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4))

        assert orch.execute() is True
        assert attached == ['rescue-disk-1', 'victim-disk']

    def test_resume_before_detach_reads_boot_disk_from_vm(self, stub_rescue, monkeypatch):
        """Before the boot disk is detached, the VM is the source, not the checkpoint."""
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk',
                            lambda self, *a, **k: pytest.fail('not needed'))
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=1))

        assert orch.execute() is True
        assert orch.original_disk_name == 'original-boot'
        assert 'victim-disk' not in attached

    def test_new_rescue_disk_and_snapshot_carry_marker(self, stub_rescue, monkeypatch):
        seen = SimpleNamespace(disk=None, snapshot=None)

        def _create_disk(self, *args, **kwargs):
            seen.disk = kwargs.get('description')
            return test_rescue._success_execute(self)

        def _create_snapshot(self, *args, **kwargs):
            seen.snapshot = kwargs.get('description')
            return test_rescue._success_execute(self)

        from gce_rescue_v2.operations import CreateDiskOperation, CreateSnapshotOperation
        from gce_rescue_v2.core.config import RescueConfig
        monkeypatch.setattr(CreateDiskOperation, 'execute', _create_disk)
        monkeypatch.setattr(CreateSnapshotOperation, 'execute', _create_snapshot)
        monkeypatch.setattr(RescueOrchestrator, '_instance_id', lambda self: INSTANCE_ID)
        orch = test_rescue._make_orch(RescueConfig(create_snapshot=True))

        assert orch.execute() is True
        assert seen.disk == rescue_disk_description(INSTANCE_ID, 'original-boot')
        assert seen.snapshot == snapshot_description('vm', INSTANCE_ID)


def _restore_checkpoint(step, original='victim-disk', rescue='rescue-disk-123'):
    return CheckpointData(
        version=1, operation='restore', session_id='abc123',
        started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
        current_step=step, total_steps=7, completed_operations=[],
        context={'rescue_disk_name': rescue, 'rescue_device_name': 'sdb',
                 'original_disk_name': original, 'original_device_name': 'sda'})


class TestRestoreResume:
    def test_resume_stops_before_attaching_unproven_disk(self, stub_restore, monkeypatch):
        attached = _record_attach(monkeypatch)
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: False)
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3))

        assert orch.execute() is False
        assert attached == []

    def test_resume_keeps_unproven_rescue_disk(self, stub_restore, monkeypatch, capsys):
        deleted = []
        monkeypatch.setattr(DeleteDiskOperation, 'execute',
                            lambda self, **kw: deleted.append(kw['disk_name'])
                            or test_restore._success_execute(self))
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: False)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: True)
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3, rescue='rescue-disk-999'))

        assert orch.execute() is True
        assert deleted == []

    def test_resume_deletes_proven_rescue_disk(self, stub_restore, monkeypatch):
        deleted = []
        monkeypatch.setattr(DeleteDiskOperation, 'execute',
                            lambda self, **kw: deleted.append(kw['disk_name'])
                            or test_restore._success_execute(self))
        monkeypatch.setattr(OwnershipChecker, 'is_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: True)
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3))

        assert orch.execute() is True
        assert deleted == ['rescue-disk-123']

    def test_resume_uses_device_names_from_the_vm(self, stub_restore):
        vm = {'disks': [
            {'source': _disk_url('rescue-disk-123'), 'deviceName': 'real-rescue-dev'},
            {'source': _disk_url('orig'), 'deviceName': 'real-orig-dev'}]}
        orch = RestoreOrchestrator(
            compute=test_restore.FakeCompute([vm, vm]), project=PROJECT, zone=ZONE,
            vm_name=VM, logger=None, suppress_progress=True)
        orch.set_resume_state(_restore_checkpoint(step=1, original='orig'))

        assert orch._verify_resumed_disks() is True
        assert orch.rescue_device_name == 'real-rescue-dev'
        assert orch.original_device_name == 'real-orig-dev'

    def test_rescue_disk_found_by_exact_name_only(self):
        vm = {'metadata': {'items': [{'key': 'rescue-original-disk', 'value': 'orig'}]},
              'disks': [
                  {'source': _disk_url('my-rescue-disk-backup'), 'deviceName': 'data'},
                  {'source': _disk_url('rescue-disk-55'), 'deviceName': 'rescue', 'boot': True},
                  {'source': _disk_url('orig'), 'deviceName': 'orig'}]}
        orch = RestoreOrchestrator(
            compute=test_restore.FakeCompute([vm]), project=PROJECT, zone=ZONE,
            vm_name=VM, logger=None, suppress_progress=True)
        orch._get_disk_info()

        assert orch.rescue_disk_name == 'rescue-disk-55'
        assert orch._rescue_disk_owned is True


class TestWindowsKeysRecord:
    """reset-windows-password adds a rescue_admin key; restore must undo it."""

    @staticmethod
    def _rescue_then_restore(before, during, record=('windows-keys',)):
        from gce_rescue_v2.operations.set_metadata import (
            SetMetadataOperation, restore_pre_rescue_items)
        op = SetMetadataOperation(Mock(), PROJECT, ZONE, None)
        rescue_items = [{'key': 'windows-startup-script-ps1', 'value': 'script'},
                        {'key': 'rescue-mode', 'value': '1'}]
        merged = op._merge_with_backup(before, rescue_items, record)
        # The user runs reset-windows-password during the rescue session.
        merged = [i for i in merged if i['key'] != 'windows-keys']
        merged.append({'key': 'windows-keys', 'value': during})
        restored = restore_pre_rescue_items(merged)
        return {i['key']: i['value'] for i in restored}

    def test_key_added_during_rescue_is_removed(self):
        restored = self._rescue_then_restore(
            before=[{'key': 'ssh-keys', 'value': 'k'}], during='{"userName":"rescue_admin"}')
        assert 'windows-keys' not in restored
        assert restored == {'ssh-keys': 'k'}

    def test_existing_keys_are_put_back(self):
        restored = self._rescue_then_restore(
            before=[{'key': 'windows-keys', 'value': '{"userName":"alice"}'}],
            during='{"userName":"alice"}\n{"userName":"rescue_admin"}')
        assert restored['windows-keys'] == '{"userName":"alice"}'

    def test_linux_rescue_records_nothing(self):
        from gce_rescue_v2.operations.set_metadata import SetMetadataOperation
        op = SetMetadataOperation(Mock(), PROJECT, ZONE, None)
        merged = op._merge_with_backup(
            [{'key': 'windows-keys', 'value': 'x'}],
            [{'key': 'startup-script', 'value': 's'}])
        keys = {i['key'] for i in merged}
        assert 'rescue-backup-windows-keys' not in keys
        assert 'rescue-backup-absent-keys' not in keys
