"""Tests for the fix script environment: OS_FLAVOR and BOOT_MODE (issue #92).

Covers detection from the instance resource (utils/os_detection.py), their
injection by compose_startup_script, and that both the diagnosis-driven
repair path and the --fix-script path hand them to the composed script.
"""

import logging
from typing import Any, Dict, List, Optional
from unittest.mock import Mock

import pytest

from gce_rescue_v2.core.config import RescueConfig, OS_TYPE_LINUX
from gce_rescue_v2.core.fix_catalog import SUPPORTED_FIX_CATEGORIES
from gce_rescue_v2.orchestration.compose import (
    PREMOUNT_ANCHOR,
    PREMOUNT_BEGIN_MARKER,
    PREMOUNT_END_MARKER,
    compose_startup_script,
)
from gce_rescue_v2.orchestration.repair import RepairOrchestrator
from gce_rescue_v2.orchestration.rescue import RescueOrchestrator
from gce_rescue_v2.utils.os_detection import (
    BOOT_MODE_BIOS,
    BOOT_MODE_UEFI,
    BOOT_MODE_UNKNOWN,
    detect_boot_mode,
    detect_os_flavor,
)


BASE_SCRIPT = (
    'log "waiting for disk"\n'
    f'{PREMOUNT_ANCHOR}\n'
    'mount /dev/sdb1 /mnt/sysroot\n'
    'signal_complete\n'
)

LICENSES = {
    'rhel': 'projects/rhel-cloud/global/licenses/rhel-9-server',
    'debian': 'projects/debian-cloud/global/licenses/debian-12-bookworm',
    'ubuntu': 'projects/ubuntu-os-cloud/global/licenses/ubuntu-2204-lts',
}


def _vm(features: Optional[List[str]] = None, license_url: Optional[str] = None,
        secure_boot: Optional[bool] = None,
        omit_features: bool = False) -> Dict[str, Any]:
    """Build a minimal instance resource with one boot disk."""
    disk: Dict[str, Any] = {
        'boot': True,
        'source': 'projects/p/zones/z/disks/test-boot-disk',
        'deviceName': 'test-boot-disk',
    }
    if not omit_features:
        disk['guestOsFeatures'] = [{'type': f} for f in (features or [])]
    if license_url:
        disk['licenses'] = [license_url]
    vm: Dict[str, Any] = {
        'status': 'TERMINATED',
        'disks': [disk],
        'metadata': {'items': [], 'fingerprint': 'abc'},
    }
    if secure_boot is not None:
        vm['shieldedInstanceConfig'] = {'enableSecureBoot': secure_boot}
    return vm


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class TestDetectBootMode:
    """BOOT_MODE comes from the boot disk's guest OS features."""

    def test_uefi_compatible_feature_is_uefi(self) -> None:
        vm = _vm(features=['VIRTIO_SCSI_MULTIQUEUE', 'UEFI_COMPATIBLE', 'GVNIC'])
        assert detect_boot_mode(vm) == BOOT_MODE_UEFI

    def test_features_without_uefi_is_bios(self) -> None:
        vm = _vm(features=['VIRTIO_SCSI_MULTIQUEUE'])
        assert detect_boot_mode(vm) == BOOT_MODE_BIOS

    def test_secure_boot_implies_uefi(self) -> None:
        vm = _vm(features=['VIRTIO_SCSI_MULTIQUEUE'], secure_boot=True)
        assert detect_boot_mode(vm) == BOOT_MODE_UEFI

    def test_secure_boot_disabled_does_not_imply_uefi(self) -> None:
        vm = _vm(features=['VIRTIO_SCSI_MULTIQUEUE'], secure_boot=False)
        assert detect_boot_mode(vm) == BOOT_MODE_BIOS

    def test_missing_features_is_unknown(self) -> None:
        """The API omits empty lists, so absence is not proof of BIOS."""
        assert detect_boot_mode(_vm(omit_features=True)) == BOOT_MODE_UNKNOWN

    def test_empty_features_is_unknown(self) -> None:
        assert detect_boot_mode(_vm(features=[])) == BOOT_MODE_UNKNOWN

    def test_no_boot_disk_is_unknown(self) -> None:
        vm = {'disks': [{'boot': False,
                         'guestOsFeatures': [{'type': 'UEFI_COMPATIBLE'}]}]}
        assert detect_boot_mode(vm) == BOOT_MODE_UNKNOWN

    def test_empty_instance_is_unknown(self) -> None:
        assert detect_boot_mode({}) == BOOT_MODE_UNKNOWN

    def test_only_boot_disk_features_count(self) -> None:
        """A UEFI data disk does not make a BIOS boot disk UEFI."""
        vm = _vm(features=['VIRTIO_SCSI_MULTIQUEUE'])
        vm['disks'].append({'boot': False,
                            'guestOsFeatures': [{'type': 'UEFI_COMPATIBLE'}]})
        assert detect_boot_mode(vm) == BOOT_MODE_BIOS


