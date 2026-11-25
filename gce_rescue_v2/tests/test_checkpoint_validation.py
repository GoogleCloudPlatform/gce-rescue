"""Tests for checkpoint validation and terminal-safe output.

Checkpoint metadata and serial console output can be written by people
other than the operator, so both are treated as untrusted input.
"""

import copy
import json
from unittest.mock import Mock

import pytest

from gce_rescue_v2.cli.checkpoint_ui import (
    _print_quiet_checkpoint_block,
    _prompt_incomplete_operation,
    _warn_ignored_checkpoint,
)
from gce_rescue_v2.orchestration.checkpoint import (
    CHECKPOINT_KEY,
    CheckpointData,
    CheckpointManager,
    CompletedOperation,
)
from gce_rescue_v2.orchestration.checkpoint_validation import (
    InvalidCheckpointError,
    validate_checkpoint_dict,
)
from gce_rescue_v2.utils.report_formatter import (
    DiagnosisReportFormatter,
    _decode_systemd_escapes,
)
from gce_rescue_v2.utils.safe_text import safe_text

ESC = '\x1b'
BEL = '\x07'
PROJECT = 'test-project'
ZONE = 'us-central1-a'
VM = 'test-vm'
DISK_SOURCE = (f'https://www.googleapis.com/compute/v1/projects/{PROJECT}'
               f'/zones/{ZONE}/disks/orig-disk')


def _no_control_chars(text: str) -> bool:
    return not any(ord(c) < 0x20 and c not in '\n\t' or 0x7f <= ord(c) <= 0x9f
                   for c in text)


def _rescue_checkpoint() -> dict:
    """A checkpoint as gce-rescue writes it during a rescue."""
    return {
        'version': 1,
        'operation': 'rescue',
        'session_id': 'a1b2c3d4',
        'started_at': '2026-11-17T09:15:42.123456Z',
        'updated_at': '2026-11-17T09:16:10.654321Z',
        'current_step': 6,
        'total_steps': 9,
        'completed_operations': [
            {'name': 'Stop VM', 'step': 1, 'rollback_data': {
                'vm_name': VM, 'original_status': 'RUNNING'}},
            {'name': 'Detach Boot Disk', 'step': 2, 'rollback_data': {
                'vm_name': VM, 'disk_info': {
                    'source': DISK_SOURCE, 'boot': True, 'autoDelete': True,
                    'deviceName': 'persistent-disk-0', 'mode': 'READ_WRITE',
                    'interface': 'NVME'}}},
            {'name': 'Create Snapshot', 'step': 3, 'rollback_data': {
                'snapshot_name': 'pre-rescue-orig-disk-1763370942',
                'disk_name': 'orig-disk', 'created_by_operation': True,
                'async_mode': True}},
            {'name': 'Create Rescue Disk', 'step': 4, 'rollback_data': {
                'disk_name': 'rescue-disk-1763370950'}},
            {'name': 'Attach Rescue Disk', 'step': 5, 'rollback_data': {
                'vm_name': VM, 'device_name': 'rescue-disk-1763370950'}},
            {'name': 'Set Metadata', 'step': 6, 'rollback_data': {
                'vm_name': VM, 'original_metadata': {
                    'fingerprint': 'abc=', 'items': [
                        {'key': 'startup-script', 'value': '#!/bin/bash\n'},
                        {'key': 'empty-key'}]}}},
        ],
        'context': {
            'vm_name': VM, 'zone': ZONE,
            'original_disk_name': 'orig-disk',
            'original_device_name': 'persistent-disk-0',
            'rescue_disk_name': 'rescue-disk-1763370950',
            'snapshot_name': 'pre-rescue-orig-disk-1763370942',
            'os_type': 'linux', 'architecture': 'x86_64',
            'original_disk_size_gb': 10,
            'original_boot_attachment': {
                'deviceName': 'persistent-disk-0', 'autoDelete': True,
                'mode': 'READ_WRITE', 'interface': 'NVME'},
        },
    }


