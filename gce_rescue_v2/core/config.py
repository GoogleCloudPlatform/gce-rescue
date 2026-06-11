"""
GCE Rescue - Configuration Management

This module manages configuration options for GCE Rescue operations.
"""

from dataclasses import dataclass, field
from typing import Optional

# Version for usage tracking (SemVer: MAJOR.MINOR.PATCH-PRERELEASE)
VERSION = '2.1.0'


def _sanitize_ua_value(value: str) -> str:
    """Sanitize a value for use in User-Agent string."""
    return value.replace(' ', '-').replace('/', '-').replace(',', '')


def build_user_agent(
    session_id: str = None,
    command: str = None,
    os_type: str = None,
    arch: str = None,
    flavor: str = None,
    mode: str = None,
    step: str = None,
) -> str:
    """Build gcloud-style User-Agent string for analytics tracking.

    Format: gce-rescue/{VERSION} session/{UUID} command/{CMD} os/{OS}
            arch/{ARCH} flavor/{FLAVOR} mode/{MODE} step/{STEP}

    Fields are omitted when not yet known (e.g., os/arch/flavor during
    the validation phase before OS detection has run).

    Args:
        session_id: 12-char hex UUID for session correlation.
        command: CLI command name (rescue, restore, diagnose, repair).
        os_type: Detected OS type (linux, windows).
        arch: CPU architecture (x86_64, arm64).
        flavor: OS flavor (debian-12, rhel-9, windows-server-2022-dc).
        mode: Execution mode (interactive, auto).
        step: Current operation step (e.g., vm-stop, disk-detach-orig).

    Returns:
        Space-separated User-Agent string.
    """
    parts = [f'gce-rescue/{VERSION}']
    if session_id:
        parts.append(f'session/{session_id}')
    if command:
        parts.append(f'command/{command}')
    if os_type:
        parts.append(f'os/{os_type}')
    if arch:
        parts.append(f'arch/{arch}')
    if flavor:
        parts.append(f'flavor/{_sanitize_ua_value(flavor)}')
    if mode:
        parts.append(f'mode/{mode}')
    if step:
        parts.append(f'step/{step}')
    return ' '.join(parts)

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
    rescue_disk_type: str = 'pd-balanced'

    # Custom rescue image (overrides all auto-selection when set)
    # Accepts full image URL: projects/PROJECT/global/images/IMAGE
    # or family URL: projects/PROJECT/global/images/family/FAMILY
    custom_rescue_image: Optional[str] = None
    # Disk size required by custom_rescue_image, pre-resolved by CLI to avoid
    # a redundant API lookup in the orchestrator. If None, orchestrator looks it up.
    custom_rescue_image_size_gb: Optional[int] = None

    # Custom fix script (from --fix-script). Holds the script CONTENT, not the
    # path: the CLI reads and validates the file, then stores its text here so
    # the orchestrator stays I/O-free. When set, repair skips diagnosis and runs
    # this script against the affected disk after mount.
    fix_script: Optional[str] = None

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
