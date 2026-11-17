"""Validation for checkpoint data read back from VM metadata.

The checkpoint lives in the instance metadata key ``gce-rescue-checkpoint``.
Anyone with ``compute.instances.setMetadata`` on the VM can write that key,
which is a much wider group than the people who run gce-rescue. A
checkpoint is therefore untrusted input: it decides which disks get
attached or deleted on resume and rollback, and its text is printed to the
operator's terminal.

``validate_checkpoint_dict`` checks the structure once, when the
checkpoint is loaded: types, step numbers, known operation names, and
resource names that match Compute Engine naming rules. Fields that
gce-rescue does not read are dropped, and fields that must never come from
metadata (``log_file``, the legacy Windows password) are removed.
``check_checkpoint_scope`` then checks that everything in it refers to the
VM, project and zone being worked on.

A checkpoint that fails is not used. That is safe: whoever can write a bad
checkpoint can also delete it, so ignoring it gives them nothing more.
"""

import re
from datetime import datetime
from typing import Any, Dict, List, Optional

# Operation names each command records, in step order.
KNOWN_OPERATIONS = {
    'rescue': (
        "Stop VM", "Detach Boot Disk", "Create Snapshot", "Create Rescue Disk",
        "Attach Rescue Disk", "Set Metadata", "Start VM",
        "Attach Original Disk", "Verify Startup",
    ),
    'restore': (
        "Stop VM", "Detach Rescue Disk", "Detach Original Disk",
        "Attach Original Disk", "Set Metadata", "Start VM",
        "Delete Rescue Disk",
    ),
}

MAX_STEPS = 20
MAX_TEXT_LEN = 256

# Compute Engine resource names (instances, disks, snapshots).
_RESOURCE_NAME = re.compile(r'^[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?$')
_ZONE = re.compile(r'^[a-z][-a-z0-9]{1,62}$')
# Device names are chosen by the user or by Compute Engine; allow the
# characters Compute Engine accepts, nothing that could reach a terminal.
_DEVICE_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
_SESSION_ID = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_SHORT_WORD = re.compile(r'^[A-Za-z0-9_]{1,32}$')
_METADATA_KEY = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
_DISK_SOURCE = re.compile(
    r'^(?:https://(?:www|compute)\.googleapis\.com/compute/[a-z0-9]+/)?'
    r'projects/(?P<project>[^/]+)/(?P<scope>zones|regions)/(?P<location>[^/]+)'
    r'/disks/(?P<name>[^/]+)$')

_DISK_MODES = ('READ_WRITE', 'READ_ONLY')
_DISK_INTERFACES = ('SCSI', 'NVME')

# Context fields gce-rescue reads, and how to check each one.
_CONTEXT_NAMES = ('vm_name', 'original_disk_name', 'rescue_disk_name',
                  'snapshot_name')
_CONTEXT_DEVICES = ('original_device_name', 'rescue_device_name')
_CONTEXT_WORDS = ('os_type', 'architecture')
_CONTEXT_SIZES = ('original_disk_size_gb', 'previous_size_gb')

# Never taken from metadata: a local file path on the operator's machine,
# and the plaintext password older versions stored here.
_CONTEXT_DROPPED = ('log_file', 'windows_rescue_password')


class InvalidCheckpointError(ValueError):
    """The checkpoint in VM metadata failed validation and is not used."""


def _fail(reason: str) -> None:
    raise InvalidCheckpointError(reason)


def _require_type(value: Any, expected: type, field: str) -> None:
    # bool is a subclass of int; a step count of True is not valid.
    if expected is int and isinstance(value, bool):
        _fail(f"{field} must be an integer")
    if not isinstance(value, expected):
        _fail(f"{field} has the wrong type")


def _check_name(value: Any, field: str, pattern: 're.Pattern[str]') -> None:
    if not isinstance(value, str) or not pattern.match(value):
        _fail(f"{field} is not a valid name")


