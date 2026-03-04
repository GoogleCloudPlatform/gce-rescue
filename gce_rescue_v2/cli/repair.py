"""Repair command handler."""

import argparse
import logging
import sys
from collections import Counter
from datetime import datetime
from typing import Optional, Dict, Any, List
from ..utils.colors import error_prefix, warning_prefix, clear_lines, green, bold
from ..utils.logger import setup_logging
from ..orchestration.checkpoint import CheckpointManager
from .output import _Spinner, _format_duration
from .preflight import get_gcloud_config, _create_tracked_client
from .checkpoint_ui import _handle_checkpoint_rollback


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
        print(f"  $ gce-rescue rescue {vm_name} --zone={zone}")
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
                print(f"  Original fstab backed up to: /etc/fstab.gce-repair-backup",
                      file=sys.stderr)
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
        restore_cmd = f"  $ gce-rescue restore {vm_name}"
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
        restore_cmd = f"  $ gce-rescue restore {vm_name}"
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
        print(f"{warning_prefix()} Repair completed but could not confirm fix results"
              f" from serial console.")
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
    from ..core.auth import AuthManager

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
    debug = args.verbosity == 'debug'
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_file = f"{args.instance_name}-repair-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    logger.debug(f"GCE Rescue - Repair")
    logger.debug(f"Log file: {log_file}")
    logger.debug(f"VM: {args.instance_name}, Zone: {args.zone}, Project: {project}")

    # Create repair orchestrator
    from ..orchestration.repair import RepairOrchestrator
    from . import args_to_rescue_config
    config = args_to_rescue_config(args)

    orchestrator = RepairOrchestrator(
        compute=compute, project=project, zone=args.zone,
        vm_name=args.instance_name, config=config, logger=logger,
        log_file=log_file
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
        from ..utils.os_detection import detect_os_type
        os_type = detect_os_type(vm)
        if os_type == 'windows':
            print(f"{error_prefix()} Repair is only supported for Linux VMs.",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print("For Windows VMs, use rescue mode for manual repair:", file=sys.stderr)
            print(f"  $ gce-rescue rescue {args.instance_name} --zone={args.zone}"
                  f" --project={project}", file=sys.stderr)
            print("", file=sys.stderr)
            print("Or check the serial console output manually:", file=sys.stderr)
            print(f"  $ gcloud compute instances get-serial-port-output"
                  f" {args.instance_name} --zone={args.zone} --project={project}",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print(f"  Console: https://console.cloud.google.com/compute/"
                  f"instancesDetail/zones/{args.zone}/instances/{args.instance_name}"
                  f"/console?project={project}&port=1", file=sys.stderr)
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
                                    f"  $ gce-rescue repair "
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
            print(f"{error_prefix()} Instance [{args.instance_name}] is {vm_status}.",
                  file=sys.stderr)
            print("", file=sys.stderr)
            print("Repair requires the VM to be running for serial console diagnosis.",
                  file=sys.stderr)
            if vm_status == 'TERMINATED':
                print("Start the VM first:", file=sys.stderr)
                print(f"  $ gcloud compute instances start {args.instance_name}"
                      f" --zone={args.zone} --project={project}", file=sys.stderr)
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
            f"    $ gce-rescue rescue {args.instance_name} "
            f"--zone={args.zone} --project={project}"
        )
        return 0

    # Guard: if fstab errors detected but no specific entries identified,
    # auto-repair can't know which lines to comment out.
    fstab_targets = orchestrator._extract_fstab_targets(diagnosis)
    if 'fstab' in fixable and not fstab_targets:
        print(f"Repair: {args.instance_name} ({args.zone})")
        print("")
        print("  Auto-repair could not identify specific fstab entries to fix.")
        print("  Run 'diagnose' for details.")
        print("")
        print("  Use rescue mode for manual repair:")
        print(
            f"    $ gce-rescue rescue {args.instance_name} "
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
        # Build fix descriptions with extracted identifiers
        if fstab_targets:
            from ..utils.report_formatter import _extract_identifier
            identifiers = []
            for err in boot_errors:
                if err.get('category') != 'fstab':
                    continue
                ident = _extract_identifier(err.get('detected_pattern', ''))
                if ident and ident not in identifiers:
                    identifiers.append(ident)
            if identifiers:
                target_str = ', '.join(bold(i) for i in identifiers)
                fix_descriptions = {
                    'fstab': f'Fix /etc/fstab (comment out {target_str})',
                }
            else:
                fix_descriptions = {
                    'fstab': 'Fix /etc/fstab (comment out invalid entries)',
                }
        else:
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
