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
    OriginalDiskProof,
    OwnershipChecker,
    RESCUE_DISK_NAME,
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
    """A compute mock: an instance, plus disks and snapshots by name.

    The first disk in `attached` is the boot disk.
    """
    disks = dict(disks or {})
    snapshots = snapshots or {}
    for snap in snapshots.values():
        source = snap.get('sourceDisk', '')
        if snap.get('sourceDiskId') and '/zones/' in source:
            disks.setdefault(source.split('/')[-1], {'id': snap['sourceDiskId']})
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = {
        'id': instance_id,
        'disks': [{'source': _disk_url(name), 'deviceName': name, 'boot': i == 0}
                  for i, name in enumerate(attached)],
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
            'sourceDisk': _disk_url(source_disk),
            'sourceDiskId': f'id-{source_disk}'}


class TestOwnershipChecker:
    def test_attached_rescue_disk_is_owned(self):
        assert _checker(attached=['rescue-disk-1']).is_rescue_disk('rescue-disk-1')

    def test_secondary_rescue_disk_needs_the_marker(self):
        # A rescue disk the user kept and attached again as a data disk.
        checker = _checker(attached=['orig', 'rescue-disk-1'])
        assert not checker.is_rescue_disk('rescue-disk-1')
        assert not checker.is_disposable_rescue_disk('rescue-disk-1')
        marked = _checker(attached=['orig', 'rescue-disk-1'], disks={'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig'),
            'lastAttachTimestamp': '2026-11-01T00:00:00Z',
            'users': [f'projects/{PROJECT}/zones/{ZONE}/instances/{VM}']}})
        assert marked.is_rescue_disk('rescue-disk-1')
        # Never deleted: it was attached before and is not the boot disk.
        assert not marked.is_disposable_rescue_disk('rescue-disk-1')

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
        assert not checker.is_original_disk('orig')
        assert checker.snapshots_taken_from('orig') == []

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
        checker = _checker(attached=['orig'])
        assert checker.is_original_disk('orig', allow_attached=True)
        assert not checker.is_original_disk('orig')

    def test_snapshot_of_an_older_disk_with_the_same_name_is_not_listed(self):
        snapshots = {'pre-rescue-orig-1': _own_snapshot('orig')}
        checker = _checker(snapshots=snapshots, disks={'orig': {'id': 'id-new'}})
        assert checker.snapshots_taken_from('orig') == []

    def test_own_snapshot_is_context_not_proof(self):
        # Anyone who can snapshot a disk can write the marker.
        snapshots = {'pre-rescue-orig-1': _own_snapshot('orig')}
        checker = _checker(snapshots=snapshots)
        assert not checker.is_original_disk('orig')
        assert checker.snapshots_taken_from('orig') == ['pre-rescue-orig-1']
        assert checker.snapshots_taken_from('orig', 'pre-rescue-orig-1') == [
            'pre-rescue-orig-1']
        assert checker.snapshots_taken_from('victim-disk', 'pre-rescue-orig-1') == []

    def test_foreign_snapshot_is_not_listed(self):
        snapshots = {'pre-rescue-victim-disk-1': {
            'description': 'Pre-rescue safety snapshot of vm',
            'sourceDisk': _disk_url('victim-disk')}}
        checker = _checker(snapshots=snapshots)
        assert checker.snapshots_taken_from('victim-disk') == []

    def test_confirmation_shows_snapshots_of_the_disk(self):
        snapshots = {'pre-rescue-orig-1': _own_snapshot('orig')}
        checker = _checker(snapshots=snapshots)
        confirm = Mock(return_value=True)
        assert checker.confirm_original_disk('orig', confirm, 'Attach it')
        details = confirm.call_args[0][1]
        assert details[-1] == 'Snapshots:     pre-rescue-orig-1'

    def test_original_disk_from_attached_rescue_disk_record(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}}
        checker = _checker(attached=['rescue-disk-1'], disks=disks)
        assert checker.is_original_disk('orig', rescue_disk='rescue-disk-1')
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_rescue_disk_record_only_counts_while_attached(self):
        # Anyone who can create disks can write the marker; only a rescue
        # disk attached to this VM vouches for the original.
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'victim-disk', True)}}
        checker = _checker(disks=disks)
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_a_record_naming_another_original_is_refused(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}}
        snapshots = {'pre-rescue-data-1': _own_snapshot('data')}
        checker = _checker(disks=disks, snapshots=snapshots)
        assert not checker.is_original_disk('data', rescue_disk='rescue-disk-1')
        confirm = Mock(return_value=True)
        assert not checker.confirm_original_disk('data', confirm, 'x',
                                                 rescue_disk='rescue-disk-1')
        confirm.assert_not_called()

    def test_disposable_rescue_disk(self):
        marked = rescue_disk_description(INSTANCE_ID, 'orig')
        disks = {
            'rescue-disk-1': {'description': marked},
            'rescue-disk-2': {'description': marked,
                              'lastAttachTimestamp': '2026-11-01T00:00:00Z'},
        }
        checker = _checker(attached=['rescue-disk-3', 'rescue-disk-4'], disks=disks)
        assert checker.is_disposable_rescue_disk('rescue-disk-1')
        assert not checker.is_disposable_rescue_disk('rescue-disk-2')
        # The boot disk now; a secondary one may be a disk the user kept.
        assert checker.is_disposable_rescue_disk('rescue-disk-3')
        assert not checker.is_disposable_rescue_disk('rescue-disk-4')

    def test_rescue_disk_record_for_another_instance_is_no_proof(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(OTHER_ID, 'victim-disk')}}
        checker = _checker(disks=disks)
        assert not checker.is_original_disk('victim-disk', rescue_disk='rescue-disk-1')

    def test_attached_is_no_proof_when_not_allowed(self):
        checker = _checker(attached=['data-disk'])
        assert checker.is_original_disk('data-disk', allow_attached=True)
        assert not checker.is_original_disk('data-disk')

    def test_auto_delete_comes_from_the_marker(self):
        disks = {'rescue-disk-1': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig', True)}}
        proof = _checker(attached=['rescue-disk-1'], disks=disks).original_disk_proof(
            'orig', rescue_disk='rescue-disk-1')
        assert proof is not None and proof.auto_delete is True
        # A snapshot marker never supplies proof, or the auto-delete setting.
        snapshots = {'pre-rescue-orig-1': {
            'description': snapshot_description(VM, INSTANCE_ID, True),
            'sourceDisk': _disk_url('orig'), 'sourceDiskId': 'id-orig'}}
        assert _checker(snapshots=snapshots).original_disk_proof('orig') is None
        proof = _checker(attached=['orig']).original_disk_proof(
            'orig', allow_attached=True)
        assert proof is not None and proof.auto_delete is None

    def test_regional_disk_does_not_pass_for_zonal(self):
        snapshots = {'pre-rescue-orig-1': {
            'description': snapshot_description(VM, INSTANCE_ID),
            'sourceDisk': ('https://www.googleapis.com/compute/v1/projects/'
                           f'{PROJECT}/regions/r/disks/orig')}}
        checker = _checker(snapshots=snapshots)
        assert checker.snapshots_taken_from('orig') == []

    def test_trailing_newline_is_not_a_rescue_disk_name(self):
        checker = _checker(attached=['rescue-disk-1'])
        assert not checker.is_rescue_disk('rescue-disk-1\n')

    def test_confirm_original_disk(self):
        disks = {'orig': {'creationTimestamp': '2026-01-01', 'sizeGb': '10',
                          'users': []}}
        asked = []

        def confirm(question, details, name):
            asked.append((question, details))
            return name == 'orig'

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
            confirm=lambda q, d, n: asked.append(q) or True, notes=notes)

        assert skipped == [] and len(asked) == 1
        info = tracker.operations[1].rollback_data['disk_info']
        assert info['autoDelete'] is False and info['mode'] == 'READ_WRITE'
        assert any('set-disk-auto-delete vm --disk=orig' in n for n in notes)

        tracker = _tracker_for(checkpoint)
        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker,
            confirm=lambda q, d, n: False)
        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert len(skipped) == 1

    def test_rescue_disk_stays_when_boot_disk_is_not_put_back(self):
        compute = _compute(attached=['rescue-disk-42'], disks={
            'rescue-disk-42': {'description': rescue_disk_description(INSTANCE_ID, 'other')}})
        checkpoint = _forged_rescue_checkpoint()
        tracker = _tracker_for(checkpoint)
        notes = []

        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker, notes=notes,
            confirm=Mock(return_value=True))

        # victim-disk contradicts the rescue disk's record: not re-attached,
        # so the rescue disk is neither detached nor deleted.
        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert len(skipped) == 1 and 'victim-disk --boot' in skipped[0]
        assert any('boot disk was not put back' in n for n in notes)

    def test_confirm_needs_the_disk_name(self, monkeypatch, capsys):
        answers = iter(['y', 'orig'])
        monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
        assert checkpoint_ui._confirm_disk('q', ['Disk: orig'], 'orig') is False
        assert checkpoint_ui._confirm_disk('q', ['Disk: orig'], 'orig') is True

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

    def test_attach_undo_never_detaches_the_boot_disk_alone(self):
        # Finding: a forged checkpoint lists an attach of the VM's own boot
        # disk and no detach step. Undoing it would detach the only boot
        # disk, even when the operator vouches for the disk.
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=8, total_steps=9,
            completed_operations=[
                CompletedOperation('Attach Original Disk', 7, {
                    'vm_name': VM, 'device_name': 'persistent-disk-0'}),
                CompletedOperation('Start VM', 8, {
                    'vm_name': VM, 'original_status': 'TERMINATED'}),
            ],
            context={'original_disk_name': 'orig'})
        compute = _compute(attached=['orig'], disks={'orig': {'id': 'id-orig'}})
        compute.instances.return_value.get.return_value.execute.return_value[
            'disks'][0]['deviceName'] = 'persistent-disk-0'
        tracker = _tracker_for(checkpoint)
        confirm = Mock(return_value=True)

        notes = []
        checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker,
            confirm=confirm, notes=notes)

        # The operator was asked and agreed; the attach was still dropped.
        confirm.assert_called_once()
        assert 'Attach Original Disk' not in [
            op.operation_name for op in tracker.operations]
        assert any('no disk was detached' in n for n in notes)

    @pytest.mark.parametrize('step', ['Detach Boot Disk', 'Detach Rescue Disk'])
    def test_boot_detach_without_disk_info_keeps_every_disk(self, step):
        # Finding: without disk_info the boot disk cannot be put back, so
        # the rescue disk must be neither detached nor deleted.
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=5, total_steps=9,
            completed_operations=[
                CompletedOperation('Stop VM', 1, {'vm_name': VM}),
                CompletedOperation(step, 2, {'vm_name': VM, 'disk_name': 'orig'}),
                CompletedOperation('Create Rescue Disk', 4, {'disk_name': 'rescue-disk-42'}),
                CompletedOperation('Attach Rescue Disk', 5, {
                    'vm_name': VM, 'device_name': 'rescue-disk-42'}),
            ],
            context={'original_disk_name': 'orig', 'rescue_disk_name': 'rescue-disk-42'})
        compute = _compute(attached=['rescue-disk-42'], disks={'rescue-disk-42': {
            'description': rescue_disk_description(INSTANCE_ID, 'orig')}})
        tracker = _tracker_for(checkpoint)

        notes = []
        skipped = checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker, notes=notes)

        assert [op.operation_name for op in tracker.operations] == ['Stop VM']
        assert skipped == [
            'gcloud compute instances attach-disk vm --disk=orig --boot '
            '--zone=z --project=p']

    def test_kept_rescue_disk_attached_as_data_disk_is_not_deleted(self):
        # Finding: a rescue disk the user kept and attached again as a data
        # disk counted as "attached", so a forged rollback deleted it.
        kept = rescue_disk_description(INSTANCE_ID, 'orig')
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=5, total_steps=9,
            completed_operations=[
                CompletedOperation('Create Rescue Disk', 4, {'disk_name': 'rescue-disk-9'}),
                CompletedOperation('Attach Rescue Disk', 5, {
                    'vm_name': VM, 'device_name': 'rescue-disk-9'}),
            ],
            context={'original_disk_name': 'orig', 'rescue_disk_name': 'rescue-disk-9'})
        compute = _compute(attached=['orig', 'rescue-disk-9'], disks={'rescue-disk-9': {
            'description': kept, 'lastAttachTimestamp': '2026-11-01T00:00:00Z',
            'users': [f'projects/{PROJECT}/zones/{ZONE}/instances/{VM}']}})
        tracker = _tracker_for(checkpoint)

        checkpoint_ui._vet_checkpoint_rollback(
            compute, PROJECT, ZONE, VM, checkpoint, tracker)

        assert 'Create Rescue Disk' not in [
            op.operation_name for op in tracker.operations]

    @pytest.mark.parametrize('disk, record, owned', [
        # A victim disk named in the checkpoint.
        ('prod-database-disk', {'description': 'production data'}, False),
        # Named like a rescue disk, but made for another instance.
        ('rescue-disk-1', {'description': rescue_disk_description(OTHER_ID, 'other')}, False),
        # This VM's rescue disk, but attached before: the user may have kept it.
        ('rescue-disk-1', {'description': rescue_disk_description(INSTANCE_ID, VM),
                           'lastAttachTimestamp': '2026-11-17T09:05:00Z'}, False),
        # This VM's rescue disk, created and never attached: still cleaned up.
        ('rescue-disk-1', {'description': rescue_disk_description(INSTANCE_ID, VM)}, True),
    ])
    def test_reconciled_rescue_disk_is_vetted(self, monkeypatch, capsys, disk, record, owned):
        # Only a harmless step is checkpointed; reconciliation finds the
        # named disk in the zone and adds the delete itself.
        checkpoint = CheckpointData(
            version=1, operation='rescue', session_id='abc123',
            started_at='2026-11-17T09:00:00Z', updated_at='2026-11-17T09:00:00Z',
            current_step=1, total_steps=9,
            completed_operations=[
                CompletedOperation('Stop VM', 1, {'vm_name': VM, 'original_status': 'RUNNING'})],
            context={'original_disk_name': VM, 'rescue_disk_name': disk})
        compute = _compute(attached=[VM], disks={disk: record})
        monkeypatch.setattr(checkpoint_ui, '_create_tracked_client', lambda c, ua: c)
        seen = []
        monkeypatch.setattr(
            RollbackHandler, 'rollback',
            lambda self, tracker, ops: seen.extend(
                (op.operation_name, op.rollback_data)
                for op in tracker.get_rollback_operations()) or True)
        monkeypatch.setattr(CheckpointManager, 'clear_checkpoint', lambda self: True)

        checkpoint_ui._handle_checkpoint_rollback(compute, PROJECT, ZONE, VM, checkpoint)

        deletes = [data for name, data in seen if name == 'Create Rescue Disk']
        assert deletes == ([{'disk_name': disk}] if owned else [])
        out = capsys.readouterr()
        assert 'disks delete' not in out.out + out.err
        if not owned:
            assert f'gcloud compute disks describe {disk}' in out.out + out.err


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
        monkeypatch.setattr(OwnershipChecker, 'snapshots_taken_from',
                            lambda self, n, s=None: [])
        asked = []
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=4))
        orch.confirm_disk = lambda q, d, n: asked.append(d) or True

        assert orch.execute() is True
        assert asked == [['Disk: victim-disk',
                          'Snapshots:     no gce-rescue snapshot of this disk']]
        assert attached == ['rescue-disk-1', 'victim-disk']
        # Only the operator vouched for it: not written into markers.
        assert orch._original_unproven is True

    def test_operator_confirmed_disk_is_kept_out_of_markers(self, stub_rescue, monkeypatch):
        from gce_rescue_v2.operations import CreateDiskOperation
        descriptions = []
        monkeypatch.setattr(CreateDiskOperation, 'execute',
                            lambda self, **kw: descriptions.append(kw.get('description'))
                            or test_rescue._success_execute(self))
        _record_attach(monkeypatch)
        monkeypatch.setattr(RescueOrchestrator, '_disk_exists', lambda self, n: False)
        monkeypatch.setattr(OwnershipChecker, 'is_original_disk', lambda self, *a, **k: False)
        monkeypatch.setattr(OwnershipChecker, 'describe_disk', lambda self, n: ['x'])
        monkeypatch.setattr(RescueOrchestrator, '_instance_id', lambda self: '42')
        orch = test_rescue._make_orch()
        orch.set_resume_state(_rescue_checkpoint(step=3))
        orch.confirm_disk = lambda q, d, n: True

        assert orch.execute() is True
        assert descriptions and 'original-disk=' not in descriptions[0]

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
        monkeypatch.setattr(OwnershipChecker, 'is_disposable_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'original_disk_proof', lambda self, *a, **k: None)
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3))

        assert orch.execute() is False
        assert attached == []

    def test_resume_keeps_unproven_rescue_disk(self, stub_restore, monkeypatch, capsys):
        deleted = []
        monkeypatch.setattr(DeleteDiskOperation, 'execute',
                            lambda self, **kw: deleted.append(kw['disk_name'])
                            or test_restore._success_execute(self))
        monkeypatch.setattr(OwnershipChecker, 'is_disposable_rescue_disk', lambda self, n: False)
        monkeypatch.setattr(OwnershipChecker, 'original_disk_proof',
                            lambda self, *a, **k: OriginalDiskProof())
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3, rescue='rescue-disk-999'))

        assert orch.execute() is True
        assert deleted == []

    def test_resume_deletes_proven_rescue_disk(self, stub_restore, monkeypatch):
        deleted = []
        monkeypatch.setattr(DeleteDiskOperation, 'execute',
                            lambda self, **kw: deleted.append(kw['disk_name'])
                            or test_restore._success_execute(self))
        monkeypatch.setattr(OwnershipChecker, 'is_disposable_rescue_disk', lambda self, n: True)
        monkeypatch.setattr(OwnershipChecker, 'original_disk_proof',
                            lambda self, *a, **k: OriginalDiskProof())
        orch = test_restore._make_orch()
        orch.set_resume_state(_restore_checkpoint(step=3))

        assert orch.execute() is True
        assert deleted == ['rescue-disk-123']

    def test_resume_uses_device_names_from_the_vm(self, stub_restore, monkeypatch):
        monkeypatch.setattr(OwnershipChecker, 'original_disk_proof',
                            lambda self, *a, **k: OriginalDiskProof())
        vm = {'disks': [
            {'source': _disk_url('rescue-disk-123'), 'deviceName': 'real-rescue-dev',
             'boot': True},
            {'source': _disk_url('orig'), 'deviceName': 'real-orig-dev'}]}
        orch = RestoreOrchestrator(
            compute=test_restore.FakeCompute([vm, vm]), project=PROJECT, zone=ZONE,
            vm_name=VM, logger=None, suppress_progress=True)
        orch.set_resume_state(_restore_checkpoint(step=1, original='orig'))

        assert orch._verify_resumed_disks() is True
        assert orch.rescue_device_name == 'real-rescue-dev'
        assert orch.original_device_name == 'real-orig-dev'

    def test_resume_never_detaches_a_disk_that_is_not_the_rescue_boot_disk(
            self, stub_restore, monkeypatch):
        # Finding: rescue_disk_name from a forged restore checkpoint named
        # a data disk of the VM, and step 2 detached it.
        monkeypatch.setattr(OwnershipChecker, 'original_disk_proof',
                            lambda self, *a, **k: OriginalDiskProof())
        vm = {'id': INSTANCE_ID, 'disks': [
            {'source': _disk_url('orig'), 'deviceName': 'persistent-disk-0', 'boot': True},
            {'source': _disk_url('prod-data'), 'deviceName': 'data'}]}
        orch = RestoreOrchestrator(
            compute=test_restore.FakeCompute([vm, vm, vm]), project=PROJECT, zone=ZONE,
            vm_name=VM, logger=None, suppress_progress=True)
        orch.set_resume_state(_restore_checkpoint(step=1, original='orig',
                                                  rescue='prod-data'))

        assert orch._verify_resumed_disks() is False

    def test_resume_asks_when_only_a_snapshot_names_the_original(
            self, stub_restore, monkeypatch):
        # Finding: a marked snapshot can be made by anyone who can snapshot
        # the disk, so it no longer replaces the operator's confirmation.
        vm = {'id': INSTANCE_ID, 'disks': []}
        compute = _compute(snapshots={'pre-rescue-orig-1': _own_snapshot('orig')})
        compute.instances.return_value.get.return_value.execute.return_value = vm
        orch = RestoreOrchestrator(
            compute=compute, project=PROJECT, zone=ZONE,
            vm_name=VM, logger=None, suppress_progress=True)
        orch.set_resume_state(_restore_checkpoint(step=3, original='orig'))
        asked = []
        orch.confirm_disk = lambda q, d, n: asked.append(d) or False

        assert orch._verify_resumed_disks() is False
        assert asked and asked[0][-1] == 'Snapshots:     pre-rescue-orig-1'

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


