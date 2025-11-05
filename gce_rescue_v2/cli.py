"""
GCE Rescue V2 - gcloud-compatible Command Line Interface

Follows gcloud conventions for future integration into gcloud SDK.

Future command structure:
    gcloud compute instances rescue <instance-name> --zone=<zone>
    gcloud compute instances restore <instance-name> --zone=<zone>

Current standalone usage:
    gce-rescue rescue <instance-name> --zone=<zone>
    gce-rescue restore <instance-name> --zone=<zone>
"""

import argparse
import sys
import os
import json
import yaml
from typing import Optional, Dict, Any
from core.config import RescueConfig, RestoreConfig, VERSION
from main import rescue_vm, restore_vm


class OutputFormatter:
    """
    Handle output formatting similar to gcloud.

    Supports: json, yaml, table, csv, value
    """

    @staticmethod
    def format_output(data: Dict[str, Any], format_type: str = 'table'):
        """Format output based on format type."""
        if format_type == 'json':
            return json.dumps(data, indent=2)
        elif format_type == 'yaml':
            return yaml.dump(data, default_flow_style=False)
        elif format_type == 'table':
            return OutputFormatter._format_table(data)
        elif format_type == 'csv':
            return OutputFormatter._format_csv(data)
        elif format_type.startswith('value('):
            # Extract specific field: value(vmName)
            field = format_type[6:-1]
            return str(data.get(field, ''))
        else:
            return str(data)

    @staticmethod
    def _format_table(data: Dict[str, Any]) -> str:
        """Format as table."""
        lines = []
        lines.append("┌─" + "─" * 50 + "─┐")
        for key, value in data.items():
            lines.append(f"│ {key:20} │ {str(value):27} │")
        lines.append("└─" + "─" * 50 + "─┘")
        return "\n".join(lines)

    @staticmethod
    def _format_csv(data: Dict[str, Any]) -> str:
        """Format as CSV."""
        keys = ",".join(data.keys())
        values = ",".join(str(v) for v in data.values())
        return f"{keys}\n{values}"


def get_gcloud_config(key: str) -> Optional[str]:
    """
    Read configuration from gcloud config.

    Args:
        key: Config key (e.g., 'core/project', 'compute/zone')

    Returns:
        Config value or None
    """
    try:
        # Try to read from gcloud config
        import subprocess
        result = subprocess.run(
            ['gcloud', 'config', 'get-value', key],
            capture_output=True,
            text=True,
            timeout=5
        )
        value = result.stdout.strip()
        return value if value and value != '(unset)' else None
    except (subprocess.SubprocessError, FileNotFoundError):
        # gcloud not available or error
        return None


