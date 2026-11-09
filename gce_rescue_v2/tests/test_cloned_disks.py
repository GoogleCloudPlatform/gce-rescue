"""Tests for the cloned boot disk check (core/cloned_disks.py, #194)."""

from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from gce_rescue_v2.core.cloned_disks import (
    CLONED_DISK_CATEGORY,
    ClonedDisk,
    LineageReader,
    clone_finding,
    clone_reason,
    find_boot_disk_clones,
    lineage,
)

API = 'https://www.googleapis.com/compute/v1/'
ZONE = 'projects/p/zones/z'


def disk_url(name: str, scope: str = ZONE) -> str:
    return f'{API}{scope}/disks/{name}'


def snapshot_url(name: str) -> str:
    return f'{API}projects/p/global/snapshots/{name}'


def image_url(name: str, project: str = 'p') -> str:
    return f'{API}projects/{project}/global/images/{name}'


class FakeCompute:
    """Compute client serving resources by (collection, project, name)."""

    def __init__(self, resources: Dict[str, Dict[str, Any]],
                 errors: Optional[Dict[str, Exception]] = None) -> None:
        self.resources = resources       # keyed by URL
        self.errors = errors or {}       # keyed by URL
        self.calls: List[str] = []

    def _request(self, url: str) -> Mock:
        self.calls.append(url)
        if url in self.errors:
            return Mock(execute=Mock(side_effect=self.errors[url]))
        return Mock(execute=Mock(return_value=self.resources[url]))

    def disks(self) -> Mock:
        return Mock(get=lambda project, zone, disk: self._request(
            disk_url(disk, f'projects/{project}/zones/{zone}')))

    def regionDisks(self) -> Mock:
        return Mock(get=lambda project, region, disk: self._request(
            disk_url(disk, f'projects/{project}/regions/{region}')))

    def snapshots(self) -> Mock:
        return Mock(get=lambda project, snapshot: self._request(
            f'{API}projects/{project}/global/snapshots/{snapshot}'))

    def images(self) -> Mock:
        return Mock(get=lambda project, image: self._request(
            image_url(image, project)))


def disk(name: str, disk_id: str, **source: str) -> Dict[str, Any]:
    """A Disk resource; `source` holds sourceDisk/sourceSnapshot/sourceImage(+Id)."""
    return {'name': name, 'id': disk_id, 'selfLink': disk_url(name), **source}


# The boot disk of most tests: created from a public image.
BOOT = disk('vm-1', '100', sourceImage=image_url('debian-12-v2', 'debian-cloud'),
            sourceImageId='900')
PUBLIC_IMAGE = {'name': 'debian-12-v2', 'id': '900'}


def instance(*attached: Dict[str, Any]) -> Dict[str, Any]:
    return {'disks': list(attached)}


def attachment(name: str, boot: bool = False, device: str = '',
               scope: str = ZONE) -> Dict[str, Any]:
    return {'type': 'PERSISTENT', 'boot': boot, 'source': disk_url(name, scope),
            'deviceName': device or name}


def check(resources: Dict[str, Dict[str, Any]], *attached: Dict[str, Any],
          errors: Optional[Dict[str, Exception]] = None) -> List[ClonedDisk]:
    compute = FakeCompute(resources, errors)
    return find_boot_disk_clones(instance(*attached), LineageReader(compute))