class TestMetadataRecords:
    """Restore only acts on the keys rescue itself backs up or adds."""

    def test_unknown_backup_keys_are_not_promoted(self):
        from gce_rescue_v2.operations.set_metadata import restore_pre_rescue_items
        items = [
            {'key': 'rescue-backup-ssh-keys', 'value': 'attacker:ssh-rsa AAA'},
            {'key': 'rescue-backup-', 'value': 'x'},
            {'key': 'rescue-backup-absent-keys', 'value': 'enable-oslogin,windows-keys'},
            {'key': 'enable-oslogin', 'value': 'TRUE'},
            {'key': 'windows-keys', 'value': '{"userName":"rescue_admin"}'},
            {'key': 'rescue-backup-startup-script', 'value': 'user script'},
            {'key': 'startup-script', 'value': 'rescue script'},
        ]
        restored = {i['key']: i['value'] for i in restore_pre_rescue_items(items)}

        assert 'ssh-keys' not in restored
        assert '' not in restored
        assert restored['rescue-backup-ssh-keys'] == 'attacker:ssh-rsa AAA'
        assert restored['enable-oslogin'] == 'TRUE'
        assert 'windows-keys' not in restored
        assert restored['startup-script'] == 'user script'

    def test_other_startup_and_sysprep_scripts_are_paused(self):
        from gce_rescue_v2.operations.set_metadata import (
            SetMetadataOperation, restore_pre_rescue_items)
        op = SetMetadataOperation(Mock(), PROJECT, ZONE, None)
        before = [
            {'key': 'sysprep-specialize-script-ps1', 'value': 'join domain'},
            {'key': 'windows-startup-script-cmd', 'value': 'start app'},
            {'key': 'startup-script-url', 'value': 'gs://b/s.sh'},
            {'key': 'ssh-keys', 'value': 'k'},
        ]
        rescue_items = [{'key': 'windows-startup-script-ps1', 'value': 'rescue'},
                        {'key': 'rescue-mode', 'value': '1'}]
        merged = {i['key']: i['value'] for i in op._merge_with_backup(before, rescue_items)}

        for key in ('sysprep-specialize-script-ps1', 'windows-startup-script-cmd',
                    'startup-script-url'):
            assert key not in merged
        assert merged['rescue-backup-sysprep-specialize-script-ps1'] == 'join domain'

        restored = {i['key']: i['value'] for i in restore_pre_rescue_items(
            [{'key': k, 'value': v} for k, v in merged.items()])}
        assert restored == {
            'sysprep-specialize-script-ps1': 'join domain',
            'windows-startup-script-cmd': 'start app',
            'startup-script-url': 'gs://b/s.sh',
            'ssh-keys': 'k',
        }


