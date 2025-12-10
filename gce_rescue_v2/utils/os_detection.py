"""
GCE Rescue - OS Detection Utility

Detects whether a VM is running Windows or Linux based on:
1. Guest OS features (most reliable)
2. Boot disk source image
3. License information
"""

from typing import Dict, Any
from ..core.config import OS_TYPE_LINUX, OS_TYPE_WINDOWS


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