class TestDetectOsFlavorForEnv:
    """OS_FLAVOR reuses detect_os_flavor (license name of the boot disk)."""

    @pytest.mark.parametrize('family,expected', [
        ('rhel', 'rhel-9-server'),
        ('debian', 'debian-12-bookworm'),
        ('ubuntu', 'ubuntu-2204-lts'),
    ])
    def test_flavor_from_license(self, family: str, expected: str) -> None:
        assert detect_os_flavor(_vm(license_url=LICENSES[family])) == expected


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

class TestComposeFixEnv:
    """compose_startup_script exports OS_FLAVOR and BOOT_MODE."""

    @pytest.mark.parametrize('flavor', [
        'rhel-9-server', 'debian-12-bookworm', 'ubuntu-2204-lts'])
    def test_os_flavor_exported(self, flavor: str) -> None:
        script = compose_startup_script(BASE_SCRIPT, ['echo fix'],
                                        os_flavor=flavor)
        assert f'export OS_FLAVOR="{flavor}"' in script

    @pytest.mark.parametrize('mode', [
        BOOT_MODE_UEFI, BOOT_MODE_BIOS, BOOT_MODE_UNKNOWN])
    def test_boot_mode_exported(self, mode: str) -> None:
        script = compose_startup_script(BASE_SCRIPT, ['echo fix'],
                                        boot_mode=mode)
        assert f'export BOOT_MODE="{mode}"' in script

    def test_defined_before_fix_body(self) -> None:
        fix = 'echo "$OS_FLAVOR $BOOT_MODE"'
        script = compose_startup_script(BASE_SCRIPT, [fix],
                                        os_flavor='rhel-9-server',
                                        boot_mode=BOOT_MODE_UEFI)
        assert script.index('export OS_FLAVOR=') < script.index(fix)
        assert script.index('export BOOT_MODE=') < script.index(fix)

    def test_omitted_when_none(self) -> None:
        """Callers that pass nothing get the previous output unchanged."""
        script = compose_startup_script(BASE_SCRIPT, ['echo fix'])
        assert 'OS_FLAVOR' not in script
        assert 'BOOT_MODE' not in script

    @pytest.mark.parametrize('bad', [
        'debian"; rm -rf /; echo "', '$(reboot)', 'a b', '', '-leading-dash'])
    def test_unsafe_flavor_replaced(self, bad: str) -> None:
        script = compose_startup_script(BASE_SCRIPT, ['echo fix'],
                                        os_flavor=bad)
        assert 'export OS_FLAVOR="unknown"' in script
        if bad:
            assert bad not in script

    @pytest.mark.parametrize('bad', ['UEFI', 'efi', '$(reboot)', ''])
    def test_unexpected_boot_mode_replaced(self, bad: str) -> None:
        script = compose_startup_script(BASE_SCRIPT, ['echo fix'],
                                        boot_mode=bad)
        assert 'export BOOT_MODE="unknown"' in script

    def test_visible_to_premount_blocks(self) -> None:
        """Pre-mount blocks run before the main fix section; they get the
        variables too."""
        fix = (f'{PREMOUNT_BEGIN_MARKER}\n'
               'echo "pre $BOOT_MODE"\n'
               f'{PREMOUNT_END_MARKER}\n'
               'echo post\n')
        script = compose_startup_script(BASE_SCRIPT, [fix],
                                        os_flavor='debian-12-bookworm',
                                        boot_mode=BOOT_MODE_BIOS)
        pre = script.index('echo "pre $BOOT_MODE"')
        first_export = script.index('export BOOT_MODE="bios"')
        assert first_export < pre < script.index(PREMOUNT_ANCHOR)
        assert script.index('export OS_FLAVOR="debian-12-bookworm"') < pre

    def test_coexists_with_existing_variables(self) -> None:
        script = compose_startup_script(
            BASE_SCRIPT, ['echo fix'], repair_targets=['UUID=abc'],
            failing_kernel='6.1.0-18-amd64', root_device='/dev/sda1',
            os_flavor='debian-12-bookworm', boot_mode=BOOT_MODE_UEFI)
        for needle in ('REPAIR_TARGETS="UUID=abc"',
                       'GCE_FAILING_KERNEL="6.1.0-18-amd64"',
                       'GCE_BOOT_ROOT="/dev/sda1"',
                       'export OS_FLAVOR="debian-12-bookworm"',
                       'export BOOT_MODE="uefi"'):
            assert needle in script


