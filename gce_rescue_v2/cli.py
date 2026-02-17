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
import threading
import time
import yaml
from typing import Optional, Dict, Any, List
from .core.config import RescueConfig, RestoreConfig, VERSION
from .main import rescue_vm, restore_vm, repair_vm
from .utils.colors import error_prefix, warning_prefix, clear_lines, green
from .utils.report_formatter import DiagnosisReportFormatter
from .orchestration.checkpoint import CheckpointManager, CheckpointData


def _create_tracked_client(compute, tracking_label: str):
    """Create a compute client with a tracking User-Agent header.

    Args:
        compute: Base compute client (used to extract credentials)
        tracking_label: Label appended to User-Agent (e.g., 'diagnose-vm-state')

    Returns:
        Compute API client with User-Agent: gce-rescue-{VERSION}-{tracking_label}
    """
    try:
        from googleapiclient import discovery
        import googleapiclient.http
        import google_auth_httplib2
        import httplib2

        # Verify compute client has real credentials (not a test mock)
        if not isinstance(getattr(compute, '_http', None), google_auth_httplib2.AuthorizedHttp):
            return compute

        credentials = compute._http.credentials
        user_agent = f'gce-rescue-{VERSION}-{tracking_label}'

        def _request_builder(http, *args, **kwargs):
            headers = kwargs.setdefault('headers', {})
            headers['user-agent'] = user_agent
            auth_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http()
            )
            return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

        return discovery.build(
            'compute', 'v1', credentials=credentials,
            cache_discovery=False, requestBuilder=_request_builder
        )
    except Exception:
        return compute


