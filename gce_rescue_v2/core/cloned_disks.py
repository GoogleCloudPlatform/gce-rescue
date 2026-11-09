"""
Find attached disks that are copies of the boot disk (#194).

A disk created from the boot disk, from a snapshot or image of it, or from
the same snapshot or image as the boot disk carries the same partition and
filesystem UUIDs. Linux finds its root, /boot and /boot/efi by those UUIDs,
so with both disks attached it can boot from or mount the copy. On a Debian
12 VM with a snapshot copy attached, root and /boot/efi came from the copy
on every boot, with nothing in the serial console to say so. The check
therefore reads disk lineage from the Compute API rather than the console.

Lineage of a disk: the disk itself, the disk, snapshot or image it was
created from, and the disk that snapshot or image was taken of (one hop).
Two disks whose lineages share an entry are copies of each other. Images are
compared by image ID, so disks from different versions of an image family
(which have different UUIDs) do not match.

Reads are best-effort: a resource that cannot be read is left out, and the
check never fails a diagnosis.
"""

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

# Category of the findings this check produces (diagnose_rules/cloned_disk.yaml).
CLONED_DISK_CATEGORY = 'cloned_disk'

# projects/<project>/(zones|regions)/<location>/disks/<name>, or
# projects/<project>/global/(snapshots|images)/<name>, possibly behind an
# https://www.googleapis.com/compute/<version>/ prefix.
_RESOURCE_RE = re.compile(
    r'projects/(?P<project>[^/]+)/'
    r'(?:(?P<scope>zones|regions)/(?P<location>[^/]+)|global)/'
    r'(?P<collection>disks|snapshots|images)/(?P<name>[^/]+)$')


def resource_name(url: str) -> str:
    """Last path segment of a resource URL (its name)."""
    return url.rstrip('/').rsplit('/', 1)[-1]


@dataclass(frozen=True)
class ClonedDisk:
    """An attached disk that is a copy of the boot disk."""
    name: str
    device_name: str
    reason: str            # how it relates to the boot disk, for the finding text
    regional: bool = False  # a regional disk (detach-disk needs --disk-scope)


class LineageReader:
    """Reads disks, snapshots and images by URL, with caching.

    Args:
        compute: Compute API client.
        log: Called with a message when a read fails (optional).
    """

    def __init__(self, compute, log: Optional[Callable[[str], None]] = None):
        self._compute = compute
        self._log = log or (lambda message: None)
        self._cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def get(self, url: str) -> Optional[Dict[str, Any]]:
        """The resource at `url`, or None when it cannot be read."""
        if url not in self._cache:
            self._cache[url] = self._read(url)
        return self._cache[url]

    def _read(self, url: str) -> Optional[Dict[str, Any]]:
        match = _RESOURCE_RE.search(url)
        if not match:
            self._log(f"Cloned-disk check: unrecognized resource URL {url}")
            return None
        project, scope, location, collection, name = match.group(
            'project', 'scope', 'location', 'collection', 'name')
        try:
            if collection == 'snapshots':
                request = self._compute.snapshots().get(project=project, snapshot=name)
            elif collection == 'images':
                request = self._compute.images().get(project=project, image=name)
            elif scope == 'regions':
                request = self._compute.regionDisks().get(
                    project=project, region=location, disk=name)
            else:
                request = self._compute.disks().get(
                    project=project, zone=location, disk=name)
            resource = request.execute()
            return resource if isinstance(resource, dict) else None
        except Exception as e:  # best-effort: e.g. no compute.disks.get
            self._log(f"Cloned-disk check: could not read {collection} {name}: {e}")
            return None


@dataclass(frozen=True)
class Ancestor:
    """One lineage entry: a resource a disk was (directly or one hop) made from."""
    label: str   # e.g. "snapshot nightly-1"
    via: str     # label of the direct source it was reached through ('' if direct)


