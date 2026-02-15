"""
GCE Rescue - OS and VM Feature Detection Utility

Detects VM characteristics:
- Operating system (Windows/Linux)
- CPU architecture (x86_64/ARM64)
- Shielded VM configuration
- Confidential VM configuration
"""

from typing import Dict, Any, Tuple
from ..core.config import OS_TYPE_LINUX, OS_TYPE_WINDOWS

# Architecture types
ARCH_X86_64 = 'x86_64'
ARCH_ARM64 = 'arm64'

# License types
LICENSE_FREE = 'free'
LICENSE_PAYG = 'payg'
LICENSE_BYOS = 'byos'

# Projects that charge per-instance license fees
_PAYG_LICENSE_PROJECTS = frozenset({
    'rhel-cloud',
    'rhel-sap-cloud',
    'suse-cloud',
    'suse-sap-cloud',
    'sles-cloud',
    'windows-cloud',
    'windows-sql-cloud',
})

def detect_os_type(vm: Dict[str, Any]) -> str:
    """
    Detect the operating system type of a VM.

    Args:
        vm: VM instance response from GCP API

    Returns:
        'windows' or 'linux'

    Detection methods (in order of reliability):
    1. guestOsFeatures - GCP sets WINDOWS feature for Windows VMs
    2. Disk licenses - Windows disks have windows license URLs
    3. Boot disk source image - Check if image name contains 'windows'
    """

    # Method 1: Check guestOsFeatures (most reliable)
    for disk in vm.get('disks', []):
        guest_features = disk.get('guestOsFeatures', [])
        for feature in guest_features:
            if feature.get('type') == 'WINDOWS':
                return OS_TYPE_WINDOWS

    # Method 2: Check disk licenses
    for disk in vm.get('disks', []):
        licenses = disk.get('licenses', [])
        for license_url in licenses:
            if 'windows' in license_url.lower():
                return OS_TYPE_WINDOWS

    # Method 3: Check boot disk source image name
    for disk in vm.get('disks', []):
        if disk.get('boot'):
            source = disk.get('source', '').lower()
            if 'windows' in source:
                return OS_TYPE_WINDOWS

    # Default to Linux
    return OS_TYPE_LINUX


def get_os_display_name(os_type: str) -> str:
    """
    Get human-readable OS name.

    Args:
        os_type: 'windows' or 'linux'

    Returns:
        Display name string
    """
    if os_type == OS_TYPE_WINDOWS:
        return "Windows"
    return "Linux"


def detect_os_from_compute(compute, project: str, zone: str, vm_name: str) -> str:
    """
    Detect OS type by fetching VM details from GCP.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM instance name

    Returns:
        'windows' or 'linux'
    """
    vm = compute.instances().get(
        project=project,
        zone=zone,
        instance=vm_name
    ).execute()

    return detect_os_type(vm)


def detect_os_flavor(vm: Dict[str, Any]) -> str:
    """Detect specific OS flavor from disk licenses or source image.

    Parses the boot disk's license URLs (e.g.,
    projects/debian-cloud/global/licenses/debian-12) or falls back
    to the source image name.

    Args:
        vm: VM instance response from GCP API

    Returns:
        OS flavor string like 'debian-12', 'ubuntu-2204-lts',
        'rhel-9', 'windows-server-2022-dc', or 'unknown'.
    """
    for disk in vm.get('disks', []):
        if not disk.get('boot'):
            continue

        # Method 1: Parse license URL (most reliable)
        for license_url in disk.get('licenses', []):
            # Format: projects/PROJECT/global/licenses/LICENSE_NAME
            parts = license_url.rstrip('/').split('/')
            if parts:
                return parts[-1]

        # Method 2: Parse source image name
        source = disk.get('source', '')
        if source:
            # Format: projects/PROJECT/zones/ZONE/disks/DISK_NAME
            # The disk name often contains the image family
            image_name = source.rstrip('/').split('/')[-1]
            if image_name:
                return image_name

    return 'unknown'


