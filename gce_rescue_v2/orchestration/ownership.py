"""Check that disks and snapshots named in a checkpoint belong to this VM.

Resume and rollback act on resources named in the checkpoint, which is VM
metadata that people other than the operator can write. Validation
(checkpoint_validation) makes sure the names are well formed and that the
checkpoint is about this VM; this module checks that each disk or snapshot
really belongs to the rescue of this VM before gce-rescue deletes it or
attaches it.

Proof of ownership comes only from places a metadata writer cannot change:

- Disks attached to this VM right now (attaching needs other permissions).
- An ownership marker in the description of disks and snapshots that
  gce-rescue creates. A description is set when the resource is created
  and cannot be changed afterwards, and it needs no permission beyond
  creating the resource. The marker names the VM's instance ID, which is
  unique even if a VM is deleted and recreated with the same name. It also
  records whether the boot disk was set to auto-delete, so that setting is
  never taken from metadata.
- The source disk Compute Engine records on a snapshot.

When ownership cannot be shown, callers ask the operator to confirm the
disk (resume and rollback are interactive), or skip the action and print
the command the user can run after checking the resource themselves.
"""

import re
from typing import Any, Callable, Dict, List, Optional, Set

# Rescue disks are named rescue-disk-<unix time> (see RescueOrchestrator).
# \Z, not $: $ would also accept a trailing newline.
RESCUE_DISK_NAME = re.compile(r'^rescue-disk-[0-9]+\Z')

_MARKER = re.compile(
    r'\[gce-rescue instance=(?P<instance>[0-9]+)'
    r'(?: original-disk=(?P<original>[a-z0-9-]+))?'
    r'(?: auto-delete=(?P<auto_delete>[01]))?\]')

# Asks the operator about a disk: (question, detail lines, disk name)
# -> yes/no.
ConfirmDisk = Callable[[str, List[str], str], bool]


def _marker_text(instance_id: str, original_disk: Optional[str],
                 auto_delete: Optional[bool]) -> str:
    marker = f"gce-rescue instance={instance_id}"
    if original_disk:
        marker += f" original-disk={original_disk}"
    if auto_delete is not None:
        marker += f" auto-delete={1 if auto_delete else 0}"
    return f"[{marker}]"


def rescue_disk_description(instance_id: Optional[str],
                            original_disk: Optional[str],
                            auto_delete: Optional[bool] = None) -> str:
    """Description for a rescue disk, with the ownership marker.

    Args:
        instance_id: Instance ID of the VM (marker omitted when unknown).
        original_disk: The VM's boot disk before rescue.
        auto_delete: Whether that boot disk was set to auto-delete.
    """
    text = "Rescue disk created by gce-rescue."
    if instance_id:
        text += " " + _marker_text(instance_id, original_disk, auto_delete)
    return text


def snapshot_description(vm_name: str, instance_id: Optional[str],
                         auto_delete: Optional[bool] = None) -> str:
    """Description for a pre-rescue snapshot, with the ownership marker."""
    text = f"Pre-rescue safety snapshot of {vm_name}"
    if instance_id:
        text += " " + _marker_text(instance_id, None, auto_delete)
    return text


def _marker(resource: Dict[str, Any]) -> Optional['re.Match[str]']:
    description = resource.get('description')
    return _MARKER.search(description if isinstance(description, str) else '')


def _disk_name(source: str) -> str:
    return (source or '').rstrip('/').split('/')[-1]


def zonal_disk_name(source: Optional[str], project: str,
                    zone: str) -> Optional[str]:
    """Disk name if `source` is a zonal disk in this project and zone.

    gce-rescue only attaches zonal disks of the VM's own project and zone.
    Comparing only the last path segment would let a regional disk, or a
    disk elsewhere, pass for one of the same name.
    """
    if not isinstance(source, str):
        return None
    path = source.rstrip('/')
    prefix = f"projects/{project}/zones/{zone}/disks/"
    for base in ('', 'https://www.googleapis.com/compute/v1/',
                 'https://compute.googleapis.com/compute/v1/'):
        if path.startswith(base + prefix):
            name = path[len(base + prefix):]
            if name and '/' not in name:
                return name
    return None


def zonal_disk_source(project: str, zone: str, disk: str) -> str:
    """Attach path of a zonal disk, as gce-rescue builds it."""
    return f"projects/{project}/zones/{zone}/disks/{disk}"