def lineage(disk: Dict[str, Any], reader: LineageReader) -> Dict[str, Ancestor]:
    """Lineage of a disk, keyed '<kind>:<id>' so IDs of different kinds never collide."""
    entries: Dict[str, Ancestor] = {
        f"disk:{disk['id']}": Ancestor(f"disk {disk.get('name', '')}", '')}

    def add(kind: str, resource_id: Optional[str], url: str, via: str = '') -> None:
        if resource_id:
            entries.setdefault(f"{kind}:{resource_id}",
                               Ancestor(f"{kind} {resource_name(url)}", via))

    add('disk', disk.get('sourceDiskId'), disk.get('sourceDisk', ''))
    for kind, url_field, id_field in (('snapshot', 'sourceSnapshot', 'sourceSnapshotId'),
                                      ('image', 'sourceImage', 'sourceImageId')):
        url, source_id = disk.get(url_field, ''), disk.get(id_field)
        if not (source_id and url):
            continue
        add(kind, source_id, url)
        source = reader.get(url)
        if source:  # one hop: what that snapshot or image was made from
            via = f"{kind} {resource_name(url)}"
            add('disk', source.get('sourceDiskId'), source.get('sourceDisk', ''), via)
            add('snapshot', source.get('sourceSnapshotId'),
                source.get('sourceSnapshot', ''), via)
    return entries


def clone_reason(boot: Dict[str, Any], boot_lineage: Dict[str, Ancestor],
                 other: Dict[str, Any], other_lineage: Dict[str, Ancestor]) -> Optional[str]:
    """How `other` relates to the boot disk, or None when it is unrelated."""
    from_boot = other_lineage.get(f"disk:{boot['id']}")
    if from_boot:
        if not from_boot.via:
            return "created from the boot disk"
        return f"created from {from_boot.via}, which was made from the boot disk"
    if f"disk:{other['id']}" in boot_lineage:
        return "the boot disk was created from it"
    shared = next((ancestor for key, ancestor in other_lineage.items()
                   if key in boot_lineage), None)
    if shared:
        through = f" (through {shared.via})" if shared.via else ""
        return f"made from the same {shared.label} as the boot disk{through}"
    return None


def find_boot_disk_clones(instance: Dict[str, Any],
                          reader: LineageReader) -> List[ClonedDisk]:
    """Attached persistent disks that are copies of the boot disk."""
    attached: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for attachment in instance.get('disks', []):
        if attachment.get('type', 'PERSISTENT') != 'PERSISTENT' or not attachment.get('source'):
            continue  # Local SSD (SCRATCH) has no source and no lineage
        disk = reader.get(attachment['source'])
        if disk and disk.get('id'):
            attached.append((attachment, disk))

    boot = next((disk for attachment, disk in attached if attachment.get('boot')), None)
    if boot is None:
        return []
    boot_lineage = lineage(boot, reader)

    clones = []
    for attachment, disk in attached:
        if disk is boot:
            continue
        reason = clone_reason(boot, boot_lineage, disk, lineage(disk, reader))
        if reason:
            clones.append(ClonedDisk(
                disk.get('name', resource_name(attachment['source'])),
                attachment.get('deviceName', ''), reason,
                regional='/regions/' in attachment['source']))
    return clones


def clone_finding(clone: ClonedDisk, vm_name: str, zone: str) -> Dict[str, Any]:
    """A diagnosis finding (boot_errors entry) for one cloned disk."""
    device = f" (device {clone.device_name})" if clone.device_name else ""
    return {
        'name': 'cloned_boot_disk',
        'category': CLONED_DISK_CATEGORY,
        'severity': 'warning',
        'description': (f"Disk {clone.name}{device} looks like a copy of the boot "
                        f"disk: {clone.reason}. Both carry the same UUIDs, so the "
                        f"VM can boot from or mount the wrong disk"),
        'detected_pattern': '',
        'suggested_fixes': [
            f"If the VM should not use it, stop the VM, detach it and start the VM: "
            f"gcloud compute instances stop {vm_name} --zone={zone} && "
            f"gcloud compute instances detach-disk {vm_name} --disk={clone.name} "
            f"--zone={zone}" + (" --disk-scope=regional" if clone.regional else "")
            + f" && gcloud compute instances start {vm_name} --zone={zone}",
        ],
        'context_lines': [],
        'matched_line_index': -1,
        'kernel_version': '',
        'root_device': '',
        'disk_name': clone.name,
    }