def _manager_with(checkpoint_value: str) -> CheckpointManager:
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = {
        'metadata': {'fingerprint': 'f', 'items': [
            {'key': CHECKPOINT_KEY, 'value': checkpoint_value}]}}
    return CheckpointManager(compute, PROJECT, ZONE, VM)


class TestSafeText:
    def test_escapes_control_characters(self):
        text = f'ok{ESC}]0;title{BEL}\x9b2J\x00'
        assert safe_text(text) == 'ok\\x1b]0;title\\x07\\x9b2J\\x00'

    def test_escapes_bidi_overrides(self):
        assert safe_text('a‮b⁦c') == 'a\\u202eb\\u2066c'

    def test_newlines_escaped_tabs_optional(self):
        assert safe_text('a\nb\tc') == 'a\\x0ab\\x09c'
        assert safe_text('a\nb\tc', keep_tabs=True) == 'a\\x0ab\tc'

    def test_plain_text_unchanged(self):
        assert safe_text('rescue-disk-123 (Stop VM) 5') == 'rescue-disk-123 (Stop VM) 5'
        assert safe_text(None) == ''
        assert safe_text(7) == '7'

    def test_truncation_never_splits_an_escape(self):
        assert safe_text('abcde' + ESC, max_len=7) == 'abcde...'
        assert safe_text('abcdefghij', max_len=4) == 'abcd...'


class TestSerialOutputEscapes:
    def test_decodes_printable_systemd_escapes(self):
        assert _decode_systemd_escapes('dev-disk-by\\x2duuid') == 'dev-disk-by-uuid'

    def test_does_not_decode_control_characters(self):
        text = 'x\\x1b]8;;https://example.test\\x07click\\x1b]8;;\\x07'
        assert _decode_systemd_escapes(text) == text
        assert ESC not in _decode_systemd_escapes('\\x1b[2J\\x9b\\x00\\x7f')

    def test_report_never_prints_control_characters(self, monkeypatch):
        # Colors off, so any ESC in the report came from the serial text.
        monkeypatch.setattr('gce_rescue_v2.utils.colors._is_tty', lambda: False)
        hostile = ('Failed to mount /data\\x1b]0;owned\\x07 '
                   f'{ESC}]8;;https://example.test{BEL}x{BEL}\x9b2J')
        diagnosis = {
            'vm_name': VM, 'zone': ZONE, 'status': 'TERMINATED',
            'os_type': 'linux', 'os_flavor': 'debian-12',
            'architecture': 'x86_64',
            'diagnosis_status': 'boot_errors_detected',
            'recommendations': [],
            'boot_errors': [{
                'category': 'fstab', 'severity': 'critical',
                'suggested_fixes': [],
                'description': 'fstab entry failed',
                'detected_pattern': hostile,
                'context_lines': ['boot', hostile, 'next'],
                'matched_line_index': 1,
            }],
        }
        report = DiagnosisReportFormatter().format_report(diagnosis)
        assert _no_control_chars(report)
        assert '\\x1b' in report