class OutputFormatter:
    """
    Handle output formatting similar to gcloud.

    Supports: json, yaml, table
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
    REPAIR_ONLY_FLAGS = []  # repair uses same flags as rescue for now

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
                    lines.append("  rescue         Boot a VM into rescue mode")
                    lines.append("  restore        Restore a VM from rescue mode")
                    lines.append("  diagnose       Diagnose VM boot issues (read-only)")
                    lines.append("  repair         Diagnose and auto-fix boot issues")
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
                "  rescue         Boot a VM into rescue mode",
                "  restore        Restore a VM from rescue mode",
                "  diagnose       Diagnose VM boot issues (read-only)",
                "  repair         Diagnose and auto-fix boot issues",
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

    Diagnose boot issues:
        $ gce-rescue-v2 diagnose my-vm --zone=us-central1-a

    Auto-repair boot issues:
        $ gce-rescue-v2 repair my-vm --zone=us-central1-a

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

    # DIAGNOSE-BOOT COMMAND
    diagnose_parser = subparsers.add_parser(
        'diagnose',
        help='Diagnose VM boot issues (read-only)',
        description='Analyze VM serial console output to detect boot errors.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Diagnose a VM:
        $ gce-rescue-v2 diagnose my-vm --zone=us-central1-a

    Diagnose and output as JSON:
        $ gce-rescue-v2 diagnose my-vm --zone=us-central1-a --format=json

    Diagnose and output as YAML:
        $ gce-rescue-v2 diagnose my-vm --zone=us-central1-a --format=yaml

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
        description='Automatically diagnose and repair boot issues. Combines diagnose, rescue (with embedded fix), and restore into a single command.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EXAMPLES
    Repair a VM with boot issues:
        $ gce-rescue-v2 repair my-vm --zone=us-central1-a

    Repair without snapshot (faster):
        $ gce-rescue-v2 repair my-vm --zone=us-central1-a --no-snapshot

    Repair in automation (no prompts):
        $ gce-rescue-v2 repair my-vm --zone=us-central1-a --quiet

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


class _Spinner:
    """Simple inline spinner for short-lived operations."""

    def __init__(self, message: str):
        self._message = message
        self._stop = False
        self._thread = None

    def start(self):
        self._stop = False
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, clear: bool = True):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.5)
        if clear:
            sys.stdout.write(f"\r{' ' * (len(self._message) + 10)}\r")
            sys.stdout.flush()

    def _spin(self):
        dots = ['.  ', '.. ', '...']
        idx = 0
        while not self._stop:
            sys.stdout.write(f"\r{self._message}{dots[idx]}")
            sys.stdout.flush()
            idx = (idx + 1) % len(dots)
            time.sleep(0.4)


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string (e.g. '1m 42s')."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes = total // 60
    secs = total % 60
    if secs == 0:
        return f"{minutes}m"
    return f"{minutes}m {secs}s"


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
        tracked = _create_tracked_client(compute, 'rescue-vm-preflight')
        vm = tracked.instances().get(
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
        tracked = _create_tracked_client(compute, 'restore-vm-preflight')
        vm = tracked.instances().get(
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


def _prompt_incomplete_operation(checkpoint: CheckpointData, operation_type: str) -> str:
    """
    Prompt user about incomplete operation (interactive mode only).

    Args:
        checkpoint: Checkpoint data from incomplete operation
        operation_type: 'rescue' or 'restore'

    Returns:
        'continue', 'rollback', or 'abort'
    """
    # Get next step name for continue option
    next_step = checkpoint.current_step + 1
    step_names = {
        1: "Stop VM", 2: "Detach Boot Disk", 3: "Create Snapshot",
        4: "Create Rescue Disk", 5: "Attach Rescue Disk", 6: "Set Metadata",
        7: "Start VM", 8: "Attach Original Disk", 9: "Verify Startup"
    }
    next_step_name = step_names.get(next_step, f"Step {next_step}")
    last_step_name = checkpoint.get_last_completed_operation() or "None"

    # Track lines for clearing after continue
    lines_printed = 0

    print(f"\n{warning_prefix()} An incomplete {operation_type} operation was detected for this instance.")
    lines_printed += 2  # includes leading newline
    print("")
    lines_printed += 1
    print(f"  Started:    {checkpoint.started_at[:19].replace('T', ' ')} ({checkpoint.get_age_display()})")
    lines_printed += 1
    print(f"  Progress:   {checkpoint.current_step} of {checkpoint.total_steps} steps completed")
    lines_printed += 1
    print(f"  Last step:  {last_step_name}")
    lines_printed += 1
    print("")
    lines_printed += 1
    print("What would you like to do?")
    lines_printed += 1
    print(f"  [1] Continue  Resume from \"{next_step_name}\"")
    lines_printed += 1
    print("  [2] Rollback  Undo completed steps and restore original state")
    lines_printed += 1
    print("  [3] Abort     Do nothing and exit")
    lines_printed += 1
    print("")
    lines_printed += 1

    while True:
        try:
            response = input("Enter your choice (1/2/3): ").strip()
            lines_printed += 1  # input line
            if response == '1':
                # Clear the warning message when continuing
                clear_lines(lines_printed)
                return 'continue'
            elif response == '2':
                return 'rollback'
            elif response == '3':
                return 'abort'
            else:
                print("Please enter 1, 2, or 3.")
                lines_printed += 1
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 'abort'


def _handle_restore_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                                        checkpoint: CheckpointData, logger=None) -> bool:
    """
    Rollback restore operation to return to rescue mode.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        logger: Optional logger

    Returns:
        True if rollback succeeded
    """
    from .orchestration.rollback import RollbackHandler
    from .orchestration.state import StateTracker
    from .operations import (
        StopVMOperation, DetachDiskOperation, AttachDiskOperation,
        SetMetadataOperation, StartVMOperation
    )

    # Create operations map for restore operations
    operations_map = {
        "Stop VM": StopVMOperation(compute, project, zone, logger),
        "Detach Rescue Disk": DetachDiskOperation(compute, project, zone, logger),
        "Detach Original Disk": DetachDiskOperation(compute, project, zone, logger),
        "Attach Original Disk": AttachDiskOperation(compute, project, zone, logger),
        "Set Metadata": SetMetadataOperation(compute, project, zone, logger),
        "Start VM": StartVMOperation(compute, project, zone, logger),
    }

    # Build state tracker from checkpoint
    state_tracker = StateTracker()
    for op in checkpoint.completed_operations:
        state_tracker.add_operation(
            operation_name=op.name,
            success=True,
            message="Completed in previous session",
            rollback_data=op.rollback_data,
            step_number=op.step
        )

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        print(f"\nNo changes were made. Checkpoint cleared for instance [{vm_name}].")
        return True

    # Perform rollback with progress indicator
    print(f"\nRolling back to rescue mode for instance [{vm_name}]:")
    spinner = _Spinner("  Undoing changes")
    spinner.start()
    handler = RollbackHandler(logger)
    success = handler.rollback(state_tracker, operations_map)
    spinner.stop()

    # Clear checkpoint after rollback
    checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
    checkpoint_mgr.clear_checkpoint()

    if success:
        print(f"Instance [{vm_name}] is back in rescue mode.")
    else:
        print(f"{error_prefix()} Rollback completed with errors. Manual intervention may be required.", file=sys.stderr)

    return success


def _reconcile_rescue_state(compute, project: str, zone: str, vm_name: str,
                            checkpoint: CheckpointData, state_tracker,
                            logger=None) -> None:
    """
    Reconcile checkpoint state with actual VM disk state.

    Inspects the real VM via GCP API and adds any operations that completed
    server-side but weren't checkpointed (e.g., Ctrl+C during API call).

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data from metadata
        state_tracker: StateTracker to add missing operations to
        logger: Optional logger
    """
    def _log(msg):
        if logger:
            logger.debug(f"[Reconcile] {msg}")

    context = checkpoint.context or {}
    original_disk_name = context.get('original_disk_name')
    rescue_disk_name = context.get('rescue_disk_name')

    if not original_disk_name:
        _log("No original_disk_name in checkpoint context, skipping reconciliation")
        return

    # Get actual VM state
    try:
        tracked = _create_tracked_client(compute, 'rescue-reconcile')
        vm = tracked.instances().get(
            project=project, zone=zone, instance=vm_name
        ).execute()
    except Exception as e:
        _log(f"Could not fetch VM state: {e}")
        return

    attached_disks = vm.get('disks', [])
    attached_disk_names = []
    for d in attached_disks:
        source = d.get('source', '')
        if source:
            attached_disk_names.append(source.split('/')[-1])

    checkpointed_op_names = {op.name for op in checkpoint.completed_operations}

    # Check 1: Original boot disk detached but not in checkpoint
    if original_disk_name not in attached_disk_names:
        if "Detach Boot Disk" not in checkpointed_op_names:
            _log(f"Original boot disk '{original_disk_name}' is detached but not in checkpoint - adding to rollback")
            disk_source = f"projects/{project}/zones/{zone}/disks/{original_disk_name}"
            state_tracker.add_operation(
                operation_name="Detach Boot Disk",
                success=True,
                message="Detected via reconciliation (completed but not checkpointed)",
                rollback_data={
                    'vm_name': vm_name,
                    'disk_info': {
                        'source': disk_source,
                        'boot': True,
                        'autoDelete': True,
                        'deviceName': original_disk_name,
                        'mode': 'READ_WRITE'
                    }
                },
                step_number=2
            )

    # Check 2: Rescue disk exists and is attached but not in checkpoint
    if rescue_disk_name and rescue_disk_name in attached_disk_names:
        if "Attach Rescue Disk" not in checkpointed_op_names:
            _log(f"Rescue disk '{rescue_disk_name}' is attached but not in checkpoint - adding to rollback")
            # Need to add Create Rescue Disk first (so rollback deletes it)
            if "Create Rescue Disk" not in checkpointed_op_names:
                state_tracker.add_operation(
                    operation_name="Create Rescue Disk",
                    success=True,
                    message="Detected via reconciliation",
                    rollback_data={'disk_name': rescue_disk_name},
                    step_number=4
                )
            state_tracker.add_operation(
                operation_name="Attach Rescue Disk",
                success=True,
                message="Detected via reconciliation",
                rollback_data={
                    'vm_name': vm_name,
                    'device_name': rescue_disk_name
                },
                step_number=5
            )

    # Check 3: Rescue disk exists (not attached) but creation not in checkpoint
    if rescue_disk_name and "Create Rescue Disk" not in checkpointed_op_names:
        if rescue_disk_name not in attached_disk_names:
            try:
                tracked_disk = _create_tracked_client(compute, 'rescue-reconcile')
                tracked_disk.disks().get(
                    project=project, zone=zone, disk=rescue_disk_name
                ).execute()
                _log(f"Rescue disk '{rescue_disk_name}' exists but not in checkpoint - adding to rollback")
                state_tracker.add_operation(
                    operation_name="Create Rescue Disk",
                    success=True,
                    message="Detected via reconciliation",
                    rollback_data={'disk_name': rescue_disk_name},
                    step_number=4
                )
            except Exception:
                pass  # Disk doesn't exist, nothing to reconcile

    # Check 4: Rescue metadata set but not in checkpoint
    metadata_obj = vm.get('metadata', {})
    metadata_items = metadata_obj.get('items', [])
    has_rescue_metadata = any(item.get('key') == 'rescue-mode' for item in metadata_items)
    if has_rescue_metadata and "Set Metadata" not in checkpointed_op_names:
        _log("Rescue metadata found but not in checkpoint - adding to rollback")
        # Reconstruct pre-rescue metadata by removing rescue keys and restoring backups
        rescue_keys = {'rescue-mode', 'startup-script', 'windows-startup-script-ps1',
                       'rescue-original-disk', 'rescue-os-type'}
        backup_prefix = 'rescue-backup-'
        restored_items = []
        for item in metadata_items:
            if item['key'] in rescue_keys:
                continue
            if item['key'].startswith(backup_prefix):
                restored_items.append({
                    'key': item['key'][len(backup_prefix):],
                    'value': item['value']
                })
            else:
                restored_items.append(item)
        original_metadata = {
            'fingerprint': metadata_obj.get('fingerprint', ''),
            'items': restored_items
        }
        state_tracker.add_operation(
            operation_name="Set Metadata",
            success=True,
            message="Detected via reconciliation",
            rollback_data={
                'vm_name': vm_name,
                'original_metadata': original_metadata
            },
            step_number=6
        )


def _handle_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                                checkpoint: CheckpointData, logger=None) -> bool:
    """
    Rollback from checkpoint.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        logger: Optional logger

    Returns:
        True if rollback succeeded
    """
    from .orchestration.rollback import RollbackHandler
    from .orchestration.state import StateTracker, OperationState
    from .operations import (
        StopVMOperation, DetachDiskOperation, AttachDiskOperation,
        SetMetadataOperation, StartVMOperation, DeleteDiskOperation,
        CreateSnapshotOperation, CreateDiskOperation
    )

    # Create operations map
    operations_map = {
        "Stop VM": StopVMOperation(compute, project, zone, logger),
        "Detach Boot Disk": DetachDiskOperation(compute, project, zone, logger),
        "Create Snapshot": CreateSnapshotOperation(compute, project, zone, logger),
        "Create Rescue Disk": CreateDiskOperation(compute, project, zone, logger),
        "Attach Rescue Disk": AttachDiskOperation(compute, project, zone, logger),
        "Set Metadata": SetMetadataOperation(compute, project, zone, logger),
        "Start VM": StartVMOperation(compute, project, zone, logger),
        "Attach Original Disk": AttachDiskOperation(compute, project, zone, logger),
    }

    # Build state tracker from checkpoint
    state_tracker = StateTracker()
    for op in checkpoint.completed_operations:
        state_tracker.add_operation(
            operation_name=op.name,
            success=True,
            message="Completed in previous session",
            rollback_data=op.rollback_data,
            step_number=op.step
        )

    # Reconcile with actual VM state to catch operations that completed
    # server-side but weren't checkpointed (e.g., Ctrl+C during API call)
    if checkpoint.operation == 'rescue':
        _reconcile_rescue_state(
            compute, project, zone, vm_name, checkpoint, state_tracker, logger
        )

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        # Nothing was changed — just clear the stale checkpoint
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        print(f"\nNo changes were made. Checkpoint cleared for instance [{vm_name}].")
        return True

    # Perform rollback with progress indicator
    print(f"\nRolling back instance [{vm_name}]:")
    spinner = _Spinner("  Undoing changes")
    spinner.start()
    handler = RollbackHandler(logger)
    success = handler.rollback(state_tracker, operations_map)
    spinner.stop()

    # Clear checkpoint after rollback
    checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
    checkpoint_mgr.clear_checkpoint()

    if success:
        print(f"Instance [{vm_name}] has been restored to its original state.")
    else:
        print(f"{error_prefix()} Rollback completed with errors. Manual intervention may be required.", file=sys.stderr)

    return success


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

    # Check for incomplete operation (interactive mode only)
    checkpoint = None
    resuming = False
    if not args.quiet:
        checkpoint_mgr = CheckpointManager(compute, project, args.zone, args.instance_name)
        checkpoint = checkpoint_mgr.detect_incomplete(operation_type='rescue')

        if checkpoint:
            action = _prompt_incomplete_operation(checkpoint, 'rescue')

            if action == 'abort':
                return 0
            elif action == 'rollback':
                success = _handle_checkpoint_rollback(
                    compute, project, args.zone, args.instance_name, checkpoint
                )
                return 0 if success else 1
            # action == 'continue': proceed with resume
            # Skip normal validation and confirmation when resuming
            vm_info = None
            has_local_ssd = False
            resuming = True

    if not resuming:
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
            print("WARNING: Stopping this VM will PERMANENTLY LOSE all data on Local SSDs!", file=sys.stderr)
            print("", file=sys.stderr)
            print("To proceed in quiet mode, use --force flag:", file=sys.stderr)
            print(f"  $ gce-rescue-v2 rescue {args.instance_name} --zone={args.zone} --quiet --force", file=sys.stderr)
            return 1

    # Interactive confirmation (unless --quiet or resuming)
    if not args.quiet and not resuming:
        # Count lines for clearing after confirmation
        lines_printed = 0

        print(f"\nYou are about to rescue instance [{args.instance_name}] in zone [{args.zone}] project [{project}].")
        lines_printed += 2  # includes leading newline
        print("")
        lines_printed += 1
        print("The following actions will be performed:")
        lines_printed += 1
        print(f" - Stop instance [{args.instance_name}].")
        lines_printed += 1
        if args.snapshot:
            print(" - Create a backup snapshot of the boot disk.")
        else:
            print(" - Snapshot creation skipped (--no-snapshot).")
        lines_printed += 1
        print(" - Detach the original boot disk.")
        lines_printed += 1
        print(" - Create a rescue disk and attach it as the new boot disk.")
        lines_printed += 1
        print(" - Replace startup-script metadata (original will be backed up).")
        lines_printed += 1
        print(" - Start the VM and attach original disk as secondary for repair.")
        lines_printed += 1

        # Show Local SSD warning if applicable
        if has_local_ssd:
            print(f" - {warning_prefix()} Data on Local SSDs ({', '.join(local_ssds)}) will be permanently lost.")
            lines_printed += 1

        print("")
        lines_printed += 1
        response = input("Do you want to continue (y/N)? ").strip().lower()
        lines_printed += 1  # The input line

        if response not in ('y', 'yes'):
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
    # Use log_file from checkpoint if resuming (continues logging to same file)
    log_file = checkpoint.context.get('log_file') if checkpoint else None
    success = rescue_vm(
        vm_name=args.instance_name,
        zone=args.zone,
        project=project,
        config=config,
        debug=debug,
        resume_checkpoint=checkpoint if checkpoint else None,
        log_file=log_file
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

    # Check for incomplete restore operation (interactive mode only)
    checkpoint = None
    resuming = False
    if not args.quiet:
        checkpoint_mgr = CheckpointManager(compute, project, args.zone, args.instance_name)
        checkpoint = checkpoint_mgr.detect_incomplete(operation_type='restore')

        if checkpoint:
            action = _prompt_incomplete_operation(checkpoint, 'restore')

            if action == 'abort':
                return 0
            elif action == 'rollback':
                # For restore rollback, we go back to rescue mode
                success = _handle_restore_checkpoint_rollback(
                    compute, project, args.zone, args.instance_name, checkpoint
                )
                return 0 if success else 1
            # action == 'continue': proceed with resume
            # Skip normal validation and confirmation when resuming
            resuming = True

    if not resuming:
        # Validate VM exists and is in rescue mode BEFORE confirmation
        valid, vm_info, error_msg = _validate_vm_for_restore(compute, project, args.zone, args.instance_name)
        if not valid:
            print(f"{error_prefix()} {error_msg}", file=sys.stderr)
            return 1

    # Interactive confirmation (unless --quiet or resuming)
    if not args.quiet and not resuming:
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
        print(" - Detach rescue disk and re-attach original disk as boot.")
        lines_printed += 1
        print(" - Restore original metadata (startup-script, etc.).")
        lines_printed += 1
        print(f" - Start instance [{args.instance_name}].")
        lines_printed += 1
        if hasattr(args, 'keep_rescue_disk') and args.keep_rescue_disk:
            print(" - Keep rescue disk for post-mortem analysis.")
        else:
            print(" - Delete the rescue disk.")
        lines_printed += 1
        print("")
        lines_printed += 1
        response = input("Do you want to continue (y/N)? ").strip().lower()
        lines_printed += 1  # The input line

        if response not in ('y', 'yes'):
            print("\nAborted by user.")
            return 0

        # Clear confirmation message after user confirms
        clear_lines(lines_printed)

    # Convert to config
    config = args_to_restore_config(args)

    # Execute
    debug = args.verbosity == 'debug'
    # Use log_file from checkpoint if resuming (continues logging to same file)
    log_file = checkpoint.context.get('log_file') if checkpoint else None
    success = restore_vm(
        vm_name=args.instance_name,
        zone=args.zone,
        project=project,
        config=config,
        debug=debug,
        resume_checkpoint=checkpoint if checkpoint else None,
        log_file=log_file
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


def handle_diagnose(args: argparse.Namespace) -> int:
    """Handle diagnose command."""
    from .core.auth import AuthManager
    from .operations import DiagnoseOperation
    from .validators import (
        ValidationRunner,
        CredentialsValidator,
        DiagnosePermissionsValidator,
    )
    import logging

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

    # Setup logging
    debug = args.verbosity == 'debug'
    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format='%(levelname)s: %(message)s'
    )
    logger = logging.getLogger(__name__)

    # Pre-flight validation: credentials + permissions + VM state
    runner = ValidationRunner()
    runner.add(CredentialsValidator(compute, project, args.zone))
    runner.add(DiagnosePermissionsValidator(
        compute, project, args.zone, args.instance_name,
        tracking_label='diagnose-val-iam'
    ))

    spinner = _Spinner("Checking VM state")
    if not debug:
        spinner.start()
    results = runner.run_all(logger)

    # Also fetch VM info for state/rescue/OS checks under the same spinner
    vm = None
    try:
        tracked = _create_tracked_client(compute, 'diagnose-vm-state')
        vm = tracked.instances().get(
            project=project, zone=args.zone, instance=args.instance_name
        ).execute()
    except Exception as e:
        logger.debug(f"Could not fetch VM info: {e}")
        # VM existence validated during diagnose execution

    if not debug:
        spinner.stop()

    if not results.all_passed():
        results.print_failures()
        return 1

    # Pre-flight checks on VM (rescue mode first, then state, then OS)
    if vm:
        # Rescue mode check first — always suggest restoring
        metadata_items = vm.get('metadata', {}).get('items', [])
        if any(item.get('key') == 'rescue-mode' for item in metadata_items):
            print(f"{error_prefix()} Instance [{args.instance_name}] is in rescue mode.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Serial console shows the rescue environment, not original boot errors.", file=sys.stderr)
            print("Restore the VM first, then run diagnose:", file=sys.stderr)
            print(f"  $ gce-rescue-v2 restore {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            return 1

        # Must be RUNNING (serial console has no logs when terminated)
        vm_status = vm.get('status', 'UNKNOWN')
        if vm_status != 'RUNNING':
            print(f"{error_prefix()} Instance [{args.instance_name}] is {vm_status}.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Diagnose requires serial console output from a running VM.", file=sys.stderr)
            if vm_status == 'TERMINATED':
                print("Start the VM first:", file=sys.stderr)
                print(f"  $ gcloud compute instances start {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            return 1

        # Linux only
        from .utils.os_detection import detect_os_type
        os_type = detect_os_type(vm)
        if os_type == 'windows':
            print(f"{error_prefix()} Diagnose is only supported for Linux VMs.", file=sys.stderr)
            print("", file=sys.stderr)
            print("For Windows VMs, check the serial console output manually:", file=sys.stderr)
            print(f"  $ gcloud compute instances get-serial-port-output {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            print("", file=sys.stderr)
            print(f"  Console: https://console.cloud.google.com/compute/instancesDetail/zones/{args.zone}/instances/{args.instance_name}/console?project={project}&port=1", file=sys.stderr)
            return 1

    # Create and execute diagnose operation
    try:
        diagnose_op = DiagnoseOperation(compute, project, args.zone, logger)

        spinner = _Spinner("Analyzing serial console output")
        if not debug:
            spinner.start()
        result = diagnose_op.execute(args.instance_name, tracking_label='diagnose')
        if not debug:
            spinner.stop()

        if not result.success:
            # Operation failed (e.g., serial console disabled)
            print(f"{error_prefix()} {result.message}", file=sys.stderr)

            # Print recommendations if available
            if result.rollback_data and result.rollback_data.get('recommendations'):
                print("", file=sys.stderr)
                for rec in result.rollback_data['recommendations']:
                    print(f"  {rec}", file=sys.stderr)

            return 1

        # Operation succeeded - format and print results
        diagnosis = result.rollback_data

        # If format is json/yaml, use structured output
        if args.format in ('json', 'yaml', 'table'):
            print(OutputFormatter.format_output(diagnosis, args.format))
            return 0

        # Otherwise, print human-readable output
        diagnosis['project'] = project
        formatter = DiagnosisReportFormatter()
        print(formatter.format_report(diagnosis))

        return 0

    except Exception as e:
        logger.error(f"Unexpected error during diagnosis: {e}", exc_info=debug)
        print(f"{error_prefix()} Unexpected error: {e}", file=sys.stderr)
        return 1


def _show_boot_verification(boot_verified: Optional[bool],
                            boot_errors: List[str],
                            vm_name: str, zone: str) -> None:
    """Display boot verification result after repair."""
    if boot_verified is True:
        print(green("Boot verification: VM is booting normally."))
    elif boot_verified is False:
        print("")
        print(f"{warning_prefix()} VM may still have boot issues:")
        for err in boot_errors:
            print(f"  - {err}")
        print("")
        print("Consider using rescue mode for manual investigation:")
        print(f"  $ gce-rescue-v2 rescue {vm_name} --zone={zone}")
    # If None, skip silently (couldn't verify)


def _show_repair_results(result: Dict[str, Any], vm_name: str,
                         zone: str = '', project: str = '') -> int:
    """Display repair results and return exit code."""
    status = result.get('status', 'unknown')
    fix_lines = result.get('fix_lines', [])
    fixed_count = result.get('fixed_count', 0)
    error = result.get('error')
    snapshot_name = result.get('snapshot_name')
    duration = result.get('duration_seconds', 0)

    duration_str = _format_duration(duration) if duration else ''

    boot_verified = result.get('boot_verified')
    boot_errors_after = result.get('boot_errors_after', [])

    if status == 'success':
        print("")
        print("Repair results:")
        for line in fix_lines:
            colored_line = line.replace('[FIXED]', green('[FIXED]'), 1)
            print(f"  {colored_line}")
        issue_word = "issue" if fixed_count == 1 else "issues"
        print(f"  {fixed_count} {issue_word} fixed.")
        if any('fstab' in line.lower() for line in fix_lines):
            print(f"  Original fstab backed up to: /etc/fstab.gce-repair-backup")
        if snapshot_name:
            print(f"  Backup snapshot: {snapshot_name}")
        print("")
        completion = f"Repair complete. Instance [{vm_name}] is now running."
        if duration_str:
            completion += f" ({duration_str})"
        print(completion)
        _show_boot_verification(boot_verified, boot_errors_after, vm_name, zone)
        return 0

    elif status == 'no_issues':
        print("")
        print("Repair results:")
        print("  No issues needed fixing (fstab entries were already valid).")
        if snapshot_name:
            print(f"  Backup snapshot: {snapshot_name}")
        print("")
        completion = f"Repair complete. Instance [{vm_name}] is now running."
        if duration_str:
            completion += f" ({duration_str})"
        print(completion)
        _show_boot_verification(boot_verified, boot_errors_after, vm_name, zone)
        return 0

    elif status == 'no_fix':
        print("No automated fix available for the detected issues.")
        return 0

    elif status == 'failed':
        print("", file=sys.stderr)
        print(f"{warning_prefix()} Fix script reported a problem: {error}", file=sys.stderr)
        if fix_lines:
            print("Partial results:", file=sys.stderr)
            for line in fix_lines:
                print(f"  {line}", file=sys.stderr)
            if any('fstab' in line.lower() for line in fix_lines):
                print(f"  Original fstab backed up to: /etc/fstab.gce-repair-backup", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"Instance [{vm_name}] has been restored and is running.", file=sys.stderr)
        if snapshot_name:
            print(f"Backup snapshot: {snapshot_name}", file=sys.stderr)
            print("To revert to the pre-repair state:", file=sys.stderr)
            print(f"  https://console.cloud.google.com/compute/snapshotsDetail"
                  f"/projects/{project}/global/snapshots/{snapshot_name}",
                  file=sys.stderr)
        print("The issue may require manual intervention.", file=sys.stderr)
        return 1

    elif status == 'mount_failed':
        print("", file=sys.stderr)
        print(f"{error_prefix()} {error}", file=sys.stderr)
        print("", file=sys.stderr)
        if snapshot_name:
            print(f"Backup snapshot: {snapshot_name}", file=sys.stderr)
        print("VM is in rescue mode for manual investigation.", file=sys.stderr)
        print("Connect via SSH and inspect the disk, then restore:", file=sys.stderr)
        ssh_cmd = f"  $ gcloud compute ssh {vm_name} --zone={zone}"
        if project:
            ssh_cmd += f" --project={project}"
        print(ssh_cmd, file=sys.stderr)
        restore_cmd = f"  $ gce-rescue-v2 restore {vm_name}"
        if zone:
            restore_cmd += f" --zone={zone}"
        if project:
            restore_cmd += f" --project={project}"
        print(restore_cmd, file=sys.stderr)
        return 1

    elif status == 'rescue_failed':
        print("", file=sys.stderr)
        print(f"{error_prefix()} {error}", file=sys.stderr)
        if snapshot_name:
            print(f"Backup snapshot: {snapshot_name}", file=sys.stderr)
        return 1

    elif status == 'restore_failed':
        print("", file=sys.stderr)
        print(f"{error_prefix()} Restore failed after repair.", file=sys.stderr)
        if fix_lines:
            print("Repair did complete:", file=sys.stderr)
            for line in fix_lines:
                print(f"  {line}", file=sys.stderr)
        print("", file=sys.stderr)
        if snapshot_name:
            print(f"Backup snapshot: {snapshot_name}", file=sys.stderr)
        print("VM may still be in rescue mode. Try restoring manually:", file=sys.stderr)
        restore_cmd = f"  $ gce-rescue-v2 restore {vm_name}"
        if zone:
            restore_cmd += f" --zone={zone}"
        if project:
            restore_cmd += f" --project={project}"
        print(restore_cmd, file=sys.stderr)
        return 1

    elif status == 'unknown':
        # All phases completed but repair markers not found in serial output.
        # The fix likely applied but we couldn't parse confirmation.
        print("")
        print(f"{warning_prefix()} Repair completed but could not confirm fix results from serial console.")
        print("")
        completion = f"Instance [{vm_name}] has been restored and is running."
        if duration_str:
            completion += f" ({duration_str})"
        print(completion)
        if snapshot_name:
            print(f"Backup snapshot: {snapshot_name}")
        _show_boot_verification(boot_verified, boot_errors_after, vm_name, zone)
        return 0

    else:
        print(f"\n{error_prefix()} Unexpected result: {status}", file=sys.stderr)
        if error:
            print(f"  {error}", file=sys.stderr)
        return 1


def handle_repair(args: argparse.Namespace) -> int:
    """Handle repair command."""
    from .core.auth import AuthManager

    # Get project from args or gcloud config
    project = args.project or get_gcloud_config('core/project')

    if not project:
        print(f"{error_prefix()} No project specified.", file=sys.stderr)
        print("", file=sys.stderr)
        print("Please specify a project using one of these methods:", file=sys.stderr)
        print("  1. --project=PROJECT_ID flag", file=sys.stderr)
        print("  2. gcloud config set project PROJECT_ID", file=sys.stderr)
        print("  3. Set CLOUDSDK_CORE_PROJECT environment variable", file=sys.stderr)
        return 1

    # Get compute client
    try:
        auth = AuthManager()
        compute, project = auth.get_client(project)
    except Exception as e:
        print(f"{error_prefix()} Authentication failed: {e}", file=sys.stderr)
        return 1

    # Setup logging
    import logging
    debug = args.verbosity == 'debug'
    log_level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=log_level, format='%(levelname)s: %(message)s')
    logger = logging.getLogger(__name__)
    logger.console_level = log_level

    # Create repair orchestrator
    from .orchestration.repair import RepairOrchestrator, SUPPORTED_FIX_CATEGORIES
    config = args_to_rescue_config(args)

    orchestrator = RepairOrchestrator(
        compute=compute, project=project, zone=args.zone,
        vm_name=args.instance_name, config=config, logger=logger
    )

    # Pre-flight: check VM state (rescue mode, running, etc.)
    spinner = _Spinner("Checking VM state")
    if not debug:
        spinner.start()
    try:
        tracked = _create_tracked_client(compute, 'repair-vm-state')
        vm = tracked.instances().get(
            project=project, zone=args.zone, instance=args.instance_name
        ).execute()
    except Exception as e:
        if not debug:
            spinner.stop()
        logger.debug(f"Could not fetch VM info: {e}")
        vm = None

    if not debug:
        spinner.stop()

    if vm:
        # Linux-only check (before any spinners or prompts)
        from .utils.os_detection import detect_os_type
        os_type = detect_os_type(vm)
        if os_type == 'windows':
            print(f"{error_prefix()} Repair is only supported for Linux VMs.", file=sys.stderr)
            print("", file=sys.stderr)
            print("For Windows VMs, use rescue mode for manual repair:", file=sys.stderr)
            print(f"  $ gce-rescue-v2 rescue {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Or check the serial console output manually:", file=sys.stderr)
            print(f"  $ gcloud compute instances get-serial-port-output {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            print("", file=sys.stderr)
            print(f"  Console: https://console.cloud.google.com/compute/instancesDetail/zones/{args.zone}/instances/{args.instance_name}/console?project={project}&port=1", file=sys.stderr)
            return 1

        vm_status = vm.get('status', 'UNKNOWN')
        metadata_items = vm.get('metadata', {}).get('items', [])
        in_rescue = any(item.get('key') == 'rescue-mode' for item in metadata_items)

        # Check for incomplete rescue checkpoint (from interrupted repair)
        if not in_rescue and not args.quiet:
            checkpoint_mgr = CheckpointManager(
                compute, project, args.zone, args.instance_name, logger
            )
            checkpoint = checkpoint_mgr.detect_incomplete(
                operation_type='rescue'
            )
            if checkpoint:
                print(f"\n{warning_prefix()} An incomplete rescue operation was "
                      f"detected for instance [{args.instance_name}].")
                print("")
                print(f"  Started:    {checkpoint.started_at[:19].replace('T', ' ')} "
                      f"({checkpoint.get_age_display()})")
                print(f"  Progress:   {checkpoint.current_step} of "
                      f"{checkpoint.total_steps} steps completed")
                last_step = checkpoint.get_last_completed_operation() or "None"
                print(f"  Last step:  {last_step}")
                print("")
                print("This must be resolved before repair can proceed.")
                print("")
                print("  [1] Rollback  Undo completed steps and restore original state")
                print("  [2] Abort     Do nothing and exit")
                print("")

                while True:
                    try:
                        response = input("Enter your choice (1/2): ").strip()
                        if response == '1':
                            success = _handle_checkpoint_rollback(
                                compute, project, args.zone,
                                args.instance_name, checkpoint, logger
                            )
                            if success:
                                print("")
                                print("Run repair again to fix boot issues:")
                                print(
                                    f"  $ gce-rescue-v2 repair "
                                    f"{args.instance_name} --zone={args.zone} "
                                    f"--project={project}"
                                )
                            return 0 if success else 1
                        elif response == '2':
                            return 0
                        else:
                            print("Please enter 1 or 2.")
                    except (KeyboardInterrupt, EOFError):
                        print("\nAborted.")
                        return 0

        # If not in rescue mode, must be RUNNING
        if not in_rescue and vm_status != 'RUNNING':
            print(f"{error_prefix()} Instance [{args.instance_name}] is {vm_status}.", file=sys.stderr)
            print("", file=sys.stderr)
            print("Repair requires the VM to be running for serial console diagnosis.", file=sys.stderr)
            if vm_status == 'TERMINATED':
                print("Start the VM first:", file=sys.stderr)
                print(f"  $ gcloud compute instances start {args.instance_name} --zone={args.zone} --project={project}", file=sys.stderr)
            return 1

        if in_rescue:
            print(f"{warning_prefix()} Instance [{args.instance_name}] is in rescue mode "
                  f"from a previous operation.")
            print("")
            print("  [1] Continue  Check repair results and restore the VM")
            print("  [2] Abort     Do nothing and exit")
            print("")

            while True:
                try:
                    response = input("Enter your choice (1/2): ").strip()
                    if response == '1':
                        break
                    elif response == '2':
                        return 0
                    else:
                        print("Please enter 1 or 2.")
                except (KeyboardInterrupt, EOFError):
                    print("\nAborted.")
                    return 0

            print("")
            result = orchestrator.resume()
            return _show_repair_results(result, args.instance_name,
                                        zone=args.zone, project=project)

    # Validate (credentials, IAM, Shielded/Confidential, Linux-only)
    spinner = _Spinner("Validating permissions")
    if not debug:
        spinner.start()
    valid = orchestrator.validate()
    if not debug:
        spinner.stop()
    if not valid:
        return 1

    # Diagnose
    spinner = _Spinner("Analyzing serial console output")
    if not debug:
        spinner.start()
    diagnosis = orchestrator.diagnose()
    if not debug:
        spinner.stop()
    if diagnosis is None:
        return 1

    # Analyze diagnosis results
    diagnosis['project'] = project
    boot_errors = diagnosis.get('boot_errors', [])
    fixable = orchestrator.get_fixable_categories(diagnosis)
    unfixable = orchestrator.get_unfixable_categories(diagnosis)
    snapshot_enabled = getattr(args, 'snapshot', True)

    # Non-repair paths: compact message and return
    if not boot_errors:
        print(f"Repair: {args.instance_name} ({args.zone})")
        print("")
        print("  No boot issues found. Nothing to repair.")
        print("  Run 'diagnose' for details.")
        return 0

    if not fixable:
        print(f"Repair: {args.instance_name} ({args.zone})")
        print("")
        for cat in unfixable:
            print(
                f"  Detected [{cat.upper()}] issue but automated fix is not yet available."
            )
        print("  Run 'diagnose' for details.")
        print("")
        print("  Use rescue mode for manual repair:")
        print(
            f"    $ gce-rescue-v2 rescue {args.instance_name} "
            f"--zone={args.zone} --project={project}"
        )
        return 0

    # Repair path: show compact summary + plan, get confirmation, then clear
    if not args.quiet:
        lines_to_clear = 0

        # Header
        print(f"Repair: {args.instance_name} ({args.zone})")
        lines_to_clear += 1
        print("")
        lines_to_clear += 1

        # Compact issue summary grouped by category
        from collections import Counter
        category_counts: Dict[str, int] = Counter(
            err['category'] for err in boot_errors
        )
        severity_counts: Dict[str, Dict[str, int]] = {}
        for err in boot_errors:
            cat = err['category']
            sev = err.get('severity', 'error')
            if cat not in severity_counts:
                severity_counts[cat] = Counter()
            severity_counts[cat][sev] += 1

        for cat, count in category_counts.items():
            sev_parts = []
            for sev in ('critical', 'error', 'warning'):
                if severity_counts[cat].get(sev, 0) > 0:
                    sev_parts.append(f"{severity_counts[cat][sev]} {sev}")
            sev_str = ', '.join(sev_parts)
            issue_word = 'issue' if count == 1 else 'issues'
            print(f"  Found {count} {cat} {issue_word} ({sev_str})")
            lines_to_clear += 1

        # Unfixable warnings
        if unfixable:
            for cat in unfixable:
                print(
                    f"  {warning_prefix()} [{cat.upper()}] requires manual repair"
                )
                lines_to_clear += 1

        print("  Run 'diagnose' for details.")
        lines_to_clear += 1
        print("")
        lines_to_clear += 1

        # Repair plan
        print("  Repair plan:")
        lines_to_clear += 1
        step = 1
        if snapshot_enabled:
            print(f"    {step}. Create backup snapshot of boot disk")
            lines_to_clear += 1
            step += 1
        print(f"    {step}. Enter rescue mode (stop VM, swap boot disk)")
        lines_to_clear += 1
        step += 1
        fix_descriptions = {
            'fstab': 'Fix /etc/fstab (comment out invalid entries)',
        }
        for cat in fixable:
            desc = fix_descriptions.get(cat, f'Fix {cat}')
            print(f"    {step}. {desc}")
            lines_to_clear += 1
            step += 1
        print(f"    {step}. Restore original boot disk and start VM")
        lines_to_clear += 1
        print("")
        lines_to_clear += 1

        # Confirmation
        try:
            response = input(
                "  Proceed? [y/N]: "
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 0

        if response not in ('y', 'yes'):
            print("\nAborted by user.")
            return 0

        lines_to_clear += 1  # input line

        # Clear diagnosis + plan + confirmation
        clear_lines(lines_to_clear)

    # Print concise repair header
    print(f"Repairing instance [{args.instance_name}]:")
    if len(boot_errors) == 1:
        err = boot_errors[0]
        print(f"  Issue:  [{err['category'].upper()}] {err['description']}")
    else:
        for i, err in enumerate(boot_errors[:3]):
            label = "  Issues:" if i == 0 else "         "
            print(f"{label} [{err['category'].upper()}] {err['description']}")
        if len(boot_errors) > 3:
            print(f"          ... and {len(boot_errors) - 3} more")

    plan_parts = []
    if snapshot_enabled:
        plan_parts.append("Snapshot")
    plan_parts.append("Rescue")
    fix_labels = {'fstab': 'Fix fstab'}
    for cat in fixable:
        plan_parts.append(fix_labels.get(cat, f'Fix {cat}'))
    plan_parts.append("Restore")
    print(f"  Plan:   {' -> '.join(plan_parts)}")
    print("")

    # Execute repair (concise header already printed)
    orchestrator._suppress_header = True
    result = orchestrator.execute(diagnosis)
    return _show_repair_results(result, args.instance_name,
                                zone=args.zone, project=project)


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
        elif args.command == 'diagnose':
            return handle_diagnose(args)
        elif args.command == 'repair':
            return handle_repair(args)
        else:
            print(f"{error_prefix()} (gce-rescue-v2) Unknown command: {args.command}", file=sys.stderr)
            return 1

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user.", file=sys.stderr)
        return 130  # Standard exit code for SIGINT
    except Exception as e:
        print(f"{error_prefix()} (gce-rescue-v2) Unexpected error: {str(e)}", file=sys.stderr)
        if '--verbosity=debug' in sys.argv or '--verbosity debug' in sys.argv:
            import traceback
            traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