def resources_with(*disks: Dict[str, Any], **extra: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    found = {d['selfLink']: d for d in disks}
    found[image_url('debian-12-v2', 'debian-cloud')] = PUBLIC_IMAGE
    found.update(extra)
    return found


# ---------------------------------------------------------------------------
# Which disks are copies
# ---------------------------------------------------------------------------

class TestFindBootDiskClones:

    def test_snapshot_of_the_boot_disk(self) -> None:
        """The customer case: snapshot the boot disk, create a disk from it."""
        clone = disk('copy', '200', sourceSnapshot=snapshot_url('nightly'),
                     sourceSnapshotId='300')
        snap = {'name': 'nightly', 'id': '300', 'sourceDisk': BOOT['selfLink'],
                'sourceDiskId': '100'}
        found = check(resources_with(BOOT, clone, **{snapshot_url('nightly'): snap}),
                      attachment('vm-1', boot=True, device='persistent-disk-0'),
                      attachment('copy', device='persistent-disk-1'))
        assert found == [ClonedDisk(
            'copy', 'persistent-disk-1',
            'created from snapshot nightly, which was made from the boot disk')]

    def test_disk_cloned_directly_from_the_boot_disk(self) -> None:
        clone = disk('copy', '200', sourceDisk=BOOT['selfLink'], sourceDiskId='100')
        found = check(resources_with(BOOT, clone),
                      attachment('vm-1', boot=True), attachment('copy'))
        assert [c.reason for c in found] == ['created from the boot disk']

    def test_custom_image_of_the_boot_disk(self) -> None:
        clone = disk('copy', '200', sourceImage=image_url('golden'), sourceImageId='400')
        golden = {'name': 'golden', 'id': '400', 'sourceDisk': BOOT['selfLink'],
                  'sourceDiskId': '100'}
        found = check(resources_with(BOOT, clone, **{image_url('golden'): golden}),
                      attachment('vm-1', boot=True), attachment('copy'))
        assert found[0].reason == ('created from image golden, which was made '
                                   'from the boot disk')

    def test_same_image_version_as_the_boot_disk(self) -> None:
        """Disks from one image version share its filesystem UUIDs."""
        clone = disk('data', '200', sourceImage=image_url('debian-12-v2', 'debian-cloud'),
                     sourceImageId='900')
        found = check(resources_with(BOOT, clone),
                      attachment('vm-1', boot=True), attachment('data'))
        assert found[0].reason == ('made from the same image debian-12-v2 as the '
                                   'boot disk')

    def test_same_snapshot_as_the_boot_disk(self) -> None:
        boot = disk('vm-1', '100', sourceSnapshot=snapshot_url('base'), sourceSnapshotId='300')
        clone = disk('copy', '200', sourceSnapshot=snapshot_url('base'), sourceSnapshotId='300')
        base = {'name': 'base', 'id': '300'}
        found = check(resources_with(boot, clone, **{snapshot_url('base'): base}),
                      attachment('vm-1', boot=True), attachment('copy'))
        assert found[0].reason == 'made from the same snapshot base as the boot disk'

    def test_snapshots_of_the_same_older_disk(self) -> None:
        """Boot disk and copy restored from two snapshots of one old disk."""
        boot = disk('vm-1', '100', sourceSnapshot=snapshot_url('mon'), sourceSnapshotId='301')
        clone = disk('copy', '200', sourceSnapshot=snapshot_url('tue'), sourceSnapshotId='302')
        old = disk_url('old-disk')
        found = check(resources_with(boot, clone, **{
            snapshot_url('mon'): {'id': '301', 'sourceDisk': old, 'sourceDiskId': '50'},
            snapshot_url('tue'): {'id': '302', 'sourceDisk': old, 'sourceDiskId': '50'},
        }), attachment('vm-1', boot=True), attachment('copy'))
        assert found[0].reason == ('made from the same disk old-disk as the boot '
                                   'disk (through snapshot tue)')

    def test_boot_disk_created_from_the_attached_disk(self) -> None:
        boot = disk('vm-1', '100', sourceDisk=disk_url('golden-disk'), sourceDiskId='200')
        original = disk('golden-disk', '200')
        found = check(resources_with(boot, original),
                      attachment('vm-1', boot=True), attachment('golden-disk'))
        assert found[0].reason == 'the boot disk was created from it'

    @pytest.mark.parametrize('data_disk', [
        disk('blank', '200'),                                        # blank disk
        disk('newer', '200', sourceImage=image_url('debian-12-v3', 'debian-cloud'),
             sourceImageId='901'),                                    # other image version
    ])
    def test_unrelated_disks_are_not_flagged(self, data_disk: Dict[str, Any]) -> None:
        found = check(resources_with(BOOT, data_disk, **{
            image_url('debian-12-v3', 'debian-cloud'): {'id': '901'}}),
            attachment('vm-1', boot=True), attachment(data_disk['name']))
        assert found == []

    def test_regional_disk_is_read_with_region_disks(self) -> None:
        regional = 'projects/p/regions/r'
        clone = {**disk('copy', '200', sourceDisk=BOOT['selfLink'], sourceDiskId='100'),
                 'selfLink': disk_url('copy', regional)}
        found = check(resources_with(BOOT, clone),
                      attachment('vm-1', boot=True), attachment('copy', scope=regional))
        assert [c.name for c in found] == ['copy']
        assert found[0].regional
        assert ('--disk=copy --zone=z --disk-scope=regional'
                in clone_finding(found[0], 'vm-1', 'z')['suggested_fixes'][0])

    def test_local_ssd_and_unreadable_disks_are_skipped(self) -> None:
        clone = disk('copy', '200', sourceDisk=BOOT['selfLink'], sourceDiskId='100')
        found = check(
            resources_with(BOOT, clone),
            attachment('vm-1', boot=True), attachment('copy'),
            attachment('secret'), {'type': 'SCRATCH', 'interface': 'NVME'},
            errors={disk_url('secret'): RuntimeError('403 compute.disks.get denied')})
        assert [c.name for c in found] == ['copy']

    def test_unreadable_boot_disk_means_no_findings(self) -> None:
        clone = disk('copy', '200', sourceDisk=BOOT['selfLink'], sourceDiskId='100')
        found = check(resources_with(clone), attachment('vm-1', boot=True),
                      attachment('copy'),
                      errors={BOOT['selfLink']: RuntimeError('403')})
        assert found == []

    def test_unreadable_snapshot_still_matches_on_the_snapshot_itself(self) -> None:
        """Without the hop, two disks from one snapshot still match."""
        boot = disk('vm-1', '100', sourceSnapshot=snapshot_url('s'), sourceSnapshotId='300')
        clone = disk('copy', '200', sourceSnapshot=snapshot_url('s'), sourceSnapshotId='300')
        found = check(resources_with(boot, clone), attachment('vm-1', boot=True),
                      attachment('copy'),
                      errors={snapshot_url('s'): RuntimeError('403')})
        assert [c.name for c in found] == ['copy']


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class TestLineageReader:

    def test_each_resource_is_read_once(self) -> None:
        compute = FakeCompute({snapshot_url('s'): {'id': '1'}})
        reader = LineageReader(compute)
        assert reader.get(snapshot_url('s')) == reader.get(snapshot_url('s')) == {'id': '1'}
        assert compute.calls == [snapshot_url('s')]

    def test_failures_are_logged_and_return_none(self) -> None:
        log: List[str] = []
        compute = FakeCompute({}, errors={snapshot_url('s'): RuntimeError('boom')})
        reader = LineageReader(compute, log=log.append)
        assert reader.get(snapshot_url('s')) is None
        assert reader.get('https://example.com/not-a-compute-url') is None
        assert any('boom' in line for line in log)
        assert any('unrecognized' in line for line in log)


class TestLineage:

    def test_keys_are_namespaced_by_kind(self) -> None:
        """A snapshot ID equal to a disk ID must not look like the same thing."""
        d = disk('d', '7', sourceSnapshot=snapshot_url('s'), sourceSnapshotId='7')
        keys = lineage(d, LineageReader(FakeCompute({snapshot_url('s'): {'id': '7'}})))
        assert set(keys) == {'disk:7', 'snapshot:7'}

    def test_clone_reason_none_for_unrelated(self) -> None:
        reader = LineageReader(FakeCompute(resources_with(BOOT)))
        other = disk('blank', '200')
        assert clone_reason(BOOT, lineage(BOOT, reader), other,
                            lineage(other, reader)) is None


class TestCloneFinding:

    def test_finding_names_the_disk_and_how_to_detach_it(self) -> None:
        finding = clone_finding(
            ClonedDisk('copy', 'persistent-disk-1', 'created from the boot disk'),
            'vm-1', 'us-central1-a')
        assert finding['category'] == CLONED_DISK_CATEGORY == 'cloned_disk'
        assert finding['severity'] == 'warning'
        assert finding['disk_name'] == 'copy'
        assert 'Disk copy (device persistent-disk-1)' in finding['description']
        assert 'created from the boot disk' in finding['description']
        (fix,) = finding['suggested_fixes']
        # Stop first: detaching the disk the VM booted from pulls its root away.
        assert fix.index('instances stop vm-1') < fix.index(
            'detach-disk vm-1 --disk=copy --zone=us-central1-a') < fix.index(
            'instances start vm-1')


# ---------------------------------------------------------------------------
# Wiring: rule file, diagnose, report
# ---------------------------------------------------------------------------

SERIAL_HEALTHY = (
    "[    0.000000] Linux version 6.1.0-54-cloud-amd64 (debian-kernel) #1 SMP\n"
    "systemd[1]: Startup finished in 1.7s (kernel) + 17.2s (userspace) = 18.9s.\n"
)
DEBIAN_LICENSE = 'projects/debian-cloud/global/licenses/debian-12-bookworm'


def _vm_with_clone(licenses: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        'status': 'RUNNING',
        'disks': [{**attachment('vm-1', boot=True, device='persistent-disk-0'),
                   'licenses': licenses or [DEBIAN_LICENSE]},
                  attachment('copy', device='persistent-disk-1')],
        'metadata': {'items': []},
    }


def _diagnose(vm: Dict[str, Any], resources: Dict[str, Dict[str, Any]],
              serial: str = SERIAL_HEALTHY, errors=None) -> Dict[str, Any]:
    from gce_rescue_v2.operations.diagnose import DiagnoseOperation
    fake = FakeCompute(resources, errors)
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = vm
    compute.instances.return_value.getSerialPortOutput.return_value \
        .execute.return_value = {'contents': serial}
    compute.disks, compute.regionDisks = fake.disks, fake.regionDisks
    compute.snapshots, compute.images = fake.snapshots, fake.images
    result = DiagnoseOperation(compute, 'p', 'z', Mock()).execute('vm-1')
    assert result.success
    return result.rollback_data


CLONE = disk('copy', '200', sourceDisk=BOOT['selfLink'], sourceDiskId='100')


class TestDiagnoseIntegration:

    def test_healthy_vm_with_a_copy_gets_a_warning(self) -> None:
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE))
        assert data['diagnosis_status'] == 'boot_errors_detected'
        (finding,) = data['boot_errors']
        assert finding['category'] == 'cloned_disk'
        assert finding['disk_name'] == 'copy'

    def test_no_copy_stays_healthy(self) -> None:
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, disk('copy', '200')))
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_windows_vm_is_not_checked(self) -> None:
        """Linux-only: Windows handles duplicate disk signatures itself."""
        vm = _vm_with_clone(['projects/windows-cloud/global/licenses/windows-server-2022-dc'])
        data = _diagnose(vm, resources_with(BOOT, CLONE))
        assert all(e['category'] != 'cloned_disk' for e in data['boot_errors'])

    def test_missing_disk_permission_does_not_fail_diagnosis(self) -> None:
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE),
                         errors={BOOT['selfLink']: RuntimeError('403 disks.get')})
        assert data['diagnosis_status'] == 'healthy'

    def test_a_bug_in_the_check_does_not_fail_diagnosis(self, monkeypatch) -> None:
        import gce_rescue_v2.operations.diagnose as diagnose_mod
        monkeypatch.setattr(diagnose_mod, 'find_boot_disk_clones',
                            Mock(side_effect=KeyError('id')))
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE))
        assert data['diagnosis_status'] == 'healthy'

    def test_copy_is_reported_next_to_serial_findings(self) -> None:
        serial = SERIAL_HEALTHY.split('\n')[0] + (
            "\nTimed out waiting for device /dev/disk/by-uuid/0B1C-2D3E.\n"
            "Dependency failed for /boot/efi.\nYou are in emergency mode.\n")
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE), serial=serial)
        categories = [e['category'] for e in data['boot_errors']]
        assert 'fstab' in categories and categories[-1] == 'cloned_disk'