class TestValidateCheckpoint:
    def test_checkpoint_written_by_gce_rescue_is_accepted(self):
        data = _rescue_checkpoint()
        checkpoint = CheckpointData.from_dict(copy.deepcopy(data))
        assert checkpoint.to_dict() == data

    def test_restore_checkpoint_is_accepted(self):
        data = {
            'version': 1, 'operation': 'restore', 'session_id': 'abc123',
            'started_at': '2026-11-17T09:15:42+00:00',
            'updated_at': '2026-11-17T09:15:42+00:00',
            'current_step': 2, 'total_steps': 7,
            'completed_operations': [
                {'name': 'Stop VM', 'step': 1, 'rollback_data': {
                    'vm_name': VM, 'original_status': 'RUNNING'}},
                {'name': 'Detach Rescue Disk', 'step': 2, 'rollback_data': {
                    'vm_name': VM, 'disk_name': 'rescue-disk-1'}},
            ],
            'context': {'rescue_disk_name': 'rescue-disk-1',
                        'rescue_device_name': 'rescue-disk-1',
                        'original_boot_attachment': None},
        }
        assert CheckpointData.from_dict(data).current_step == 2

    def test_host_path_and_password_are_dropped(self):
        data = _rescue_checkpoint()
        data['context']['log_file'] = '/home/operator/.bashrc'
        data['context']['windows_rescue_password'] = 'secret'
        data['context']['unknown'] = 'x'
        context = CheckpointData.from_dict(data).context
        assert 'log_file' not in context
        assert 'windows_rescue_password' not in context
        assert 'unknown' not in context

    def test_unknown_rollback_fields_are_dropped(self):
        data = _rescue_checkpoint()
        data['completed_operations'][0]['rollback_data']['extra'] = 'x'
        checkpoint = CheckpointData.from_dict(data)
        assert 'extra' not in checkpoint.completed_operations[0].rollback_data

    @pytest.mark.parametrize('mutate', [
        # Terminal escapes in printed fields.
        lambda d: d['completed_operations'][-1].update(name=f'Set Metadata{ESC}[2J'),
        lambda d: d.update(started_at=f'{ESC}]0;x{BEL}2026-11-17T09:15:42Z'),
        lambda d: d.update(current_step=f'6{ESC}[2J'),
        lambda d: d.update(total_steps='9'),
        # Structure.
        lambda d: d.update(operation='repair'),
        lambda d: d.update(version=2),
        lambda d: d.update(session_id='../../x'),
        lambda d: d.update(current_step=10),
        lambda d: d.update(current_step=True),
        lambda d: d.update(total_steps=500, current_step=6),
        lambda d: d.update(started_at='2026-11-17T09:15:42'),
        lambda d: d.update(completed_operations={'name': 'Stop VM'}),
        lambda d: d['completed_operations'].append(
            {'name': 'Delete Everything', 'step': 7, 'rollback_data': {}}),
        lambda d: d['completed_operations'].append(
            {'name': 'Delete Rescue Disk', 'step': 7, 'rollback_data': {}}),
        lambda d: d['completed_operations'][0].update(step=12),
        # Resource names.
        lambda d: d['context'].update(rescue_disk_name='Prod Database'),
        lambda d: d['context'].update(rescue_disk_name='rescue-disk-1\n'),
        lambda d: d['context'].update(original_disk_name='disk/../other'),
        lambda d: d['context'].update(zone=f'us{ESC}'),
        lambda d: d['completed_operations'][3]['rollback_data'].update(
            disk_name='projects/other/zones/z/disks/db'),
        lambda d: d['completed_operations'][1]['rollback_data']['disk_info'].update(
            source='file:///etc/passwd'),
        lambda d: d['completed_operations'][1]['rollback_data']['disk_info'].update(
            mode='SHARED'),
        lambda d: d['completed_operations'][5]['rollback_data'][
            'original_metadata']['items'].append({'key': f'k{ESC}', 'value': 'v'}),
    ])
    def test_invalid_checkpoint_is_rejected(self, mutate):
        data = _rescue_checkpoint()
        mutate(data)
        with pytest.raises(InvalidCheckpointError):
            validate_checkpoint_dict(data)

    def test_error_message_does_not_echo_the_value(self):
        data = _rescue_checkpoint()
        data['context']['rescue_disk_name'] = f'x{ESC}]0;owned{BEL}'
        with pytest.raises(InvalidCheckpointError) as exc:
            validate_checkpoint_dict(data)
        assert ESC not in str(exc.value)
        assert 'owned' not in str(exc.value)


