"""
Unit tests for VM feature detection functions.

Covers:
- Architecture detection (x86_64, ARM64)
- Shielded VM detection
- Confidential VM detection
"""

import pytest

from gce_rescue_v2.utils.os_detection import (
    detect_architecture,
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