class TestRuleFile:

    def test_cloned_disk_is_detect_only_and_not_auto_repaired(self) -> None:
        from gce_rescue_v2.core.fix_catalog import (
            CATEGORY_FIX_GUIDANCE, DETECT_ONLY_CATEGORIES, SUPPORTED_FIX_CATEGORIES)
        assert 'cloned_disk' in DETECT_ONLY_CATEGORIES
        assert 'cloned_disk' not in SUPPORTED_FIX_CATEGORIES
        assert 'findmnt' in CATEGORY_FIX_GUIDANCE['cloned_disk']

    @pytest.mark.parametrize('data,message', [
        ({'category': 'x', 'source': 'api', 'patterns': [
            {'name': 'p', 'severity': 'warning', 'description': 'd', 'regex': ['x']}]},
         "have no serial patterns"),
        ({'category': 'x', 'source': 'api', 'patterns': 'nope'}, "must be a list"),
        ({'category': 'x', 'source': 'magic', 'patterns': []}, "'source' must be one of"),
        ({'category': 'x', 'patterns': []}, "non-empty list"),
    ])
    def test_source_validation(self, data: Dict[str, Any], message: str) -> None:
        from gce_rescue_v2.core.diagnosis import _validate_pattern_file
        with pytest.raises(ValueError, match=message):
            _validate_pattern_file(data, 'test.yaml')

    def test_api_source_without_patterns_is_valid(self) -> None:
        from gce_rescue_v2.core.diagnosis import _validate_pattern_file
        _validate_pattern_file({'category': 'x', 'source': 'api', 'patterns': []}, 't.yaml')


