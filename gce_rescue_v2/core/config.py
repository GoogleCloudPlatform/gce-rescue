"""
GCE Rescue - Configuration Management

This module manages configuration options for GCE Rescue operations.
"""

from dataclasses import dataclass, field
from typing import Optional

# Version for usage tracking (SemVer: MAJOR.MINOR.PATCH-PRERELEASE)
VERSION = '2.0.0-beta.4'

# OS Types
OS_TYPE_LINUX = 'linux'
OS_TYPE_WINDOWS = 'windows'


@dataclass
class RescueConfig:
    """
    Configuration for rescue operations.

    This stores all options that can be customized for a rescue operation.
    Makes it easy to pass configuration around without many parameters.

    Example:
        config = RescueConfig(
            rescue_disk_size_gb=20,
            create_snapshot=True
        )
    """

    # Rescue disk settings
    rescue_disk_size_gb: int = 10
    rescue_disk_type: str = 'pd-standard'

    # Linux rescue image (default)
    rescue_image_family: str = 'debian-12'  # Use newer kernel for XFS/Btrfs compatibility
    rescue_image_project: str = 'debian-cloud'

    # Windows rescue image (auto-selected for Windows VMs)
    windows_rescue_image_family: str = 'windows-2022'
    windows_rescue_image_project: str = 'windows-cloud'
    windows_rescue_disk_size_gb: int = 50  # Windows needs more space

    # ARM64 Linux rescue image (auto-selected for ARM64/T2A VMs)
    arm64_rescue_image_family: str = 'debian-12-arm64'
    arm64_rescue_image_project: str = 'debian-cloud'

    # Snapshot settings (safety feature)
    create_snapshot: bool = True  # DEFAULT: Create snapshot for safety
    require_snapshot: bool = False  # Abort if snapshot creation fails
    async_snapshot: bool = True  # DEFAULT: Async for speed (snapshot created after disk detach)
    snapshot_name_prefix: str = 'pre-rescue'
    snapshot_timeout: int = 600  # 10 minutes for snapshot creation

    # Timeout settings (in seconds)
    vm_stop_timeout: int = 300  # 5 minutes
    vm_start_timeout: int = 300  # 5 minutes
    disk_create_timeout: int = 300  # 5 minutes
    operation_timeout: int = 600  # 10 minutes
    startup_verification_timeout: int = 120  # 2 minutes for startup script completion

    # Logging settings
    log_level: str = 'INFO'
    log_file: Optional[str] = None

    # Behavior settings
    dry_run: bool = False  # Show what would happen without doing it
    interactive: bool = False  # Ask for confirmation
    auto_rollback: bool = True  # Automatically rollback on failure

    # Advanced settings
    preserve_rescue_disk: bool = False  # Keep rescue disk after restore
    skip_health_check: bool = False  # Skip health checks
    force: bool = False  # Force operation (e.g., stop VM with Local SSDs)


@dataclass
class RestoreConfig:
    """
    Configuration for restore operations.

    Example:
        config = RestoreConfig(
            delete_rescue_disk=True
        )
    """

    # Restore settings
    delete_rescue_disk: bool = True
    create_rescue_snapshot: bool = False  # Snapshot rescue disk before deleting

    # Timeout settings (in seconds)
    vm_stop_timeout: int = 300
    vm_start_timeout: int = 300
    operation_timeout: int = 600

    # Logging settings
    log_level: str = 'INFO'
    log_file: Optional[str] = None

    # Behavior settings
    dry_run: bool = False
    interactive: bool = False
    auto_rollback: bool = True
    skip_health_check: bool = False
    force: bool = False  # Force operation (e.g., stop VM with Local SSDs)


# Default configurations
DEFAULT_RESCUE_CONFIG = RescueConfig()
DEFAULT_RESTORE_CONFIG = RestoreConfig()


def create_rescue_config(**kwargs) -> RescueConfig:
    """
    Create a rescue configuration with custom options.

    Args:
        **kwargs: Configuration options (any field from RescueConfig)

    Returns:
        RescueConfig: Configuration object

    Example:
        config = create_rescue_config(
            rescue_disk_size_gb=20,
            create_snapshot=True,
            log_level='DEBUG'
        )
    """
    return RescueConfig(**kwargs)


def create_restore_config(**kwargs) -> RestoreConfig:
    """
    Create a restore configuration with custom options.

    Args:
        **kwargs: Configuration options (any field from RestoreConfig)

    Returns:
        RestoreConfig: Configuration object

    Example:
        config = create_restore_config(
            delete_rescue_disk=False,
            log_level='DEBUG'
        )
    """
    return RestoreConfig(**kwargs)