class OriginalDiskProof:
    """Why a disk is taken to be this VM's original boot disk.

    Attributes:
        auto_delete: The boot disk's auto-delete setting recorded in the
            ownership marker, or None when the proof carries no record.
    """

    def __init__(self, auto_delete: Optional[bool] = None):
        self.auto_delete = auto_delete


def _auto_delete_of(match: 're.Match[str]') -> Optional[bool]:
    value = match.group('auto_delete')
    return None if value is None else value == '1'


class OwnershipChecker:
    """Answers "does this disk or snapshot belong to the rescue of this VM?".

    Each check returns True only with positive proof; any API error or
    missing resource counts as "not shown".

    Args:
        compute: Compute API client.
        project: Project of the VM.
        zone: Zone of the VM.
        vm_name: The VM being rescued or restored.
        logger: Optional logger for debug output.
    """

    def __init__(self, compute, project: str, zone: str, vm_name: str,
                 logger=None):
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.logger = logger
        self._vm: Optional[Dict[str, Any]] = None

    def _log(self, message: str) -> None:
        if self.logger:
            self.logger.debug(f"[Ownership] {message}")

    def _instance(self) -> Dict[str, Any]:
        if self._vm is None:
            self._vm = self._fetch(
                f"instance {self.vm_name}",
                lambda: self.compute.instances().get(
                    project=self.project, zone=self.zone,
                    instance=self.vm_name).execute()) or {}
        return self._vm

    def refresh(self) -> None:
        """Forget the cached VM, e.g. after attaching or detaching disks."""
        self._vm = None

    @property
    def instance_id(self) -> Optional[str]:
        value = self._instance().get('id')
        return str(value) if value else None

    def _attached(self) -> List[Dict[str, Any]]:
        disks = self._instance().get('disks') or []
        return [d for d in disks if isinstance(d, dict)]

    def attached_disks(self) -> Set[str]:
        """Names of this project's zonal disks attached to this VM now."""
        names = set()
        for d in self._attached():
            name = zonal_disk_name(d.get('source'), self.project, self.zone)
            if name:
                names.add(name)
        return names

    def device_disk(self, device_name: Optional[str]) -> Optional[str]:
        """Disk attached to this VM under `device_name`, if any."""
        if not device_name:
            return None
        for d in self._attached():
            if d.get('deviceName') == device_name:
                return zonal_disk_name(d.get('source'), self.project,
                                       self.zone) or _disk_name(
                                           d.get('source', ''))
        return None

    def _fetch(self, what: str, call) -> Optional[Dict[str, Any]]:
        # Any failure, or a response that is not a resource, means
        # "ownership not shown"; it never counts as proof.
        try:
            result = call()
        except Exception as e:  # pylint: disable=broad-except
            self._log(f"Could not read {what}: {e}")
            return None
        return result if isinstance(result, dict) else None

    def get_disk(self, name: str) -> Optional[Dict[str, Any]]:
        """The disk resource, or None if it cannot be read."""
        return self._fetch(f"disk {name}", lambda: self.compute.disks().get(
            project=self.project, zone=self.zone, disk=name).execute())

    def _get_snapshot(self, name: str) -> Optional[Dict[str, Any]]:
        return self._fetch(f"snapshot {name}",
                           lambda: self.compute.snapshots().get(
                               project=self.project, snapshot=name).execute())

    def _marked_for_this_vm(self, resource: Optional[Dict[str, Any]]
                            ) -> Optional['re.Match[str]']:
        if not resource:
            return None
        match = _marker(resource)
        instance_id = self.instance_id
        if match and instance_id and match.group('instance') == instance_id:
            return match
        return None

    def rescue_disk_marker(self, name: Optional[str]
                           ) -> Optional['re.Match[str]']:
        """Ownership marker of rescue disk `name`, if it is for this VM.

        Anyone who can create disks can write such a marker, so on its own
        it only shows that a disk is safe to treat as gce-rescue's (to
        delete it, or to keep it). What it says about the original disk
        counts only while the rescue disk is attached to this VM (see
        original_disk_proof).
        """
        if not name or not RESCUE_DISK_NAME.match(name):
            return None
        return self._marked_for_this_vm(self.get_disk(name))

    def is_rescue_disk(self, name: Optional[str]) -> bool:
        """True if `name` is a rescue disk gce-rescue made for this VM.

        It must be named like a rescue disk, and either be attached to this
        VM or carry the ownership marker for this VM.
        """
        if not name or not RESCUE_DISK_NAME.match(name):
            return False
        if name in self.attached_disks():
            return True
        return self.rescue_disk_marker(name) is not None

    def is_disposable_rescue_disk(self, name: Optional[str]) -> bool:
        """True if rescue disk `name` may be deleted by a rollback.

        Either it is attached to this VM now, or it is marked for this VM
        and was never attached (created by the interrupted run before it
        was attached). A rescue disk that was attached before and is not
        attached now may be one the user kept (restore --keep-rescue-disk)
        and is never deleted.
        """
        if not name or not RESCUE_DISK_NAME.match(name):
            return False
        if name in self.attached_disks():
            return True
        disk = self.get_disk(name)
        return (self._marked_for_this_vm(disk) is not None
                and not disk.get('lastAttachTimestamp')
                and not disk.get('users'))

    def is_own_snapshot(self, name: Optional[str]) -> bool:
        """True if `name` is a snapshot gce-rescue took for this VM."""
        if not name:
            return False
        return self._marked_for_this_vm(self._get_snapshot(name)) is not None

    def _snapshots_of(self, disk: str) -> List[str]:
        """Names of pre-rescue snapshots of `disk` (by gce-rescue naming)."""
        result = self._fetch(f"snapshots of {disk}",
                             lambda: self.compute.snapshots().list(
                                 project=self.project,
                                 filter=f'name:pre-rescue-{disk}-*').execute())
        items = (result or {}).get('items') or []
        return [i['name'] for i in items
                if isinstance(i, dict) and isinstance(i.get('name'), str)]

    def contradicts_record(self, name: Optional[str],
                           rescue_disk: Optional[str]) -> bool:
        """True if rescue disk `rescue_disk` records an original other than `name`."""
        if not name or not rescue_disk:
            return False
        match = self.rescue_disk_marker(rescue_disk)
        recorded = match.group('original') if match else None
        return bool(recorded) and recorded != name

    def original_disk_proof(self, name: Optional[str],
                            rescue_disk: Optional[str] = None,
                            snapshot: Optional[str] = None,
                            search_snapshots: bool = False,
                            allow_attached: bool = False
                            ) -> Optional[OriginalDiskProof]:
        """Proof that `name` is this VM's own boot disk from before rescue.

        Shown by any of:
        - a snapshot gce-rescue took for this VM was taken from this very
          disk (Compute Engine records the source disk's ID; making a
          snapshot needs read access to the disk);
        - the rescue disk attached to this VM records it as the original
          (attaching needs permissions a metadata writer may not have);
        - the disk is attached to this VM, only if `allow_attached`.

        Never shown when the rescue disk records a different original.

        Args:
            name: Disk to check.
            rescue_disk: Rescue disk name, if known.
            snapshot: Pre-rescue snapshot name, if known.
            search_snapshots: Also look for pre-rescue snapshots of the
                disk by name (when the snapshot name is not known).
            allow_attached: Accept "attached to this VM" as proof. Being
                attached says nothing about which disk is the boot disk,
                so callers that attach or re-attach a disk leave it off.

        Returns:
            The proof, or None if ownership was not shown.
        """
        if not name or self.contradicts_record(name, rescue_disk):
            return None
        proof = None
        if rescue_disk and rescue_disk in self.attached_disks():
            match = self.rescue_disk_marker(rescue_disk)
            if match and match.group('original') == name:
                proof = OriginalDiskProof(_auto_delete_of(match))
                if proof.auto_delete is not None:
                    return proof
        candidates = [snapshot] if snapshot else []
        if search_snapshots:
            candidates += [n for n in self._snapshots_of(name)
                           if n not in candidates]
        disk_id = None
        for candidate in candidates:
            snap = self._get_snapshot(candidate)
            match = self._marked_for_this_vm(snap)
            if not match or zonal_disk_name(snap.get('sourceDisk'), self.project,
                                            self.zone) != name:
                continue
            if disk_id is None:
                disk_id = str((self.get_disk(name) or {}).get('id') or '')
            # Same disk, not a newer one with the same name.
            if not disk_id or str(snap.get('sourceDiskId') or '') != disk_id:
                continue
            found = OriginalDiskProof(_auto_delete_of(match))
            if proof is None or found.auto_delete is not None:
                proof = found
            if proof.auto_delete is not None:
                return proof
        if proof is None and allow_attached and name in self.attached_disks():
            proof = OriginalDiskProof()
        return proof

    def is_original_disk(self, name: Optional[str],
                         rescue_disk: Optional[str] = None,
                         snapshot: Optional[str] = None,
                         search_snapshots: bool = False,
                         allow_attached: bool = False) -> bool:
        """True if `name` is shown to be this VM's own boot disk.

        See original_disk_proof.
        """
        return self.original_disk_proof(
            name, rescue_disk=rescue_disk, snapshot=snapshot,
            search_snapshots=search_snapshots,
            allow_attached=allow_attached) is not None

    def describe_disk(self, name: str) -> Optional[List[str]]:
        """Facts about disk `name` for the operator to judge it by.

        Returns:
            Lines to show, or None if the disk cannot be read or is in use
            by another instance (such a disk is never offered).
        """
        disk = self.get_disk(name)
        if not disk:
            return None
        users = [_disk_name(u) for u in disk.get('users') or []
                 if isinstance(u, str)]
        if any(u != self.vm_name for u in users):
            return None

        # Shown in a terminal: keep only plain values.
        def plain(value: Any, limit: int = 80) -> str:
            text = str(value) if value not in (None, '') else '-'
            return ''.join(c for c in text if c.isprintable())[:limit]

        labels = disk.get('labels') if isinstance(disk.get('labels'), dict) else {}
        lines = [
            f"Disk:          {plain(name)}",
            f"Description:   {plain(disk.get('description'), 120)}",
            f"Labels:        {plain(', '.join(f'{k}={v}' for k, v in labels.items()), 120)}",
            f"Created:       {plain(disk.get('creationTimestamp'))}",
            f"Size:          {plain(disk.get('sizeGb'))} GB",
            f"Source image:  {plain(_disk_name(disk.get('sourceImage') or ''))}",
            f"Last attached: {plain(disk.get('lastAttachTimestamp'))}",
            f"Last detached: {plain(disk.get('lastDetachTimestamp'))}",
            f"Attached to:   {plain(', '.join(users) or 'nothing')}",
        ]
        return lines

    def confirm_original_disk(self, name: Optional[str],
                              confirm: Optional[ConfirmDisk],
                              action: str,
                              rescue_disk: Optional[str] = None) -> bool:
        """Ask the operator whether `name` is this VM's boot disk.

        Used when ownership cannot be shown, for example after a rescue
        with --no-snapshot or one made by an older version. Returns False
        without asking when there is no one to ask, when the disk is
        missing or in use by another instance, or when the rescue disk
        records a different original.

        Args:
            name: The disk.
            confirm: Callback that asks the operator, or None.
            action: What will be done with the disk, for the question.
            rescue_disk: Rescue disk name, if known.
        """
        if not name or confirm is None:
            return False
        if self.contradicts_record(name, rescue_disk):
            return False
        details = self.describe_disk(name)
        if details is None:
            return False
        question = (f"gce-rescue could not confirm that disk [{name}] is the "
                    f"boot disk of instance [{self.vm_name}] from before the "
                    f"rescue. The name comes from instance metadata, which "
                    f"others can change. {action} only if you know it is "
                    f"this instance's boot disk.")
        return bool(confirm(question, details, name))


