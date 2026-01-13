"""
GCE Rescue V2 - gcloud-compatible Command Line Interface

Follows gcloud conventions for future integration into gcloud SDK.

Future command structure:
    gcloud compute instances rescue <instance-name> --zone=<zone>
    gcloud compute instances restore <instance-name> --zone=<zone>

Current standalone usage:
    gce-rescue-v2 rescue <instance-name> --zone=<zone>
    gce-rescue-v2 restore <instance-name> --zone=<zone>
"""

import argparse
import sys
import os
import json
import yaml
from typing import Optional, Dict, Any
from .core.config import RescueConfig, RestoreConfig, VERSION
from .main import rescue_vm, restore_vm
from .utils.colors import error_prefix, warning_prefix, clear_lines


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
        """Format as table (ASCII-safe for Windows compatibility)."""
        lines = []
        lines.append("+-" + "-" * 50 + "-+")
        for key, value in data.items():
            lines.append(f"| {key:20} | {str(value):27} |")
        lines.append("+-" + "-" * 50 + "-+")
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
        import platform

        # On Windows, gcloud is a batch file, need shell=True
        use_shell = platform.system() == 'Windows'
        result = subprocess.run(
            ['gcloud', 'config', 'get-value', key],
            capture_output=True,
            text=True,
            timeout=5,
            shell=use_shell
        )
        value = result.stdout.strip()
        return value if value and value != '(unset)' else None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # gcloud not available or error
        return None


class CustomArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser with cleaner error messages."""

    # Flags specific to each command (for helpful error messages)
    RESCUE_ONLY_FLAGS = ['--snapshot', '--no-snapshot']
    RESTORE_ONLY_FLAGS = ['--keep-rescue-disk']

    def error(self, message: str):
        """Override to provide cleaner error format."""
        import re

        lines = []

        if "invalid choice:" in message:
            # Check if it's an invalid subcommand or invalid flag value
            # Subcommand: "argument command: invalid choice: 'restor' (choose from rescue, restore)"
            # Flag value: "argument --format: invalid choice: 'invalid' (choose from 'json', ...)"
            if "argument command:" in message:
                # Invalid subcommand
                match = re.search(r"invalid choice: '(\w+)' \(choose from (.+)\)", message)
                if match:
                    invalid = match.group(1)
                    lines.append(f"Invalid command '{invalid}'.")
                    lines.append("")
                    lines.append("Usage: gce-rescue-v2 COMMAND VM_NAME --zone=ZONE [OPTIONS]")
                    lines.append("")
                    lines.append("Available commands:")
                    lines.append("  rescue   Boot a VM into rescue mode")
                    lines.append("  restore  Restore a VM from rescue mode")
                else:
                    lines.append(f"{message}")
            else:
                # Invalid value for a flag (e.g., --format=invalid)
                flag_match = re.search(r"argument (--[\w-]+):", message)
                value_match = re.search(r"invalid choice: '(\w+)' \(choose from (.+)\)", message)
                if flag_match and value_match:
                    flag_name = flag_match.group(1)
                    invalid_value = value_match.group(1)
                    # Clean up valid options (remove quotes)
                    valid_options = value_match.group(2).replace("'", "")
                    lines.append(f"Invalid value '{invalid_value}' for {flag_name}.")
                    lines.append("")
                    lines.append("Valid options:")
                    for opt in valid_options.split(", "):
                        lines.append(f"  {opt.strip()}")
                else:
                    lines.append(f"{message}")
        elif "unrecognized arguments:" in message.lower():
            # Extract the unrecognized argument
            match = re.search(r"unrecognized arguments: (.+)", message, re.IGNORECASE)
            if match:
                unrecognized = match.group(1).strip()
                lines.append(f"Unrecognized argument: {unrecognized}")

                # Check if it's a flag from another command
                for flag in self.RESCUE_ONLY_FLAGS:
                    if flag in unrecognized:
                        lines.append("")
                        lines.append(f"Note: '{flag}' is only available for the 'rescue' command.")
                        lines.append("")
                        lines.append("Example:")
                        lines.append(f"  $ gce-rescue-v2 rescue VM_NAME --zone=ZONE {flag}")
                        break
                for flag in self.RESTORE_ONLY_FLAGS:
                    if flag in unrecognized:
                        lines.append("")
                        lines.append(f"Note: '{flag}' is only available for the 'restore' command.")
                        lines.append("")
                        lines.append("Example:")
                        lines.append(f"  $ gce-rescue-v2 restore VM_NAME --zone=ZONE {flag}")
                        break
            else:
                lines.append(f"{message.capitalize()}")
        elif "required: command" in message.lower():
            # Not an error - user just wants to know how to use the tool
            usage_lines = [
                "Usage: gce-rescue-v2 COMMAND VM_NAME --zone=ZONE [OPTIONS]",
                "",
                "Commands:",
                "  rescue   Boot a VM into rescue mode",
                "  restore  Restore a VM from rescue mode",
                "",
                "Examples:",
                "  $ gce-rescue-v2 rescue my-vm --zone=us-central1-a",
                "  $ gce-rescue-v2 restore my-vm --zone=us-central1-a",
                "",
                "For detailed help:",
                "  $ gce-rescue-v2 --help",
                ""
            ]
            self.exit(0, "\n".join(usage_lines) + "\n")
        elif "required:" in message.lower():
            # Extract what's required
            match = re.search(r"required: (.+)", message.lower())
            if match:
                required = match.group(1)
                lines.append(f"Missing required argument: {required}")
                lines.append("")
                lines.append("Usage: gce-rescue-v2 COMMAND VM_NAME --zone=ZONE [OPTIONS]")
                lines.append("")
                lines.append("Example:")
                lines.append("  $ gce-rescue-v2 rescue my-vm --zone=us-central1-a")
            else:
                lines.append(f"{message.capitalize()}")
        else:
            lines.append(f"{message.capitalize()}")

        lines.append("")
        lines.append("For help, run:")
        lines.append("  $ gce-rescue-v2 --help")

        self.exit(2, f"{error_prefix()} " + "\n".join(lines) + "\n\n")


def create_parser() -> CustomArgumentParser:
    """
    Create argument parser with gcloud-compatible structure.

    Returns:
        Configured ArgumentParser
    """

    # Main parser
    parser = CustomArgumentParser(
        prog='gce-rescue-v2',
        description='Google Compute Engine VM Rescue Tool (Beta)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Rescue a VM:
        $ gce-rescue-v2 rescue my-vm --zone=us-central1-a

    Rescue without snapshot (faster):
        $ gce-rescue-v2 rescue my-vm --zone=us-central1-a --no-snapshot

    Restore a VM:
        $ gce-rescue-v2 restore my-vm --zone=us-central1-a

    Automation (no prompts):
        $ gce-rescue-v2 rescue my-vm --zone=us-central1-a --quiet

SUPPORTED OS
    - Linux (auto-detected): Boots Debian 12 rescue environment
    - Windows (auto-detected): Boots Windows Server 2022 rescue environment

For more information: https://github.com/GoogleCloudPlatform/gce-rescue
        """
    )

    # Global flags (gcloud standard)
    parser.add_argument(
        '--version',
        action='version',
        version=f'gce-rescue-v2 {VERSION}'
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
        description='Boot a VM into rescue mode. Automatically detects OS (Linux/Windows) and uses appropriate rescue environment.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Rescue a VM:
        $ gce-rescue-v2 rescue my-vm --zone=us-central1-a

    Rescue without snapshot (faster but riskier):
        $ gce-rescue-v2 rescue my-vm --zone=us-central1-a --no-snapshot

AFTER RESCUE
    Linux VMs:
        $ gcloud compute ssh my-vm --zone=us-central1-a
        Affected disk mounted at: /mnt/sysroot

    Windows VMs:
        Connect via RDP using credentials shown after rescue
        Affected disk mounted at: D:\\ (or next available drive)

TO EXIT RESCUE MODE
    $ gce-rescue-v2 restore my-vm --zone=us-central1-a
        """
    )

    _add_common_args(rescue_parser)
    _add_rescue_args(rescue_parser)

    # RESTORE COMMAND
    restore_parser = subparsers.add_parser(
        'restore',
        help='Restore a VM from rescue mode',
        description='Restore a VM to normal operation by re-attaching your affected boot disk.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Restore a VM:
        $ gce-rescue-v2 restore my-vm --zone=us-central1-a

    Restore and keep rescue disk (for analysis):
        $ gce-rescue-v2 restore my-vm --zone=us-central1-a --keep-rescue-disk

NOTES
    The rescue disk is deleted by default after restore.
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

    # Output flags
    output = parser.add_argument_group('OUTPUT FLAGS')
    output.add_argument(
        '--format',
        metavar='FORMAT',
        choices=['json', 'yaml', 'table', 'disable'],
        default='disable',
        help='Output format: json, yaml, table, disable. Default: disable'
    )
    output.add_argument(
        '--verbosity',
        metavar='VERBOSITY',
        choices=['debug', 'info', 'warning', 'error'],
        default='info',
        help='Logging verbosity: debug, info, warning, error. Default: info'
    )

    # Interactive flags
    interactive = parser.add_argument_group('OTHER FLAGS')
    interactive.add_argument(
        '--quiet',
        action='store_true',
        help='Disable interactive prompts (for automation)'
    )
    interactive.add_argument(
        '--force',
        action='store_true',
        help='Required with --quiet if VM has Local SSDs (data on Local SSDs will be LOST)'
    )


def _add_rescue_args(parser: argparse.ArgumentParser):
    """Add rescue-specific arguments."""

    # Snapshot flags (safety feature) - the only rescue-specific option for beta
    snapshot_group = parser.add_argument_group('SNAPSHOT FLAGS')
    snapshot_group.add_argument(
        '--snapshot',
        action='store_true',
        default=True,
        help='Create snapshot of boot disk before rescue (default: enabled)'
    )
    snapshot_group.add_argument(
        '--no-snapshot',
        dest='snapshot',
        action='store_false',
        help='Skip snapshot creation (faster but riskier)'
    )


def _add_restore_args(parser: argparse.ArgumentParser):
    """Add restore-specific arguments."""

    # Restore configuration - only essential option for beta
    restore_group = parser.add_argument_group('RESTORE FLAGS')
    restore_group.add_argument(
        '--keep-rescue-disk',
        action='store_true',
        help='Keep rescue disk after restore instead of deleting it'
    )


def validate_args(args: argparse.Namespace) -> bool:
    """
    Validate arguments.

    Args:
        args: Parsed arguments

    Returns:
        True if valid, False with error message
    """
    # For beta, minimal validation - all flags have safe defaults
    return True


def args_to_rescue_config(args: argparse.Namespace) -> RescueConfig:
    """Convert arguments to RescueConfig."""
    config = RescueConfig()

    # Snapshot setting (only configurable rescue option in beta)
    if hasattr(args, 'snapshot'):
        config.create_snapshot = args.snapshot

    # Force setting (for Local SSD VMs)
    if hasattr(args, 'force'):
        config.force = args.force

    # Verbosity to log level
    verbosity_map = {
        'debug': 'DEBUG',
        'info': 'INFO',
        'warning': 'WARNING',
        'error': 'ERROR'
    }
    config.log_level = verbosity_map.get(args.verbosity, 'INFO')

    return config


def args_to_restore_config(args: argparse.Namespace) -> RestoreConfig:
    """Convert arguments to RestoreConfig."""
    config = RestoreConfig()

    # Keep rescue disk setting (only configurable restore option in beta)
    config.delete_rescue_disk = not args.keep_rescue_disk

    # Force setting (for Local SSD VMs)
    if hasattr(args, 'force'):
        config.force = args.force

    # Verbosity to log level
    verbosity_map = {
        'debug': 'DEBUG',
        'info': 'INFO',
        'warning': 'WARNING',
        'error': 'ERROR'
    }
    config.log_level = verbosity_map.get(args.verbosity, 'INFO')

    return config


def _parse_api_error(e: Exception, vm_name: str, zone: str, project: str = None) -> str:
    """Parse GCP API error and return user-friendly message."""
    error_str = str(e)

    if 'was not found' in error_str or 'notFound' in error_str:
        list_cmd = f"gcloud compute instances list --project={project}" if project else "gcloud compute instances list"
        lines = [
            f"Instance [{vm_name}] not found.",
            f"  Zone: {zone}",
        ]
        if project:
            lines.append(f"  Project: {project}")
        lines.append("")
        lines.append("To see available instances, run:")
        lines.append(f"  $ {list_cmd}")
        lines.append("")
        return "\n".join(lines)

    if 'Unknown zone' in error_str or ("Invalid value for field" in error_str and "'zone'" in error_str):
        lines = [
            f"Invalid zone '{zone}'.",
            "",
            "To see available zones, run:",
            "  $ gcloud compute zones list",
            ""
        ]
        return "\n".join(lines)

    if 'forbidden' in error_str.lower() or 'permission' in error_str.lower() or '403' in error_str:
        lines = [
            "Permission denied.",
        ]
        if project:
            lines.append(f"  Project: {project}")
        lines.append("")
        lines.append("To verify project access, run:")
        lines.append("  $ gcloud projects list")
        lines.append("")
        return "\n".join(lines)

    if 'Invalid value for field' in error_str:
        lines = [
            "Invalid request parameters.",
            "",
            "Verify the following are correct:",
            f"  Instance: {vm_name}",
            f"  Zone: {zone}",
        ]
        if project:
            lines.append(f"  Project: {project}")
        lines.append("")
        return "\n".join(lines)

    # Fallback: return simplified error
    return f"API error: {error_str[:200]}\n"


def _validate_vm_exists(compute, project: str, zone: str, vm_name: str) -> tuple:
    """
    Validate VM exists and is in a valid state for rescue.

    Returns:
        (success: bool, vm_info: dict or None, error_message: str or None)
    """
    try:
        vm = compute.instances().get(
            project=project,
            zone=zone,
            instance=vm_name
        ).execute()

        # Check if already in rescue mode
        metadata = vm.get('metadata', {}).get('items', [])
        for item in metadata:
            if item.get('key') == 'rescue-mode':
                lines = [
                    f"Instance [{vm_name}] is already in rescue mode.",
                    "",
                    "To exit rescue mode and restore the VM, run:",
                    f"  $ gce-rescue-v2 restore {vm_name} --zone={zone} --project={project}",
                    ""
                ]
                return (False, None, "\n".join(lines))

        # Check VM state
        status = vm.get('status', 'UNKNOWN')
        invalid_states = ['STAGING', 'PROVISIONING', 'SUSPENDING', 'SUSPENDED', 'REPAIRING']
        if status in invalid_states:
            lines = [
                f"Instance [{vm_name}] is in state '{status}'.",
                "",
                "The VM must be in RUNNING or TERMINATED state to rescue.",
                "",
                "To check the current VM status, run:",
                f"  $ gcloud compute instances describe {vm_name} --zone={zone} --project={project} --format='value(status)'",
                ""
            ]
            return (False, None, "\n".join(lines))

        return (True, vm, None)

    except Exception as e:
        return (False, None, _parse_api_error(e, vm_name, zone, project))


def _check_local_ssds(vm_info: dict) -> list:
    """Check if VM has Local SSDs attached. Returns list of Local SSD names."""
    if not vm_info:
        return []

    local_ssds = []
    for disk in vm_info.get('disks', []):
        if disk.get('type') == 'SCRATCH':
            local_ssds.append(disk.get('deviceName', 'unknown'))
    return local_ssds


def _validate_vm_for_restore(compute, project: str, zone: str, vm_name: str) -> tuple:
    """
    Validate VM exists and is in rescue mode for restore.

    Returns:
        (success: bool, vm_info: dict or None, error_message: str or None)
    """
    try:
        vm = compute.instances().get(
            project=project,
            zone=zone,
            instance=vm_name
        ).execute()

        # Check if in rescue mode
        metadata = vm.get('metadata', {}).get('items', [])
        in_rescue_mode = False
        for item in metadata:
            if item.get('key') == 'rescue-mode':
                in_rescue_mode = True
                break

        if not in_rescue_mode:
            lines = [
                f"Instance [{vm_name}] is not in rescue mode.",
                "",
                "To put the VM into rescue mode first, run:",
                f"  $ gce-rescue-v2 rescue {vm_name} --zone={zone} --project={project}",
                ""
            ]
            return (False, None, "\n".join(lines))

        return (True, vm, None)

    except Exception as e:
        return (False, None, _parse_api_error(e, vm_name, zone, project))


def handle_rescue(args: argparse.Namespace) -> int:
    """Handle rescue command."""
    from .core.auth import AuthManager

    # Get project from args or gcloud config
    project = args.project or get_gcloud_config('core/project')

    # Validate project is set
    if not project:
        print(f"{error_prefix()} No project specified.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Please specify a project using one of these methods:", file=sys.stderr)
        print("  1. --project=PROJECT_ID flag", file=sys.stderr)
        print("  2. gcloud config set project PROJECT_ID", file=sys.stderr)
        print("  3. Set CLOUDSDK_CORE_PROJECT environment variable", file=sys.stderr)
        return 1

    # Get compute client for API calls
    try:
        auth = AuthManager()
        compute, project = auth.get_client(project)
    except Exception as e:
        print(f"{error_prefix()} Authentication failed: {e}", file=sys.stderr)
        return 1

    # Validate VM exists and state BEFORE confirmation
    valid, vm_info, error_msg = _validate_vm_exists(compute, project, args.zone, args.instance_name)
    if not valid:
        print(f"{error_prefix()} {error_msg}", file=sys.stderr)
        return 1

    # Check for Local SSDs using validated VM info
    local_ssds = _check_local_ssds(vm_info)
    has_local_ssd = len(local_ssds) > 0

    # In quiet mode with Local SSDs, require --force
    if args.quiet and has_local_ssd and not args.force:
        print(f"{error_prefix()} VM has Local SSDs attached.", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"Local SSDs found: {', '.join(local_ssds)}", file=sys.stderr)
        print("", file=sys.stderr)
        print("WARNING: Stopping this VM will PERMANENTLY DELETE all data on Local SSDs!", file=sys.stderr)
        print("", file=sys.stderr)
        print("To proceed in quiet mode, use --force flag:", file=sys.stderr)
        print(f"  gce-rescue-v2 rescue {args.instance_name} --zone={args.zone} --quiet --force", file=sys.stderr)
        return 1

    # Interactive confirmation (unless --quiet)
    if not args.quiet:
        # Count lines for clearing after confirmation
        lines_printed = 0

        print(f"\nYou are about to rescue instance [{args.instance_name}] in zone [{args.zone}].")
        lines_printed += 2  # includes leading newline
        print("")
        lines_printed += 1
        print("The following actions will be performed:")
        lines_printed += 1
        print(f" - Stop instance [{args.instance_name}].")
        lines_printed += 1
        print(" - Create a snapshot of your affected boot disk.")
        lines_printed += 1
        print(" - Create a rescue disk and boot from it.")
        lines_printed += 1
        print(" - Attach your affected boot disk for repair.")
        lines_printed += 1

        # Show Local SSD warning if applicable
        if has_local_ssd:
            print(f" - {warning_prefix()} Local SSDs ({', '.join(local_ssds)}) will be permanently deleted.")
            lines_printed += 1

        print("")
        lines_printed += 1
        response = input("Do you want to continue (Y/n)? ").strip().lower()
        lines_printed += 1  # The input line

        if response not in ('y', 'yes', ''):
            print("\nAborted by user.")
            return 0

        # Clear confirmation message after user confirms
        clear_lines(lines_printed)

    # Convert to config
    config = args_to_rescue_config(args)

    # If Local SSDs present, set force=True since user confirmed
    if has_local_ssd:
        config.force = True

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
        print(OutputFormatter.format_output(result, args.format))

    return 0 if success else 1


def handle_restore(args: argparse.Namespace) -> int:
    """Handle restore command."""
    from .core.auth import AuthManager

    # Get project from args or gcloud config
    project = args.project or get_gcloud_config('core/project')

    # Validate project is set
    if not project:
        print(f"{error_prefix()} No project specified.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Please specify a project using one of these methods:", file=sys.stderr)
        print("  1. --project=PROJECT_ID flag", file=sys.stderr)
        print("  2. gcloud config set project PROJECT_ID", file=sys.stderr)
        print("  3. Set CLOUDSDK_CORE_PROJECT environment variable", file=sys.stderr)
        return 1

    # Get compute client for API calls
    try:
        auth = AuthManager()
        compute, project = auth.get_client(project)
    except Exception as e:
        print(f"{error_prefix()} Authentication failed: {e}", file=sys.stderr)
        return 1

    # Validate VM exists and is in rescue mode BEFORE confirmation
    valid, vm_info, error_msg = _validate_vm_for_restore(compute, project, args.zone, args.instance_name)
    if not valid:
        print(f"{error_prefix()} {error_msg}", file=sys.stderr)
        return 1

    # Interactive confirmation (unless --quiet)
    if not args.quiet:
        # Count lines for clearing after confirmation
        lines_printed = 0

        print(f"\nYou are about to restore instance [{args.instance_name}] in zone [{args.zone}] project [{project}].")
        lines_printed += 2  # includes leading newline
        print("")
        lines_printed += 1
        print("The following actions will be performed:")
        lines_printed += 1
        print(f" - Stop instance [{args.instance_name}].")
        lines_printed += 1
        print(" - Delete the rescue disk.")
        lines_printed += 1
        print(" - Restore your affected boot disk as the primary boot device.")
        lines_printed += 1
        print(f" - Start instance [{args.instance_name}].")
        lines_printed += 1
        print("")
        lines_printed += 1
        response = input("Do you want to continue (Y/n)? ").strip().lower()
        lines_printed += 1  # The input line

        if response not in ('y', 'yes', ''):
            print("\nAborted by user.")
            return 0

        # Clear confirmation message after user confirms
        clear_lines(lines_printed)

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
            print(f"{error_prefix()} (gce-rescue) Unknown command: {args.command}", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.", file=sys.stderr)
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"{error_prefix()} (gce-rescue) Unexpected error: {str(e)}", file=sys.stderr)
        if '--verbosity=debug' in sys.argv or '--verbosity debug' in sys.argv:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