class TestFreshRescueDisk:
    def test_new_rescue_never_reuses_an_existing_disk(self, stub_rescue, monkeypatch):
        # A disk with the generated name was not made by this run (another
        # rescue in the same second, or a disk someone created in advance).
        attached = _record_attach(monkeypatch)
        taken = []
        monkeypatch.setattr(
            RescueOrchestrator, '_disk_exists',
            lambda self, n: n.count('-') == 2 and len(n) == len('rescue-disk-') + 10
            and not taken.append(n))
        orch = test_rescue._make_orch()

        assert orch.execute() is True
        assert taken and attached[0] != taken[0]
        assert RESCUE_DISK_NAME.match(attached[0])


class TestRollbackReportsFailedOperations:
    @pytest.mark.parametrize('op_cls, data', [
        ('AttachDiskOperation', {'vm_name': VM, 'device_name': 'rescue-disk-1'}),
        ('DetachDiskOperation', {'vm_name': VM, 'disk_info': {
            'source': 'projects/p/zones/z/disks/orig', 'boot': True,
            'autoDelete': False, 'deviceName': 'persistent-disk-0',
            'mode': 'READ_WRITE'}}),
        ('SetMetadataOperation', {'vm_name': VM, 'original_metadata': {
            'fingerprint': 'f', 'items': []}}),
    ])
    def test_failed_operation_is_not_reported_as_rolled_back(self, op_cls, data):
        from gce_rescue_v2 import operations
        op = getattr(operations, op_cls)(Mock(), PROJECT, ZONE, None)
        op._wait_for_operation = lambda *a, **k: False
        assert op.rollback(data) is False
