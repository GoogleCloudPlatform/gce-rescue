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
  unique even if a VM is deleted and recreated with the same name.
- The source disk Compute Engine records on a snapshot.

When ownership cannot be shown, callers skip the action and print the
command the user can run after checking the resource themselves.
"""

import re
from typing import Any, Dict, List, Optional, Set

# Rescue disks are named rescue-disk-<unix time> (see RescueOrchestrator).
RESCUE_DISK_NAME = re.compile(r'^rescue-disk-[0-9]+$')

_MARKER = re.compile(
    r'\[gce-rescue instance=(?P<instance>[0-9]+)'
    r'(?: original-disk=(?P<original>[a-z0-9-]+))?\]')


def rescue_disk_description(instance_id: Optional[str],
                            original_disk: Optional[str]) -> str:
    """Description for a rescue disk, with the ownership marker."""
    text = "Rescue disk created by gce-rescue."
    if instance_id:
        marker = f"gce-rescue instance={instance_id}"
        if original_disk:
            marker += f" original-disk={original_disk}"
        text += f" [{marker}]"
    return text


def snapshot_description(vm_name: str, instance_id: Optional[str]) -> str:
    """Description for a pre-rescue snapshot, with the ownership marker."""
    text = f"Pre-rescue safety snapshot of {vm_name}"
    if instance_id:
        text += f" [gce-rescue instance={instance_id}]"
    return text


def _marker(resource: Dict[str, Any]) -> Optional['re.Match[str]']:
    return _MARKER.search(resource.get('description') or '')


def _disk_name(source: str) -> str:
    return (source or '').rstrip('/').split('/')[-1]


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

    def attached_disks(self) -> Set[str]:
        """Names of the disks attached to this VM right now."""
        return {_disk_name(d.get('source', ''))
                for d in self._instance().get('disks', []) if d.get('source')}

    def _fetch(self, what: str, call) -> Optional[Dict[str, Any]]:
        # Any failure, or a response that is not a resource, means
        # "ownership not shown"; it never counts as proof.
        try:
            result = call()
        except Exception as e:  # pylint: disable=broad-except
            self._log(f"Could not read {what}: {e}")
            return None
        return result if isinstance(result, dict) else None

    def _get_disk(self, name: str) -> Optional[Dict[str, Any]]:
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

    def is_rescue_disk(self, name: Optional[str]) -> bool:
        """True if `name` is a rescue disk gce-rescue made for this VM.

        It must be named like a rescue disk, and either be attached to this
        VM or carry the ownership marker for this VM.
        """
        if not name or not RESCUE_DISK_NAME.match(name):
            return False
        if name in self.attached_disks():
            return True
        return self._marked_for_this_vm(self._get_disk(name)) is not None

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

    def is_original_disk(self, name: Optional[str],
                         rescue_disk: Optional[str] = None,
                         snapshot: Optional[str] = None,
                         search_snapshots: bool = False) -> bool:
        """True if `name` is this VM's own boot disk (from before rescue).

        Shown by any of:
        - the disk is attached to this VM;
        - a snapshot gce-rescue took for this VM was taken from it (the
          snapshot's source disk is recorded by Compute Engine);
        - the rescue disk attached to this VM records it as the original.

        Args:
            name: Disk to check.
            rescue_disk: Rescue disk name, if known.
            snapshot: Pre-rescue snapshot name, if known.
            search_snapshots: Also look for pre-rescue snapshots of the
                disk by name (when the snapshot name is not known).
        """
        if not name:
            return False
        if name in self.attached_disks():
            return True
        candidates = [snapshot] if snapshot else []
        if search_snapshots:
            candidates += [n for n in self._snapshots_of(name)
                           if n not in candidates]
        for candidate in candidates:
            snap = self._get_snapshot(candidate)
            if (self._marked_for_this_vm(snap)
                    and self._same_zone_disk(snap.get('sourceDisk', ''), name)):
                return True
        if rescue_disk and rescue_disk in self.attached_disks():
            match = self._marked_for_this_vm(self._get_disk(rescue_disk))
            if match and match.group('original') == name:
                return True
        return False

    def _same_zone_disk(self, source: str, name: str) -> bool:
        source = (source or '').rstrip('/')
        region = self.zone.rsplit('-', 1)[0]
        return (source.endswith(
                    f"projects/{self.project}/zones/{self.zone}/disks/{name}")
                or source.endswith(
                    f"projects/{self.project}/regions/{region}/disks/{name}"))


def attach_disk_command(vm_name: str, zone: str, project: str, disk: str,
                        boot: bool = False) -> str:
    """The gcloud command to attach a disk, for the user to run."""
    boot_flag = " --boot" if boot else ""
    return (f"gcloud compute instances attach-disk {vm_name} --disk={disk}"
            f"{boot_flag} --zone={zone} --project={project}")


def delete_disk_command(disk: str, zone: str, project: str) -> str:
    """The gcloud command to delete a disk, for the user to run."""
    return (f"gcloud compute disks delete {disk} --zone={zone} "
            f"--project={project}")


def delete_snapshot_command(snapshot: str, project: str) -> str:
    """The gcloud command to delete a snapshot, for the user to run."""
    return f"gcloud compute snapshots delete {snapshot} --project={project}"