class TestCheckpointScope:
    def test_valid_checkpoint_loads(self):
        manager = _manager_with(json.dumps(_rescue_checkpoint()))
        checkpoint = manager.load_checkpoint()
        assert checkpoint is not None
        assert manager.invalid_reason is None

    @pytest.mark.parametrize('mutate, reason', [
        (lambda d: d['completed_operations'][5]['rollback_data'].update(
            vm_name='other-vm'), 'different instance'),
        (lambda d: d['completed_operations'][0]['rollback_data'].update(
            vm_name='other-vm'), 'different instance'),
        (lambda d: d['context'].update(vm_name='other-vm'), 'different instance'),
        (lambda d: d['context'].update(zone='europe-west1-b'), 'different zone'),
        (lambda d: d['completed_operations'][1]['rollback_data']['disk_info'].update(
            source=DISK_SOURCE.replace(PROJECT, 'victim-project')),
         'different project'),
    ])
    def test_checkpoint_for_another_resource_is_ignored(self, mutate, reason):
        data = _rescue_checkpoint()
        mutate(data)
        manager = _manager_with(json.dumps(data))
        assert manager.load_checkpoint() is None
        assert reason in manager.invalid_reason
        assert manager.detect_incomplete('rescue') is None

    def test_invalid_json_is_ignored_with_reason(self):
        manager = _manager_with('{not json')
        assert manager.load_checkpoint() is None
        assert manager.invalid_reason == 'it is not valid JSON'

    def test_update_keeps_working_on_own_checkpoint(self):
        """A checkpoint gce-rescue writes must pass its own validation."""
        stored = {}
        compute = Mock()

        def get_vm(**kwargs):
            items = [{'key': k, 'value': v} for k, v in stored.items()]
            return Mock(execute=Mock(return_value={
                'metadata': {'fingerprint': 'f', 'items': items}}))

        def set_metadata(**kwargs):
            stored.clear()
            stored.update({i['key']: i['value'] for i in kwargs['body']['items']})
            return Mock(execute=Mock(return_value={}))

        compute.instances.return_value.get.side_effect = get_vm
        compute.instances.return_value.setMetadata.side_effect = set_metadata
        manager = CheckpointManager(compute, PROJECT, ZONE, VM)
        context = copy.deepcopy(_rescue_checkpoint()['context'])
        manager.create_checkpoint('rescue', total_steps=9, context=context)
        for op in _rescue_checkpoint()['completed_operations']:
            assert manager.update_checkpoint(op['step'], op['name'],
                                             rollback_data=op['rollback_data'])
        checkpoint = manager.load_checkpoint()
        assert checkpoint.current_step == 6
        assert len(checkpoint.completed_operations) == 6


class TestCheckpointPrompts:
    @staticmethod
    def _checkpoint() -> CheckpointData:
        # Built directly, so it skips validation: the prompts must stay
        # safe even if a hostile value reached them.
        return CheckpointData(
            version=1, operation='rescue', session_id='abc',
            started_at=f'2026-11-17T09:15:42Z{ESC}[2J',
            updated_at='2026-11-17T09:15:42Z',
            current_step=3, total_steps=f'9{ESC}]0;x{BEL}',
            completed_operations=[CompletedOperation(
                f'Stop VM{ESC}[1A{ESC}[2K[1] Rollback is safe', 1, {})],
            context={})

    def test_interactive_prompt_escapes_checkpoint_fields(self, monkeypatch, capsys):
        checkpoint = self._checkpoint()
        monkeypatch.setattr(CheckpointData, 'get_age_display', lambda self: 'now')
        monkeypatch.setattr('builtins.input', lambda *a: '3')
        assert _prompt_incomplete_operation(checkpoint, 'rescue') == 'abort'
        out = capsys.readouterr().out
        assert ESC not in out and BEL not in out
        assert 'Stop VM\\x1b[1A' in out

    def test_quiet_block_escapes_checkpoint_fields(self, monkeypatch, capsys):
        checkpoint = self._checkpoint()
        monkeypatch.setattr(CheckpointData, 'get_age_display', lambda self: 'now')
        _print_quiet_checkpoint_block(checkpoint, 'rescue', VM, ZONE, PROJECT)
        err = capsys.readouterr().err
        assert ESC not in err and BEL not in err

    def test_ignored_checkpoint_warning(self, capsys):
        manager = _manager_with(json.dumps({'operation': 'rescue'}))
        manager.load_checkpoint()
        _warn_ignored_checkpoint(manager, VM)
        err = capsys.readouterr().err
        assert f'Ignoring the gce-rescue checkpoint on instance [{VM}]' in err

    def test_no_warning_when_checkpoint_used(self, capsys):
        manager = _manager_with(json.dumps(_rescue_checkpoint()))
        manager.load_checkpoint()
        _warn_ignored_checkpoint(manager, VM)
        assert capsys.readouterr().err == ''