def attach_disk_command(vm_name: str, zone: str, project: str, disk: str,
                        boot: bool = False) -> str:
    """The gcloud command to attach a disk, for the user to run."""
    boot_flag = " --boot" if boot else ""
    return (f"gcloud compute instances attach-disk {vm_name} --disk={disk}"
            f"{boot_flag} --zone={zone} --project={project}")


def detach_disk_command(vm_name: str, zone: str, project: str,
                        device_name: str) -> str:
    """The gcloud command to detach a disk by device name."""
    return (f"gcloud compute instances detach-disk {vm_name} "
            f"--device-name={device_name} --zone={zone} --project={project}")


def auto_delete_command(vm_name: str, zone: str, project: str,
                        disk: str) -> str:
    """The gcloud command to turn on auto-delete for an attached disk."""
    return (f"gcloud compute instances set-disk-auto-delete {vm_name} "
            f"--disk={disk} --auto-delete --zone={zone} --project={project}")


def describe_disk_command(disk: str, zone: str, project: str) -> str:
    """The gcloud command to look at a disk.

    Shown for a disk gce-rescue could not confirm instead of a delete
    command: the name comes from metadata others can write, so the user
    is never handed a ready-to-run delete for it.
    """
    return (f"gcloud compute disks describe {disk} --zone={zone} "
            f"--project={project}")


def delete_disk_command(disk: str, zone: str, project: str) -> str:
    """The gcloud command to delete a disk, for the user to run."""
    return (f"gcloud compute disks delete {disk} --zone={zone} "
            f"--project={project}")


def delete_snapshot_command(snapshot: str, project: str) -> str:
    """The gcloud command to delete a snapshot, for the user to run."""
    return f"gcloud compute snapshots delete {snapshot} --project={project}"
