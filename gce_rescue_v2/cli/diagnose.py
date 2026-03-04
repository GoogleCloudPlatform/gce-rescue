"""Diagnose command handler."""

import argparse
import logging
import sys
from ..utils.colors import error_prefix
from ..utils.report_formatter import DiagnosisReportFormatter
from .output import OutputFormatter, _Spinner
from .preflight import get_gcloud_config, _create_tracked_client


def handle_diagnose(args: argparse.Namespace) -> int:
    """Handle diagnose command."""
    from ..core.auth import AuthManager
    from ..operations import DiagnoseOperation
    from ..validators import (
        ValidationRunner,
        CredentialsValidator,
        DiagnosePermissionsValidator,
    )

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
        # Rescue mode check first -- always suggest restoring
        metadata_items = vm.get('metadata', {}).get('items', [])
        if any(item.get('key') == 'rescue-mode' for item in metadata_items):
            print(f"{error_prefix()} Instance [{args.instance_name}] is in rescue mode.",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print("Serial console shows the rescue environment, not original boot errors.",
                  file=sys.stderr)
            print("Restore the VM first, then run diagnose:", file=sys.stderr)
            print(f"  $ gce-rescue restore {args.instance_name} --zone={args.zone}"
                  f" --project={project}", file=sys.stderr)
            return 1

        # Must be RUNNING (serial console has no logs when terminated)
        vm_status = vm.get('status', 'UNKNOWN')
        if vm_status != 'RUNNING':
            print(f"{error_prefix()} Instance [{args.instance_name}] is {vm_status}.",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print("Diagnose requires serial console output from a running VM.",
                  file=sys.stderr)
            if vm_status == 'TERMINATED':
                print("Start the VM first:", file=sys.stderr)
                print(f"  $ gcloud compute instances start {args.instance_name}"
                      f" --zone={args.zone} --project={project}", file=sys.stderr)
            return 1

        # Linux only
        from ..utils.os_detection import detect_os_type
        os_type = detect_os_type(vm)
        if os_type == 'windows':
            print(f"{error_prefix()} Diagnose is only supported for Linux VMs.",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print("For Windows VMs, check the serial console output manually:",
                  file=sys.stderr)
            print(f"  $ gcloud compute instances get-serial-port-output"
                  f" {args.instance_name} --zone={args.zone} --project={project}",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print(f"  Console: https://console.cloud.google.com/compute/"
                  f"instancesDetail/zones/{args.zone}/instances/{args.instance_name}"
                  f"/console?project={project}&port=1", file=sys.stderr)
            return 1

    # Create and execute diagnose operation
    try:
        diagnose_op = DiagnoseOperation(compute, project, args.zone, logger)

        spinner = _Spinner("Analyzing serial console output")
        if not debug:
            spinner.start()
        result = diagnose_op.execute(
            args.instance_name, tracking_label='diagnose', stabilize=True
        )
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
