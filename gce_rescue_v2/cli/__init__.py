"""
GCE Rescue - gcloud-compatible Command Line Interface

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
from ..core.config import RescueConfig, RestoreConfig, VERSION
from ..utils.colors import error_prefix

# Re-export submodule symbols for backward compatibility.
# Tests and external code import these as gce_rescue_v2.cli.<name>.
from .output import OutputFormatter, _Spinner, _format_duration  # noqa: F401
from .preflight import get_gcloud_config, _create_tracked_client  # noqa: F401
from .checkpoint_ui import (  # noqa: F401
    _prompt_incomplete_operation,
    _handle_checkpoint_rollback,
    _handle_restore_checkpoint_rollback,
    _reconcile_rescue_state,
)
from .rescue import handle_rescue  # noqa: F401
from .restore import handle_restore  # noqa: F401
from .diagnose import handle_diagnose  # noqa: F401
from .repair import handle_repair, _show_repair_results, _show_boot_verification  # noqa: F401


class CustomArgumentParser(argparse.ArgumentParser):
    """Custom ArgumentParser with cleaner error messages."""

    # Flags specific to each command (for helpful error messages)
    RESCUE_ONLY_FLAGS = ['--snapshot', '--no-snapshot', '--rescue-image']
    RESTORE_ONLY_FLAGS = ['--keep-rescue-disk']
    REPAIR_ONLY_FLAGS = []  # repair uses same flags as rescue for now

    def error(self, message: str):
        """Override to provide cleaner error format."""
        import re

        lines = []

        if "invalid choice:" in message:
            if "argument command:" in message:
                match = re.search(r"invalid choice: '(\w+)' \(choose from (.+)\)", message)
                if match:
                    invalid = match.group(1)
                    lines.append(f"Invalid command '{invalid}'.")
                    lines.append("")
                    lines.append("Usage: gce-rescue COMMAND VM_NAME --zone=ZONE [OPTIONS]")
                    lines.append("")
                    lines.append("Available commands:")
                    lines.append("  rescue         Boot a VM into rescue mode")
                    lines.append("  restore        Restore a VM from rescue mode")
                    lines.append("  diagnose       Diagnose VM boot issues (read-only)")
                    lines.append("  repair         Diagnose and auto-fix boot issues")
                else:
                    lines.append(f"{message}")
            else:
                flag_match = re.search(r"argument (--[\w-]+):", message)
                value_match = re.search(r"invalid choice: '(\w+)' \(choose from (.+)\)", message)
                if flag_match and value_match:
                    flag_name = flag_match.group(1)
                    invalid_value = value_match.group(1)
                    valid_options = value_match.group(2).replace("'", "")
                    lines.append(f"Invalid value '{invalid_value}' for {flag_name}.")
                    lines.append("")
                    lines.append("Valid options:")
                    for opt in valid_options.split(", "):
                        lines.append(f"  {opt.strip()}")
                else:
                    lines.append(f"{message}")
        elif "unrecognized arguments:" in message.lower():
            match = re.search(r"unrecognized arguments: (.+)", message, re.IGNORECASE)
            if match:
                unrecognized = match.group(1).strip()
                lines.append(f"Unrecognized argument: {unrecognized}")

                for flag in self.RESCUE_ONLY_FLAGS:
                    if flag in unrecognized:
                        lines.append("")
                        lines.append(f"Note: '{flag}' is only available for the"
                                     f" 'rescue' command.")
                        lines.append("")
                        lines.append("Example:")
                        lines.append(f"  $ gce-rescue rescue VM_NAME --zone=ZONE {flag}")
                        break
                for flag in self.RESTORE_ONLY_FLAGS:
                    if flag in unrecognized:
                        lines.append("")
                        lines.append(f"Note: '{flag}' is only available for the"
                                     f" 'restore' command.")
                        lines.append("")
                        lines.append("Example:")
                        lines.append(f"  $ gce-rescue restore VM_NAME --zone=ZONE {flag}")
                        break
            else:
                lines.append(f"{message.capitalize()}")
        elif "required: command" in message.lower():
            usage_lines = [
                "Usage: gce-rescue COMMAND VM_NAME --zone=ZONE [OPTIONS]",
                "",
                "Commands:",
                "  rescue         Boot a VM into rescue mode",
                "  restore        Restore a VM from rescue mode",
                "  diagnose       Diagnose VM boot issues (read-only)",
                "  repair         Diagnose and auto-fix boot issues",
                "",
                "Examples:",
                "  $ gce-rescue rescue my-vm --zone=us-central1-a",
                "  $ gce-rescue restore my-vm --zone=us-central1-a",
                "",
                "For detailed help:",
                "  $ gce-rescue --help",
                "",
                "Looking for V1? It's available as: gce-rescue-v1",
                ""
            ]
            self.exit(0, "\n".join(usage_lines) + "\n")
        elif "required:" in message.lower():
            match = re.search(r"required: (.+)", message.lower())
            if match:
                required = match.group(1)
                lines.append(f"Missing required argument: {required}")
                lines.append("")
                lines.append("Usage: gce-rescue COMMAND VM_NAME --zone=ZONE [OPTIONS]")
                lines.append("")
                lines.append("Example:")
                lines.append("  $ gce-rescue rescue my-vm --zone=us-central1-a")
            else:
                lines.append(f"{message.capitalize()}")
        else:
            lines.append(f"{message.capitalize()}")

        lines.append("")
        lines.append("For help, run:")
        lines.append("  $ gce-rescue --help")

        self.exit(2, f"{error_prefix()} " + "\n".join(lines) + "\n\n")


def create_parser() -> CustomArgumentParser:
    """
    Create argument parser with gcloud-compatible structure.

    Returns:
        Configured ArgumentParser
    """

    # Main parser
    parser = CustomArgumentParser(
        prog='gce-rescue',
        description='Google Compute Engine VM Rescue Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Rescue a VM:
        $ gce-rescue rescue my-vm --zone=us-central1-a

    Rescue without snapshot (faster):
        $ gce-rescue rescue my-vm --zone=us-central1-a --no-snapshot

    Restore a VM:
        $ gce-rescue restore my-vm --zone=us-central1-a

    Automation (no prompts):
        $ gce-rescue rescue my-vm --zone=us-central1-a --quiet

    Diagnose boot issues:
        $ gce-rescue diagnose my-vm --zone=us-central1-a

    Auto-repair boot issues:
        $ gce-rescue repair my-vm --zone=us-central1-a

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
        version=f'gce-rescue {VERSION}'
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
        description='Boot a VM into rescue mode. Automatically detects OS'
                    ' (Linux/Windows) and uses appropriate rescue environment.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Rescue a VM:
        $ gce-rescue rescue my-vm --zone=us-central1-a

    Rescue without snapshot (faster but riskier):
        $ gce-rescue rescue my-vm --zone=us-central1-a --no-snapshot

    Rescue with a custom image:
        $ gce-rescue rescue my-vm --zone=us-central1-a \\
            --rescue-image=projects/my-project/global/images/my-rescue-image

    Rescue with a custom image family:
        $ gce-rescue rescue my-vm --zone=us-central1-a \\
            --rescue-image=projects/debian-cloud/global/images/family/debian-11

AFTER RESCUE
    Linux VMs:
        $ gcloud compute ssh my-vm --zone=us-central1-a
        Affected disk mounted at: /mnt/sysroot

    Windows VMs:
        Connect via RDP using credentials shown after rescue
        Affected disk mounted at: D:\\ (or next available drive)

TO EXIT RESCUE MODE
    $ gce-rescue restore my-vm --zone=us-central1-a
        """
    )

    _add_common_args(rescue_parser)
    _add_rescue_args(rescue_parser)

    # RESTORE COMMAND
    restore_parser = subparsers.add_parser(
        'restore',
        help='Restore a VM from rescue mode',
        description='Restore a VM to normal operation by re-attaching your'
                    ' affected boot disk.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Restore a VM:
        $ gce-rescue restore my-vm --zone=us-central1-a

    Restore and keep rescue disk (for analysis):
        $ gce-rescue restore my-vm --zone=us-central1-a --keep-rescue-disk

NOTES
    The rescue disk is deleted by default after restore.
        """
    )

    _add_common_args(restore_parser)
    _add_restore_args(restore_parser)

    # DIAGNOSE-BOOT COMMAND
    diagnose_parser = subparsers.add_parser(
        'diagnose',
        help='Diagnose VM boot issues (read-only)',
        description='Analyze VM serial console output to detect boot errors.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Diagnose a VM:
        $ gce-rescue diagnose my-vm --zone=us-central1-a

    Diagnose and output as JSON:
        $ gce-rescue diagnose my-vm --zone=us-central1-a --format=json

    Diagnose and output as YAML:
        $ gce-rescue diagnose my-vm --zone=us-central1-a --format=yaml

NOTES
    This is a read-only operation that does not modify the VM.
    It analyzes serial console output for common boot error patterns.
        """
    )

    _add_common_args(diagnose_parser)

    # REPAIR COMMAND
    repair_parser = subparsers.add_parser(
        'repair',
        help='Diagnose and auto-fix boot issues (Linux only)',
        description='Automatically diagnose and repair boot issues. Combines'
                    ' diagnose, rescue (with embedded fix), and restore into a'
                    ' single command.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Repair a VM with boot issues:
        $ gce-rescue repair my-vm --zone=us-central1-a

    Repair without snapshot (faster):
        $ gce-rescue repair my-vm --zone=us-central1-a --no-snapshot

    Repair in automation (no prompts):
        $ gce-rescue repair my-vm --zone=us-central1-a --quiet

SUPPORTED FIXES
    - fstab: Comments out invalid UUID, device, or label entries

NOTES
    This command is Linux-only. For Windows VMs, use 'rescue' for manual fix.
    The VM will be stopped during repair and restarted when complete.
        """
    )

    _add_common_args(repair_parser)
    _add_repair_args(repair_parser)

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
        help='Required with --quiet if VM has Local SSDs'
             ' (data on Local SSDs will be LOST)'
    )


def _add_rescue_args(parser: argparse.ArgumentParser):
    """Add rescue-specific arguments."""

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

    image_group = parser.add_argument_group('IMAGE FLAGS')
    image_group.add_argument(
        '--rescue-image',
        metavar='IMAGE_URL',
        dest='rescue_image',
        default=None,
        help=(
            'Custom rescue disk image URL. Overrides the default OS/arch-based'
            ' image selection. Accepts a specific image URL'
            ' (projects/PROJECT/global/images/IMAGE) or an image family URL'
            ' (projects/PROJECT/global/images/family/FAMILY).'
        )
    )


def _add_repair_args(parser: argparse.ArgumentParser):
    """Add repair-specific arguments."""
    snapshot_group = parser.add_argument_group('SNAPSHOT FLAGS')
    snapshot_group.add_argument(
        '--snapshot',
        action='store_true',
        default=True,
        help='Create snapshot of boot disk before repair (default: enabled)'
    )
    snapshot_group.add_argument(
        '--no-snapshot',
        dest='snapshot',
        action='store_false',
        help='Skip snapshot creation (faster but riskier)'
    )

    image_group = parser.add_argument_group('IMAGE FLAGS')
    image_group.add_argument(
        '--rescue-image',
        metavar='IMAGE_URL',
        dest='rescue_image',
        default=None,
        help=(
            'Custom rescue disk image URL. Overrides the default OS/arch-based'
            ' image selection. Accepts a specific image URL'
            ' (projects/PROJECT/global/images/IMAGE) or an image family URL'
            ' (projects/PROJECT/global/images/family/FAMILY).'
        )
    )


def _add_restore_args(parser: argparse.ArgumentParser):
    """Add restore-specific arguments."""

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

    # Custom rescue image (overrides auto OS/arch detection)
    if hasattr(args, 'rescue_image') and args.rescue_image:
        config.custom_rescue_image = args.rescue_image

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


def _get_log_file() -> str:
    """Get the log file path from the active logger, if any."""
    import logging
    logger = logging.getLogger('gce_rescue')
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler):
            return handler.baseFilename
    return ''


def _print_support_footer(exit_code: int):
    """Print support footer with email and log file reference."""
    log_file = _get_log_file()
    import os
    if exit_code != 0:
        if log_file:
            log_name = os.path.basename(log_file)
            print(f"\nStuck? We've got your back. Email: gce-rescue-dev@google.com"
                  f" (attach log: {log_name})", file=sys.stderr)
        else:
            print(f"\nStuck? We've got your back. Email: gce-rescue-dev@google.com",
                  file=sys.stderr)
    else:
        print(f"\nSaved you time? We'd love to hear about it. Email:"
              f" gce-rescue-dev@google.com", file=sys.stderr)


def main():
    """Main CLI entry point (gcloud-compatible)."""

    exit_code = 0
    try:
        # Parse arguments
        parser = create_parser()
        args = parser.parse_args()

        # Validate
        if not validate_args(args):
            exit_code = 1
        # Execute command
        elif args.command == 'rescue':
            exit_code = handle_rescue(args)
        elif args.command == 'restore':
            exit_code = handle_restore(args)
        elif args.command == 'diagnose':
            exit_code = handle_diagnose(args)
        elif args.command == 'repair':
            exit_code = handle_repair(args)
        else:
            print(f"{error_prefix()} (gce-rescue) Unknown command: {args.command}",
                  file=sys.stderr)
            exit_code = 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.", file=sys.stderr)
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"{error_prefix()} (gce-rescue) Unexpected error: {str(e)}",
              file=sys.stderr)
        if '--verbosity=debug' in sys.argv or '--verbosity debug' in sys.argv:
            import traceback
            traceback.print_exc()
        exit_code = 1

    _print_support_footer(exit_code)
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