def create_parser() -> argparse.ArgumentParser:
    """
    Create argument parser with gcloud-compatible structure.

    Returns:
        Configured ArgumentParser
    """

    # Main parser
    parser = argparse.ArgumentParser(
        prog='gce-rescue',
        description='Google Compute Engine VM Rescue Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    To rescue a VM:
        $ gce-rescue rescue my-vm --zone=us-central1-a

    To rescue with custom disk:
        $ gce-rescue rescue my-vm --zone=us-central1-a \\
            --rescue-disk-size=50 --rescue-disk-type=pd-ssd

    To restore a VM:
        $ gce-rescue restore my-vm --zone=us-central1-a

NOTES
    This tool follows gcloud conventions and will be integrated into
    gcloud SDK in the future as:
        gcloud compute instances rescue <instance-name>

For more information, visit: https://github.com/your-org/gce-rescue
        """
    )

    # Global flags (gcloud standard)
    parser.add_argument(
        '--version',
        action='version',
        version=f'gce-rescue v{VERSION}'
    )

    # Subcommands
    subparsers = parser.add_subparsers(
        dest='command',
        required=True,
        help='Available commands'
    )

    # RESCUE COMMAND
    rescue_parser = subparsers.add_parser(
        'rescue',
        help='Boot a VM into rescue mode',
        description='Boot a VM into rescue mode by creating a rescue disk and attaching the original boot disk as secondary.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    To rescue a VM:
        $ gce-rescue rescue my-vm --zone=us-central1-a

    To rescue with a 50GB SSD disk:
        $ gce-rescue rescue my-vm --zone=us-central1-a \\
            --rescue-disk-size=50 --rescue-disk-type=pd-ssd

    To rescue with snapshot backup:
        $ gce-rescue rescue my-vm --zone=us-central1-a --snapshot

    To use Ubuntu rescue image:
        $ gce-rescue rescue my-vm --zone=us-central1-a \\
            --rescue-image-family=ubuntu-2204-lts \\
            --rescue-image-project=ubuntu-os-cloud

NOTES
    After rescue completes, SSH into the VM:
        $ gcloud compute ssh my-vm --zone=us-central1-a

    Original disk is mounted at /mnt/sysroot

    To exit rescue mode:
        $ gce-rescue restore my-vm --zone=us-central1-a
        """
    )

    _add_common_args(rescue_parser)
    _add_rescue_args(rescue_parser)

    # RESTORE COMMAND
    restore_parser = subparsers.add_parser(
        'restore',
        help='Restore a VM from rescue mode',
        description='Restore a VM to normal operation by re-attaching the original boot disk.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    To restore a VM:
        $ gce-rescue restore my-vm --zone=us-central1-a

    To restore and keep the rescue disk:
        $ gce-rescue restore my-vm --zone=us-central1-a \\
            --keep-rescue-disk

NOTES
    The rescue disk will be deleted by default. Use --keep-rescue-disk
    to retain it for analysis.
        """
    )

    _add_common_args(restore_parser)
    _add_restore_args(restore_parser)

    return parser


def _add_common_args(parser: argparse.ArgumentParser):
    """Add arguments common to all commands (gcloud style)."""

    # Positional arguments
    positional = parser.add_argument_group('POSITIONAL ARGUMENTS')
    positional.add_argument(
        'instance_name',
        metavar='INSTANCE_NAME',
        help='Name of the instance to operate on.'
    )

    # Required flags
    required = parser.add_argument_group('REQUIRED FLAGS')
    required.add_argument(
        '--zone',
        metavar='ZONE',
        required=True,
        help='Zone of the instance. Example: us-central1-a'
    )

    # Optional flags (gcloud standard)
    optional = parser.add_argument_group('OPTIONAL FLAGS')
    optional.add_argument(
        '--project',
        metavar='PROJECT',
        help='GCP project ID. Defaults to gcloud config project.'
    )

    # Output flags (gcloud standard)
    output = parser.add_argument_group('OUTPUT FLAGS')
    output.add_argument(
        '--format',
        metavar='FORMAT',
        choices=['json', 'yaml', 'table', 'csv', 'disable'],
        default='table',
        help='Output format. One of: json, yaml, table, csv, disable. Default: table'
    )
    output.add_argument(
        '--verbosity',
        metavar='VERBOSITY',
        choices=['debug', 'info', 'warning', 'error', 'critical'],
        default='info',
        help='Logging verbosity. One of: debug, info, warning, error, critical. Default: info'
    )
    output.add_argument(
        '--log-file',
        metavar='LOG_FILE',
        help='Write logs to this file.'
    )

    # Interactive flags (gcloud standard)
    interactive = parser.add_argument_group('INTERACTIVE FLAGS')
    interactive.add_argument(
        '--quiet',
        action='store_true',
        help='Disable interactive prompts. Useful for automation.'
    )

    # Other flags
    other = parser.add_argument_group('OTHER FLAGS')
    other.add_argument(
        '--dry-run',
        action='store_true',
        help='Print what would be done without actually doing it.'
    )
    other.add_argument(
        '--no-rollback',
        action='store_true',
        help='Disable automatic rollback on failure. Not recommended.'
    )


def _add_rescue_args(parser: argparse.ArgumentParser):
    """Add rescue-specific arguments."""

    # Rescue disk configuration
    disk_group = parser.add_argument_group('RESCUE DISK FLAGS')
    disk_group.add_argument(
        '--rescue-disk-size',
        type=int,
        metavar='SIZE',
        help='Size of rescue disk in GB. Default: 10'
    )
    disk_group.add_argument(
        '--rescue-disk-type',
        metavar='TYPE',
        choices=['pd-standard', 'pd-ssd', 'pd-balanced'],
        help='Type of rescue disk. One of: pd-standard, pd-ssd, pd-balanced. Default: pd-standard'
    )
    disk_group.add_argument(
        '--rescue-disk-name',
        metavar='NAME',
        help='Custom name for rescue disk. Default: auto-generated'
    )

    # Rescue image configuration
    image_group = parser.add_argument_group('RESCUE IMAGE FLAGS')
    image_group.add_argument(
        '--rescue-image-family',
        metavar='FAMILY',
        help='Image family for rescue disk. Default: debian-11'
    )
    image_group.add_argument(
        '--rescue-image-project',
        metavar='PROJECT',
        help='Project containing rescue image. Default: debian-cloud'
    )

    # Snapshot flags (safety feature)
    snapshot_group = parser.add_argument_group('SNAPSHOT FLAGS (Safety)')
    snapshot_group.add_argument(
        '--snapshot',
        action='store_true',
        default=True,
        help='Create snapshot of boot disk before rescue (default: enabled for safety)'
    )
    snapshot_group.add_argument(
        '--no-snapshot',
        dest='snapshot',
        action='store_false',
        help='Skip snapshot creation (faster but riskier - not recommended)'
    )
    snapshot_group.add_argument(
        '--require-snapshot',
        action='store_true',
        help='Abort if snapshot creation fails (maximum safety mode)'
    )
    snapshot_group.add_argument(
        '--snapshot-name',
        metavar='NAME',
        help='Custom snapshot name (auto-generated if not provided)'
    )

    # Startup script flags
    script_group = parser.add_argument_group('STARTUP SCRIPT FLAGS')
    script_group.add_argument(
        '--startup-script',
        metavar='FILE',
        help='Path to custom startup script.'
    )
    script_group.add_argument(
        '--no-startup-script',
        action='store_true',
        help='Skip startup script (manual mounting).'
    )
    script_group.add_argument(
        '--mount-point',
        metavar='PATH',
        help='Mount point for original disk. Default: /mnt/sysroot'
    )

    # Timeout flags
    timeout_group = parser.add_argument_group('TIMEOUT FLAGS')
    timeout_group.add_argument(
        '--vm-stop-timeout',
        type=int,
        metavar='SECONDS',
        help='Timeout for VM stop in seconds. Default: 300'
    )
    timeout_group.add_argument(
        '--vm-start-timeout',
        type=int,
        metavar='SECONDS',
        help='Timeout for VM start in seconds. Default: 300'
    )
    timeout_group.add_argument(
        '--disk-create-timeout',
        type=int,
        metavar='SECONDS',
        help='Timeout for disk creation in seconds. Default: 600'
    )


def _add_restore_args(parser: argparse.ArgumentParser):
    """Add restore-specific arguments."""

    # Restore configuration
    restore_group = parser.add_argument_group('RESTORE FLAGS')
    restore_group.add_argument(
        '--keep-rescue-disk',
        action='store_true',
        help='Keep rescue disk after restore instead of deleting it.'
    )
    restore_group.add_argument(
        '--rescue-disk-name',
        metavar='NAME',
        help='Name of rescue disk. Auto-detected if not provided.'
    )

    # Timeout flags
    timeout_group = parser.add_argument_group('TIMEOUT FLAGS')
    timeout_group.add_argument(
        '--vm-stop-timeout',
        type=int,
        metavar='SECONDS',
        help='Timeout for VM stop in seconds. Default: 300'
    )
    timeout_group.add_argument(
        '--vm-start-timeout',
        type=int,
        metavar='SECONDS',
        help='Timeout for VM start in seconds. Default: 300'
    )


def validate_args(args: argparse.Namespace) -> bool:
    """
    Validate arguments (gcloud-style validation).

    Args:
        args: Parsed arguments

    Returns:
        True if valid, False with error message
    """

    # Snapshot name requires snapshot
    if hasattr(args, 'snapshot_name') and args.snapshot_name and not args.snapshot:
        print(f"ERROR: (gce-rescue) Invalid flag combination:", file=sys.stderr)
        print(f"  --snapshot-name requires --snapshot", file=sys.stderr)
        return False

    # Startup script must exist
    if hasattr(args, 'startup_script') and args.startup_script:
        if not os.path.exists(args.startup_script):
            print(f"ERROR: (gce-rescue) File not found:", file=sys.stderr)
            print(f"  --startup-script: {args.startup_script}", file=sys.stderr)
            return False

    # Disk size validation
    if hasattr(args, 'rescue_disk_size') and args.rescue_disk_size:
        if args.rescue_disk_size < 10:
            print(f"ERROR: (gce-rescue) Invalid value:", file=sys.stderr)
            print(f"  --rescue-disk-size must be at least 10 GB", file=sys.stderr)
            return False
        if args.rescue_disk_size > 65536:
            print(f"ERROR: (gce-rescue) Invalid value:", file=sys.stderr)
            print(f"  --rescue-disk-size cannot exceed 65536 GB", file=sys.stderr)
            return False

    # Timeout validation
    timeout_flags = ['vm_stop_timeout', 'vm_start_timeout', 'disk_create_timeout']
    for flag in timeout_flags:
        if hasattr(args, flag):
            value = getattr(args, flag)
            if value and value < 10:
                flag_name = flag.replace('_', '-')
                print(f"ERROR: (gce-rescue) Invalid value:", file=sys.stderr)
                print(f"  --{flag_name} must be at least 10 seconds", file=sys.stderr)
                return False

    return True


def args_to_rescue_config(args: argparse.Namespace) -> RescueConfig:
    """Convert arguments to RescueConfig."""
    config = RescueConfig()

    # Disk settings
    if hasattr(args, 'rescue_disk_size') and args.rescue_disk_size:
        config.rescue_disk_size_gb = args.rescue_disk_size
    if hasattr(args, 'rescue_disk_type') and args.rescue_disk_type:
        config.rescue_disk_type = args.rescue_disk_type
    if hasattr(args, 'rescue_disk_name') and args.rescue_disk_name:
        config.rescue_disk_name = args.rescue_disk_name

    # Image settings
    if hasattr(args, 'rescue_image_family') and args.rescue_image_family:
        config.rescue_image_family = args.rescue_image_family
    if hasattr(args, 'rescue_image_project') and args.rescue_image_project:
        config.rescue_image_project = args.rescue_image_project

    # Snapshot settings (default is True, can be disabled with --no-snapshot)
    if hasattr(args, 'snapshot'):
        config.create_snapshot = args.snapshot
    if hasattr(args, 'require_snapshot') and args.require_snapshot:
        config.require_snapshot = True
    if hasattr(args, 'snapshot_name') and args.snapshot_name:
        config.snapshot_name_prefix = args.snapshot_name

    # Startup script
    if hasattr(args, 'startup_script') and args.startup_script:
        with open(args.startup_script, 'r') as f:
            config.startup_script = f.read()
    if hasattr(args, 'no_startup_script') and args.no_startup_script:
        config.startup_script = None
    if hasattr(args, 'mount_point') and args.mount_point:
        config.mount_point = args.mount_point

    # Timeouts
    if hasattr(args, 'vm_stop_timeout') and args.vm_stop_timeout:
        config.vm_stop_timeout = args.vm_stop_timeout
    if hasattr(args, 'vm_start_timeout') and args.vm_start_timeout:
        config.vm_start_timeout = args.vm_start_timeout
    if hasattr(args, 'disk_create_timeout') and args.disk_create_timeout:
        config.disk_create_timeout = args.disk_create_timeout

    # Safety settings
    config.dry_run = args.dry_run
    config.auto_rollback = not args.no_rollback

    # Verbosity to log level
    verbosity_map = {
        'debug': 'DEBUG',
        'info': 'INFO',
        'warning': 'WARNING',
        'error': 'ERROR',
        'critical': 'CRITICAL'
    }
    config.log_level = verbosity_map.get(args.verbosity, 'INFO')

    return config


def args_to_restore_config(args: argparse.Namespace) -> RestoreConfig:
    """Convert arguments to RestoreConfig."""
    config = RestoreConfig()

    # Rescue disk settings
    config.delete_rescue_disk = not args.keep_rescue_disk
    if hasattr(args, 'rescue_disk_name') and args.rescue_disk_name:
        config.rescue_disk_name = args.rescue_disk_name

    # Timeouts
    if hasattr(args, 'vm_stop_timeout') and args.vm_stop_timeout:
        config.vm_stop_timeout = args.vm_stop_timeout
    if hasattr(args, 'vm_start_timeout') and args.vm_start_timeout:
        config.vm_start_timeout = args.vm_start_timeout

    # Safety settings
    config.dry_run = args.dry_run
    config.auto_rollback = not args.no_rollback

    # Verbosity to log level
    verbosity_map = {
        'debug': 'DEBUG',
        'info': 'INFO',
        'warning': 'WARNING',
        'error': 'ERROR',
        'critical': 'CRITICAL'
    }
    config.log_level = verbosity_map.get(args.verbosity, 'INFO')

    return config


def handle_rescue(args: argparse.Namespace) -> int:
    """Handle rescue command."""

    # Get project from args or gcloud config
    project = args.project or get_gcloud_config('core/project')

    # Convert to config
    config = args_to_rescue_config(args)

    # Execute
    debug = args.verbosity == 'debug'
    success = rescue_vm(
        vm_name=args.instance_name,
        zone=args.zone,
        project=project,
        config=config,
        debug=debug
    )

    # Format output
    if args.format != 'disable' and success:
        result = {
            'instanceName': args.instance_name,
            'zone': args.zone,
            'project': project or 'default',
            'status': 'RESCUE_MODE',
            'operation': 'rescue',
            'success': True
        }
        if args.format != 'table':  # table already printed by main
            print(OutputFormatter.format_output(result, args.format))

    return 0 if success else 1


def handle_restore(args: argparse.Namespace) -> int:
    """Handle restore command."""

    # Get project from args or gcloud config
    project = args.project or get_gcloud_config('core/project')

    # Convert to config
    config = args_to_restore_config(args)

    # Execute
    debug = args.verbosity == 'debug'
    success = restore_vm(
        vm_name=args.instance_name,
        zone=args.zone,
        project=project,
        config=config,
        debug=debug
    )

    # Format output
    if args.format != 'disable' and success:
        result = {
            'instanceName': args.instance_name,
            'zone': args.zone,
            'project': project or 'default',
            'status': 'RUNNING',
            'operation': 'restore',
            'success': True
        }
        if args.format != 'table':  # table already printed by main
            print(OutputFormatter.format_output(result, args.format))

    return 0 if success else 1


def main():
    """Main CLI entry point (gcloud-compatible)."""

    try:
        # Parse arguments
        parser = create_parser()
        args = parser.parse_args()

        # Validate
        if not validate_args(args):
            return 1

        # Execute command
        if args.command == 'rescue':
            return handle_rescue(args)
        elif args.command == 'restore':
            return handle_restore(args)
        else:
            print(f"ERROR: (gce-rescue) Unknown command: {args.command}", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.", file=sys.stderr)
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"ERROR: (gce-rescue) Unexpected error: {str(e)}", file=sys.stderr)
        if '--verbosity=debug' in sys.argv or '--verbosity debug' in sys.argv:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