# ---------------------------------------------------------------------------
# Orchestrators
# ---------------------------------------------------------------------------

def _repair_orchestrator(vm_info: Dict[str, Any]) -> RepairOrchestrator:
    compute = Mock()
    compute.instances.return_value.get.return_value.execute.return_value = vm_info
    logger = logging.getLogger('test_fix_script_env')
    orch = RepairOrchestrator(compute, 'proj', 'zone-a', 'vm-1', logger=logger)
    orch._create_tracked_client = lambda label: compute
    return orch


class TestRepairInjectsFixEnv:
    """Diagnosis-driven repair exports the variables for every category."""

    @pytest.mark.parametrize('family,flavor', [
        ('rhel', 'rhel-9-server'),
        ('debian', 'debian-12-bookworm'),
        ('ubuntu', 'ubuntu-2204-lts'),
    ])
    def test_os_flavor_from_instance(self, family: str, flavor: str) -> None:
        orch = _repair_orchestrator(
            _vm(features=['UEFI_COMPATIBLE'], license_url=LICENSES[family]))
        diagnosis = {'boot_errors': [{'category': 'fstab',
                                      'severity': 'critical'}]}
        script = orch._generate_repair_script(diagnosis)
        assert f'export OS_FLAVOR="{flavor}"' in script

    @pytest.mark.parametrize('vm_kwargs,mode', [
        ({'features': ['UEFI_COMPATIBLE']}, 'uefi'),
        ({'features': ['VIRTIO_SCSI_MULTIQUEUE']}, 'bios'),
        ({'omit_features': True}, 'unknown'),
    ])
    def test_boot_mode_from_instance(self, vm_kwargs: Dict[str, Any],
                                     mode: str) -> None:
        orch = _repair_orchestrator(
            _vm(license_url=LICENSES['debian'], **vm_kwargs))
        diagnosis = {'boot_errors': [{'category': 'fstab',
                                      'severity': 'critical'}]}
        script = orch._generate_repair_script(diagnosis)
        assert f'export BOOT_MODE="{mode}"' in script

    @pytest.mark.parametrize('category', sorted(SUPPORTED_FIX_CATEGORIES))
    def test_present_for_every_category(self, category: str) -> None:
        orch = _repair_orchestrator(
            _vm(features=['UEFI_COMPATIBLE'], license_url=LICENSES['rhel']))
        error: Dict[str, Any] = {'category': category, 'severity': 'critical'}
        if category == 'initramfs':
            # Only rebuild-addressable findings select the initramfs fix
            error['name'] = 'initramfs_no_root_fs'
        script = orch._generate_repair_script({'boot_errors': [error]})
        assert 'export OS_FLAVOR="rhel-9-server"' in script
        assert 'export BOOT_MODE="uefi"' in script


class TestCustomFixScriptInjectsFixEnv:
    """--fix-script (Linux) runs get the same variables."""

    def _orchestrator(self, vm_info: Optional[Dict[str, Any]]) -> RescueOrchestrator:
        config = RescueConfig()
        config.fix_script = '#!/bin/bash\necho "$OS_FLAVOR $BOOT_MODE"\n'
        orch = RescueOrchestrator(compute=object(), project='p', zone='z',
                                  vm_name='vm-1', config=config)
        orch.os_type = OS_TYPE_LINUX
        orch.original_disk_name = 'test-boot-disk'
        if vm_info is not None:
            orch.os_flavor = detect_os_flavor(vm_info)
            orch.boot_mode = detect_boot_mode(vm_info)
        return orch

    def test_custom_script_gets_detected_values(self) -> None:
        orch = self._orchestrator(
            _vm(features=['UEFI_COMPATIBLE'], license_url=LICENSES['ubuntu']))
        script = orch._generate_startup_script()
        assert 'export OS_FLAVOR="ubuntu-2204-lts"' in script
        assert 'export BOOT_MODE="uefi"' in script
        assert (script.index('export BOOT_MODE=')
                < script.index('echo "$OS_FLAVOR $BOOT_MODE"'))

    def test_custom_script_unknown_when_not_detected(self) -> None:
        """Resumed sessions without instance data fall back to 'unknown'."""
        script = self._orchestrator(None)._generate_startup_script()
        assert 'export OS_FLAVOR="unknown"' in script
        assert 'export BOOT_MODE="unknown"' in script

    def test_no_fix_script_no_variables(self) -> None:
        orch = self._orchestrator(_vm(features=['UEFI_COMPATIBLE']))
        orch.config.fix_script = None
        script = orch._generate_startup_script()
        assert 'OS_FLAVOR' not in script
        assert 'BOOT_MODE' not in script
