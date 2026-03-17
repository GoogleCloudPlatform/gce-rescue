"""
Unit tests for VM feature detection functions.

Covers:
- Architecture detection (x86_64, ARM64)
- Shielded VM detection
- Confidential VM detection
- Rescue disk type detection (Hyperdisk-only families)
"""

import pytest

from gce_rescue_v2.utils.os_detection import (
    detect_architecture,
    get_rescue_disk_type,
    is_shielded_vm,
    get_shielded_config,
    is_confidential_vm,
    get_confidential_type,
    ARCH_X86_64,
    ARCH_ARM64,
)


class TestArchitectureDetection:
    """Tests for detect_architecture function."""

    def test_x86_64_from_disk_field(self):
        """Detect x86_64 from disk architecture field."""
        vm = {
            'disks': [
                {'boot': True, 'architecture': 'X86_64', 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_X86_64

    def test_arm64_from_disk_field(self):
        """Detect ARM64 from disk architecture field."""
        vm = {
            'disks': [
                {'boot': True, 'architecture': 'ARM64', 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_ARM64

    def test_architecture_case_insensitive(self):
        """Architecture detection handles different cases."""
        vm = {
            'disks': [
                {'boot': True, 'architecture': 'arm64', 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_ARM64

    def test_arm64_from_machine_type_t2a(self):
        """Detect ARM64 from T2A machine type."""
        vm = {
            'machineType': 'zones/us-central1-a/machineTypes/t2a-standard-4',
            'disks': [
                {'boot': True, 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_ARM64

    def test_arm64_from_short_machine_type(self):
        """Detect ARM64 from short T2A machine type."""
        vm = {
            'machineType': 't2a-standard-1',
            'disks': [
                {'boot': True, 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_ARM64

    def test_default_x86_64_no_architecture_field(self):
        """Default to x86_64 when no architecture field."""
        vm = {
            'machineType': 'zones/us-central1-a/machineTypes/e2-micro',
            'disks': [
                {'boot': True, 'source': '/disks/boot'}
            ]
        }
        assert detect_architecture(vm) == ARCH_X86_64

    def test_default_x86_64_empty_vm(self):
        """Default to x86_64 when VM has minimal info."""
        vm = {}
        assert detect_architecture(vm) == ARCH_X86_64

    def test_multiple_disks_uses_boot_disk(self):
        """Uses boot disk for architecture detection."""
        vm = {
            'disks': [
                {'boot': False, 'architecture': 'X86_64', 'source': '/disks/data'},
                {'boot': True, 'architecture': 'ARM64', 'source': '/disks/boot'},
            ]
        }
        assert detect_architecture(vm) == ARCH_ARM64


class TestShieldedVMDetection:
    """Tests for Shielded VM detection functions."""

    def test_secure_boot_enabled(self):
        """Detect Secure Boot enabled."""
        vm = {
            'shieldedInstanceConfig': {
                'enableSecureBoot': True,
                'enableVtpm': True,
                'enableIntegrityMonitoring': True
            }
        }
        assert is_shielded_vm(vm) is True

    def test_secure_boot_disabled(self):
        """Detect Secure Boot disabled."""
        vm = {
            'shieldedInstanceConfig': {
                'enableSecureBoot': False,
                'enableVtpm': True,
                'enableIntegrityMonitoring': True
            }
        }
        assert is_shielded_vm(vm) is False

    def test_no_shielded_config(self):
        """No shielded config means not a shielded VM."""
        vm = {}
        assert is_shielded_vm(vm) is False

    def test_empty_shielded_config(self):
        """Empty shielded config defaults to False."""
        vm = {'shieldedInstanceConfig': {}}
        assert is_shielded_vm(vm) is False

    def test_get_shielded_config_all_enabled(self):
        """Get full shielded config when all enabled."""
        vm = {
            'shieldedInstanceConfig': {
                'enableSecureBoot': True,
                'enableVtpm': True,
                'enableIntegrityMonitoring': True
            }
        }
        config = get_shielded_config(vm)
        assert config['secure_boot'] is True
        assert config['vtpm'] is True
        assert config['integrity_monitoring'] is True

    def test_get_shielded_config_partial(self):
        """Get shielded config with partial settings."""
        vm = {
            'shieldedInstanceConfig': {
                'enableSecureBoot': True
            }
        }
        config = get_shielded_config(vm)
        assert config['secure_boot'] is True
        assert config['vtpm'] is False
        assert config['integrity_monitoring'] is False

    def test_get_shielded_config_empty(self):
        """Get shielded config when not configured."""
        vm = {}
        config = get_shielded_config(vm)
        assert config['secure_boot'] is False
        assert config['vtpm'] is False
        assert config['integrity_monitoring'] is False


class TestConfidentialVMDetection:
    """Tests for Confidential VM detection functions."""

    def test_confidential_vm_enabled(self):
        """Detect Confidential VM."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': True
            }
        }
        assert is_confidential_vm(vm) is True

    def test_confidential_vm_disabled(self):
        """Detect non-confidential VM."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': False
            }
        }
        assert is_confidential_vm(vm) is False

    def test_no_confidential_config(self):
        """No confidential config means not confidential."""
        vm = {}
        assert is_confidential_vm(vm) is False

    def test_empty_confidential_config(self):
        """Empty confidential config defaults to False."""
        vm = {'confidentialInstanceConfig': {}}
        assert is_confidential_vm(vm) is False

    def test_get_confidential_type_sev(self):
        """Get SEV type for confidential VM."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': True,
                'confidentialInstanceType': 'SEV'
            }
        }
        assert get_confidential_type(vm) == 'SEV'

    def test_get_confidential_type_sev_snp(self):
        """Get SEV-SNP type for confidential VM."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': True,
                'confidentialInstanceType': 'SEV_SNP'
            }
        }
        assert get_confidential_type(vm) == 'SEV_SNP'

    def test_get_confidential_type_tdx(self):
        """Get TDX type for confidential VM."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': True,
                'confidentialInstanceType': 'TDX'
            }
        }
        assert get_confidential_type(vm) == 'TDX'

    def test_get_confidential_type_default(self):
        """Default to SEV when type not specified."""
        vm = {
            'confidentialInstanceConfig': {
                'enableConfidentialCompute': True
            }
        }
        assert get_confidential_type(vm) == 'SEV'

    def test_get_confidential_type_not_confidential(self):
        """Return empty string for non-confidential VM."""
        vm = {}
        assert get_confidential_type(vm) == ''


class TestRescueDiskTypeDetection:
    """Tests for get_rescue_disk_type function."""

    def test_hyperdisk_c4(self):
        """C4 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/c4-standard-2'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_n4(self):
        """N4 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/n4-standard-4'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_c4a(self):
        """C4A machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/c4a-standard-2'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_c4d(self):
        """C4D machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'c4d-standard-8'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_m4(self):
        """M4 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/m4-megamem-416'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_x4(self):
        """X4 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/x4-megamem-960'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_a4(self):
        """A4 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/a4-highgpu-8g'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_h4d(self):
        """H4D machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/h4d-standard-4'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_h3(self):
        """H3 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/h3-standard-88'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_a3(self):
        """A3 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/a3-highgpu-8g'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_hyperdisk_z3(self):
        """Z3 machine type requires hyperdisk-balanced."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/z3-standard-88'}
        assert get_rescue_disk_type(vm) == 'hyperdisk-balanced'

    def test_pd_balanced_e2(self):
        """E2 machine type uses pd-balanced (default)."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/e2-micro'}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_pd_balanced_n2(self):
        """N2 machine type uses pd-balanced (default)."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/n2-standard-4'}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_pd_balanced_c3(self):
        """C3 machine type uses pd-balanced (no pd-standard support)."""
        vm = {'machineType': 'zones/us-central1-a/machineTypes/c3-standard-4'}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_pd_balanced_n1(self):
        """N1 machine type uses pd-balanced (default)."""
        vm = {'machineType': 'n1-standard-1'}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_no_machine_type(self):
        """Missing machineType field falls back to default."""
        vm = {}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_empty_machine_type(self):
        """Empty machineType string falls back to default."""
        vm = {'machineType': ''}
        assert get_rescue_disk_type(vm) == 'pd-balanced'

    def test_custom_default(self):
        """Custom default is returned for standard families."""
        vm = {'machineType': 'e2-micro'}
        assert get_rescue_disk_type(vm, default='pd-ssd') == 'pd-ssd'

    def test_custom_default_overridden_for_hyperdisk(self):
        """Custom default is overridden for Hyperdisk-only families."""
        vm = {'machineType': 'c4-standard-2'}
        assert get_rescue_disk_type(vm, default='pd-ssd') == 'hyperdisk-balanced'