class TestReport:

    def test_text_report_names_the_disk_without_rescue_steps(self) -> None:
        from gce_rescue_v2.utils.report_formatter import DiagnosisReportFormatter
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE))
        data.update(vm_name='vm-1', zone='z')
        report = DiagnosisReportFormatter().format_report(data)
        assert '[CLONED_DISK] Disk copy (device persistent-disk-1)' in report
        assert 'Detach the copy of the boot disk:' in report
        assert 'detach-disk vm-1 --disk=copy --zone=z' in report
        assert 'Stop the VM before detaching' in report
        assert 'gce-rescue rescue' not in report

    def test_copy_comes_first_when_disk_findings_need_rescue(self) -> None:
        """fsck /dev/sdb1 in rescue mode may hit the copy: detach it first."""
        from gce_rescue_v2.utils.report_formatter import DiagnosisReportFormatter
        serial = SERIAL_HEALTHY.split('\n')[0] + (
            "\nTimed out waiting for device /dev/disk/by-uuid/0B1C-2D3E.\n"
            "Dependency failed for /boot/efi.\nYou are in emergency mode.\n")
        data = _diagnose(_vm_with_clone(), resources_with(BOOT, CLONE), serial=serial)
        data.update(vm_name='vm-1', zone='z')
        report = DiagnosisReportFormatter().format_report(data)
        first, rest = report.split('Then, if the VM still does not boot:')
        assert 'First, deal with the copy of the boot disk' in first
        assert 'detach-disk vm-1 --disk=copy' in first
        assert 'gce-rescue rescue' in rest and 'gce-rescue rescue' not in first