def detect_license_type(vm: Dict[str, Any]) -> str:
    """Detect boot disk license type from license URL project.

    Parses the license project from the boot disk's license URL
    to determine if the OS is free, PAYG, or BYOS.

    Args:
        vm: VM instance response from GCP API

    Returns:
        'free', 'payg', 'byos', or 'unknown'.
    """
    for disk in vm.get('disks', []):
        if not disk.get('boot'):
            continue

        for license_url in disk.get('licenses', []):
            # Check BYOS/BYOL first (in the license name)
            license_name = license_url.rstrip('/').split('/')[-1].lower()
            if 'byos' in license_name or 'byol' in license_name:
                return LICENSE_BYOS

            # Extract project from URL
            # Format: .../projects/{PROJECT}/global/licenses/{NAME}
            parts = license_url.split('/')
            if 'projects' in parts:
                project = parts[parts.index('projects') + 1]
                if project in _PAYG_LICENSE_PROJECTS:
                    return LICENSE_PAYG

        return LICENSE_FREE

    return 'unknown'


def detect_architecture(vm: Dict[str, Any]) -> str:
    """
    Detect the CPU architecture of a VM.

    Args:
        vm: VM instance response from GCP API

    Returns:
        'x86_64' or 'arm64'

    Detection methods:
    1. Check boot disk 'architecture' field (most reliable)
    2. Check machine type for T2A family (ARM64)
    """
    # Method 1: Check boot disk architecture field
    for disk in vm.get('disks', []):
        if disk.get('boot'):
            arch = disk.get('architecture', '').upper()
            if arch == 'ARM64':
                return ARCH_ARM64
            elif arch == 'X86_64':
                return ARCH_X86_64

    # Method 2: Check machine type (T2A = ARM64)
    machine_type = vm.get('machineType', '').lower()
    # Machine type format: zones/ZONE/machineTypes/TYPE or just TYPE
    machine_type_name = machine_type.split('/')[-1] if '/' in machine_type else machine_type
    if machine_type_name.startswith('t2a-'):
        return ARCH_ARM64

    # Default to x86_64
    return ARCH_X86_64


def is_shielded_vm(vm: Dict[str, Any]) -> bool:
    """
    Check if VM has Shielded VM features enabled (Secure Boot).

    Shielded VMs with Secure Boot enabled cannot boot unsigned rescue disks.

    Args:
        vm: VM instance response from GCP API

    Returns:
        True if Secure Boot is enabled
    """
    shielded_config = vm.get('shieldedInstanceConfig', {})
    return shielded_config.get('enableSecureBoot', False)


def get_shielded_config(vm: Dict[str, Any]) -> Dict[str, bool]:
    """
    Get full Shielded VM configuration.

    Args:
        vm: VM instance response from GCP API

    Returns:
        Dict with secure_boot, vtpm, and integrity_monitoring settings
    """
    shielded_config = vm.get('shieldedInstanceConfig', {})
    return {
        'secure_boot': shielded_config.get('enableSecureBoot', False),
        'vtpm': shielded_config.get('enableVtpm', False),
        'integrity_monitoring': shielded_config.get('enableIntegrityMonitoring', False)
    }


def is_confidential_vm(vm: Dict[str, Any]) -> bool:
    """
    Check if VM is a Confidential VM.

    Confidential VMs use memory encryption (AMD SEV/Intel TDX) and cannot
    be rescued because the disk contents cannot be accessed externally.

    Args:
        vm: VM instance response from GCP API

    Returns:
        True if Confidential Computing is enabled
    """
    confidential_config = vm.get('confidentialInstanceConfig', {})
    return confidential_config.get('enableConfidentialCompute', False)


def get_confidential_type(vm: Dict[str, Any]) -> str:
    """
    Get the type of Confidential Computing technology.

    Args:
        vm: VM instance response from GCP API

    Returns:
        Type string (SEV, SEV_SNP, TDX) or empty string if not confidential
    """
    confidential_config = vm.get('confidentialInstanceConfig', {})
    if not confidential_config.get('enableConfidentialCompute', False):
        return ''
    return confidential_config.get('confidentialInstanceType', 'SEV')