def _check_timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str) or len(value) > 64:
        _fail(f"{field} is not a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        _fail(f"{field} is not a timestamp")
    if parsed.tzinfo is None:
        _fail(f"{field} has no time zone")


def _check_size(value: Any, field: str) -> None:
    if value is None:
        return
    _require_type(value, int, field)
    if not 1 <= value <= 65536:
        _fail(f"{field} is out of range")


def validate_disk_info(info: Any, field: str) -> Dict[str, Any]:
    """Check a disk attachment record (the body used to re-attach a disk)."""
    _require_type(info, dict, field)
    source = info.get('source')
    if not isinstance(source, str) or not _DISK_SOURCE.match(source):
        _fail(f"{field}.source is not a disk path")
    _check_name(_DISK_SOURCE.match(source).group('name'),
                f"{field}.source", _RESOURCE_NAME)
    clean: Dict[str, Any] = {'source': source}
    for key in ('boot', 'autoDelete'):
        if key in info:
            _require_type(info[key], bool, f"{field}.{key}")
            clean[key] = info[key]
    if 'deviceName' in info:
        _check_name(info['deviceName'], f"{field}.deviceName", _DEVICE_NAME)
        clean['deviceName'] = info['deviceName']
    if 'mode' in info:
        if info['mode'] not in _DISK_MODES:
            _fail(f"{field}.mode is not a disk mode")
        clean['mode'] = info['mode']
    if info.get('interface') is not None:
        if info['interface'] not in _DISK_INTERFACES:
            _fail(f"{field}.interface is not a disk interface")
        clean['interface'] = info['interface']
    return clean


def _validate_boot_attachment(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    _require_type(value, dict, 'original_boot_attachment')
    clean: Dict[str, Any] = {}
    if 'deviceName' in value:
        _check_name(value['deviceName'], 'original_boot_attachment.deviceName',
                    _DEVICE_NAME)
        clean['deviceName'] = value['deviceName']
    if 'autoDelete' in value:
        _require_type(value['autoDelete'], bool,
                      'original_boot_attachment.autoDelete')
        clean['autoDelete'] = value['autoDelete']
    if 'mode' in value:
        if value['mode'] not in _DISK_MODES:
            _fail("original_boot_attachment.mode is not a disk mode")
        clean['mode'] = value['mode']
    if value.get('interface') is not None:
        if value['interface'] not in _DISK_INTERFACES:
            _fail("original_boot_attachment.interface is not a disk interface")
        clean['interface'] = value['interface']
    return clean


def _validate_context(context: Any) -> Dict[str, Any]:
    _require_type(context, dict, 'context')
    clean: Dict[str, Any] = {}
    for key in _CONTEXT_NAMES:
        if context.get(key) is not None:
            _check_name(context[key], key, _RESOURCE_NAME)
            clean[key] = context[key]
    for key in _CONTEXT_DEVICES:
        if context.get(key) is not None:
            _check_name(context[key], key, _DEVICE_NAME)
            clean[key] = context[key]
    for key in _CONTEXT_WORDS:
        if context.get(key) is not None:
            _check_name(context[key], key, _SHORT_WORD)
            clean[key] = context[key]
    for key in _CONTEXT_SIZES:
        if context.get(key) is not None:
            _check_size(context[key], key)
            clean[key] = context[key]
    if context.get('zone') is not None:
        _check_name(context['zone'], 'zone', _ZONE)
        clean['zone'] = context['zone']
    if 'original_boot_attachment' in context:
        clean['original_boot_attachment'] = _validate_boot_attachment(
            context['original_boot_attachment'])
    return clean


def _validate_metadata_body(value: Any, field: str) -> Dict[str, Any]:
    _require_type(value, dict, field)
    items = value.get('items', [])
    _require_type(items, list, f"{field}.items")
    clean_items: List[Dict[str, Any]] = []
    for item in items:
        _require_type(item, dict, f"{field}.items[]")
        key, val = item.get('key'), item.get('value')
        if not isinstance(key, str) or not _METADATA_KEY.match(key):
            _fail(f"{field}.items[] has an invalid key")
        clean_item = {'key': key}
        if val is not None:
            _require_type(val, str, f"{field}.items[].value")
            clean_item['value'] = val
        clean_items.append(clean_item)
    clean: Dict[str, Any] = {'items': clean_items}
    if 'fingerprint' in value:
        _require_type(value['fingerprint'], str, f"{field}.fingerprint")
        clean['fingerprint'] = value['fingerprint']
    return clean


def _validate_rollback_data(name: str, data: Any) -> Dict[str, Any]:
    """Keep only the rollback fields the operation reads, checked."""
    field = f"rollback data for '{name}'"
    _require_type(data, dict, field)
    clean: Dict[str, Any] = {}
    if data.get('vm_name') is not None:
        _check_name(data['vm_name'], f"{field}: vm_name", _RESOURCE_NAME)
        clean['vm_name'] = data['vm_name']

    if name in ("Stop VM", "Start VM"):
        status = data.get('original_status')
        if status is not None:
            _check_name(status, f"{field}: original_status", _SHORT_WORD)
            clean['original_status'] = status
    elif name in ("Detach Boot Disk", "Detach Rescue Disk",
                  "Detach Original Disk"):
        if data.get('disk_info') is not None:
            clean['disk_info'] = validate_disk_info(
                data['disk_info'], f"{field}: disk_info")
        if data.get('disk_name') is not None:
            _check_name(data['disk_name'], f"{field}: disk_name",
                        _RESOURCE_NAME)
            clean['disk_name'] = data['disk_name']
    elif name in ("Attach Rescue Disk", "Attach Original Disk"):
        if data.get('device_name') is not None:
            _check_name(data['device_name'], f"{field}: device_name",
                        _DEVICE_NAME)
            clean['device_name'] = data['device_name']
    elif name in ("Create Rescue Disk", "Delete Rescue Disk"):
        if data.get('disk_name') is not None:
            _check_name(data['disk_name'], f"{field}: disk_name",
                        _RESOURCE_NAME)
            clean['disk_name'] = data['disk_name']
    elif name == "Create Snapshot":
        for key in ('snapshot_name', 'disk_name'):
            if data.get(key) is not None:
                _check_name(data[key], f"{field}: {key}", _RESOURCE_NAME)
                clean[key] = data[key]
        for key in ('created_by_operation', 'async_mode'):
            if key in data:
                _require_type(data[key], bool, f"{field}: {key}")
                clean[key] = data[key]
    elif name == "Set Metadata":
        if data.get('original_metadata') is not None:
            clean['original_metadata'] = _validate_metadata_body(
                data['original_metadata'], f"{field}: original_metadata")
    return clean


def validate_checkpoint_dict(data: Any) -> Dict[str, Any]:
    """Check checkpoint data from metadata and return a cleaned copy.

    Args:
        data: The parsed JSON value of the checkpoint metadata key.

    Returns:
        A new dict with the same layout, holding only checked fields.

    Raises:
        InvalidCheckpointError: The data is not a checkpoint gce-rescue
            wrote. The message names the first problem found, never the
            bad value itself.
    """
    _require_type(data, dict, 'checkpoint')
    version = data.get('version', 1)
    _require_type(version, int, 'version')
    if version != 1:
        _fail("version is not supported")

    operation = data.get('operation')
    if operation not in KNOWN_OPERATIONS:
        _fail("operation is not rescue or restore")

    session_id = data.get('session_id')
    _check_name(session_id, 'session_id', _SESSION_ID)
    _check_timestamp(data.get('started_at'), 'started_at')
    _check_timestamp(data.get('updated_at'), 'updated_at')

    current_step = data.get('current_step')
    total_steps = data.get('total_steps')
    _require_type(current_step, int, 'current_step')
    _require_type(total_steps, int, 'total_steps')
    if not 0 <= current_step <= total_steps <= MAX_STEPS:
        _fail("step numbers are out of range")

    ops = data.get('completed_operations', [])
    _require_type(ops, list, 'completed_operations')
    if len(ops) > MAX_STEPS:
        _fail("too many completed operations")
    known = KNOWN_OPERATIONS[operation]
    clean_ops = []
    for op in ops:
        _require_type(op, dict, 'completed operation')
        name = op.get('name')
        if name not in known:
            _fail(f"completed operation is not a known {operation} step")
        step = op.get('step')
        _require_type(step, int, 'completed operation step')
        if not 1 <= step <= total_steps:
            _fail("completed operation step is out of range")
        clean_ops.append({
            'name': name,
            'step': step,
            'rollback_data': _validate_rollback_data(
                name, op.get('rollback_data', {})),
        })

    return {
        'version': version,
        'operation': operation,
        'session_id': session_id,
        'started_at': data['started_at'],
        'updated_at': data['updated_at'],
        'current_step': current_step,
        'total_steps': total_steps,
        'completed_operations': clean_ops,
        'context': _validate_context(data.get('context', {})),
    }


def disk_source_project(source: str) -> Optional[str]:
    """Return the project in a disk path, or None if it is not one."""
    match = _DISK_SOURCE.match(source or '')
    return match.group('project') if match else None


def check_checkpoint_scope(checkpoint: Any, project: str, zone: str,
                           vm_name: str) -> None:
    """Check that a validated checkpoint only refers to this VM.

    Every operation replayed from a checkpoint runs against the VM named
    in its rollback data. A checkpoint that names another VM, zone or
    project would make gce-rescue act on a resource the operator never
    chose, so it is rejected.

    Args:
        checkpoint: A CheckpointData built from validated data.
        project: Project the command runs in.
        zone: Zone the command runs in.
        vm_name: Instance the command runs on.

    Raises:
        InvalidCheckpointError: Something in the checkpoint points
            elsewhere.
    """
    context = checkpoint.context
    if context.get('vm_name') not in (None, vm_name):
        _fail("checkpoint belongs to a different instance")
    if context.get('zone') not in (None, zone):
        _fail("checkpoint belongs to a different zone")
    for op in checkpoint.completed_operations:
        data = op.rollback_data
        if data.get('vm_name') not in (None, vm_name):
            _fail(f"rollback data for '{op.name}' names a different instance")
        info = data.get('disk_info')
        if info and disk_source_project(info['source']) != project:
            _fail(f"rollback data for '{op.name}' names a disk in a "
                  f"different project")
