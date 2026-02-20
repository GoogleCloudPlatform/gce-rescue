"""Rescue command handler."""

import argparse
import sys
from datetime import datetime
from ..core.config import RescueConfig, OS_TYPE_WINDOWS
from ..core.error_messages import get_error_suggestion
from ..utils.colors import error_prefix, warning_prefix, note_prefix, clear_lines
from ..utils.logger import setup_logging
from ..orchestration.checkpoint import CheckpointManager
from ..orchestration import RescueOrchestrator
from .output import OutputFormatter
from . import preflight
from . import checkpoint_ui


def handle_rescue(args: argparse.Namespace) -> int:
    """Handle rescue command."""
    from ..core.auth import AuthManager

    # Get project from args or gcloud config
    project = args.project or preflight.get_gcloud_config('core/project')

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
            action = checkpoint_ui._prompt_incomplete_operation(checkpoint, 'rescue')

            if action == 'abort':
                return 0
            elif action == 'rollback':
                success = checkpoint_ui._handle_checkpoint_rollback(
                    compute, project, args.zone, args.instance_name, checkpoint
                )
                return 0 if success else 1
            # action == 'continue': proceed with resume
            vm_info = None
            has_local_ssd = False
            resuming = True

    if not resuming:
        # Validate VM exists and state BEFORE confirmation
        valid, vm_info, error_msg = preflight._validate_vm_exists(
            compute, project, args.zone, args.instance_name
        )
        if not valid:
            print(f"{error_prefix()} {error_msg}", file=sys.stderr)
            return 1

        # Check for Local SSDs using validated VM info
        local_ssds = preflight._check_local_ssds(vm_info)
        has_local_ssd = len(local_ssds) > 0

        # In quiet mode with Local SSDs, require --force
        if args.quiet and has_local_ssd and not args.force:
            print(f"{error_prefix()} VM has Local SSDs attached.", file=sys.stderr)
            print("", file=sys.stderr)
            print(f"Local SSDs found: {', '.join(local_ssds)}", file=sys.stderr)
            print("", file=sys.stderr)
            print("WARNING: Stopping this VM will PERMANENTLY LOSE all data on"
                  " Local SSDs!", file=sys.stderr)
            print("", file=sys.stderr)
            print("To proceed in quiet mode, use --force flag:", file=sys.stderr)
            print(f"  $ gce-rescue-v2 rescue {args.instance_name} --zone={args.zone}"
                  f" --quiet --force", file=sys.stderr)
            return 1

    # Interactive confirmation (unless --quiet or resuming)
    if not args.quiet and not resuming:
        lines_printed = 0

        print(f"\nYou are about to rescue instance [{args.instance_name}] in zone"
              f" [{args.zone}] project [{project}].")
        lines_printed += 2
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

        if has_local_ssd:
            print(f" - {warning_prefix()} Data on Local SSDs"
                  f" ({', '.join(local_ssds)}) will be permanently lost.")
            lines_printed += 1

        print("")
        lines_printed += 1
        response = input("Do you want to continue (y/N)? ").strip().lower()
        lines_printed += 1

        if response not in ('y', 'yes'):
            print("\nAborted by user.")
            return 0

        clear_lines(lines_printed)

    # Convert to config
    from . import args_to_rescue_config
    config = args_to_rescue_config(args)

    if has_local_ssd:
        config.force = True

    # Setup logging
    debug = args.verbosity == 'debug'
    log_file = checkpoint.context.get('log_file') if checkpoint else None
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = f"{args.instance_name}-rescue-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    vm_name = args.instance_name
    zone = args.zone

    if checkpoint:
        logger.debug("=== Session Resumed ===")
    logger.debug(f"GCE Rescue V2 - Rescue")
    logger.debug(f"Log file: {log_file}")
    logger.debug(f"VM: {vm_name}, Zone: {zone}, Project: {project}")

    try:
        # Create orchestrator
        orchestrator = RescueOrchestrator(
            compute=compute, project=project, zone=zone,
            vm_name=vm_name, config=config, logger=logger,
            log_file=log_file
        )

        # Set resume state if resuming from checkpoint
        if checkpoint:
            orchestrator.set_resume_state(checkpoint)
            logger.debug(f"Resuming from step {checkpoint.current_step + 1}")

        # Validate (skip if resuming)
        if not checkpoint and not orchestrator.validate():
            logger.error("Validation failed. Cannot proceed with rescue.")
            return 1

        # Execute
        if not orchestrator.execute():
            logger.error("Rescue failed.")
            return 1

        # Success output
        logger.info("")
        logger.info(f"Rescue mode enabled for instance [{vm_name}].")
        logger.info("")

        mount_path = "D:\\" if orchestrator.os_type == OS_TYPE_WINDOWS else "/mnt/sysroot"
        logger.info(f"Affected disk mounted at: {mount_path}")
        if orchestrator.snapshot_name:
            logger.info(f"Backup snapshot: {orchestrator.snapshot_name}")
        logger.info("")

        if not orchestrator.verification_succeeded:
            is_win = orchestrator.os_type == OS_TYPE_WINDOWS
            wait_time = "1-2 minutes" if is_win else "30 seconds"
            rescue_log = "C:\\gce-rescue.log" if is_win else "/var/log/gce-rescue.log"
            creds_note = ", credentials" if is_win else ""
            logger.info(f"{note_prefix()} Disk mount is still in progress.")
            logger.info(f"      Wait ~{wait_time} before connecting."
                        f" If disk is not available, check:")
            logger.info(f"      - Rescue log: {rescue_log}")
            logger.info(f"      - Serial console (mount status{creds_note}):")
            logger.info(f"        $ gcloud compute instances get-serial-port-output"
                        f" {vm_name} --zone={zone} --project={project}")
            logger.info("")

        logger.info("Next Steps:")

        if orchestrator.os_type == OS_TYPE_WINDOWS:
            vm = compute.instances().get(
                project=project, zone=zone, instance=vm_name
            ).execute()
            external_ip = None
            internal_ip = None
            for iface in vm.get('networkInterfaces', []):
                if not internal_ip:
                    internal_ip = iface.get('networkIP')
                for access in iface.get('accessConfigs', []):
                    if access.get('natIP'):
                        external_ip = access.get('natIP')
                        break

            logger.info("1. Connect via RDP:")
            if external_ip:
                logger.info(f"   IP: {external_ip}")
            else:
                logger.info(f"   IP: {internal_ip} (internal - use IAP tunnel)")
                logger.info(f"   Tunnel: gcloud compute start-iap-tunnel {vm_name}"
                            f" 3389 --local-host-port=localhost:3389"
                            f" --zone={zone} --project={project}")
            logger.info(f"   User: rescue_admin")
            logger.info(f"   Password: {orchestrator.windows_rescue_password}")
            logger.info("")
            logger.info("2. Fix the issue (affected boot disk is mounted at D:\\).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name}"
                        f" --zone={zone} --project={project}")
        else:
            logger.info("1. Connect to the instance:")
            logger.info("   a. Using gcloud CLI (add --tunnel-through-iap if needed):")
            logger.info(f"      $ gcloud compute ssh {vm_name}"
                        f" --zone={zone} --project={project}")
            logger.info("   OR")
            logger.info("   b. Using Google Cloud Console:")
            logger.info(f"      https://ssh.cloud.google.com/v2/ssh/projects/{project}"
                        f"/zones/{zone}/instances/{vm_name}"
                        f"?authuser=0&hl=en_US&useAdminProxy=true")
            logger.info("")
            logger.info("2. Fix the issue (affected boot disk is mounted at /mnt/sysroot).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name}"
                        f" --zone={zone} --project={project}")

        logger.info("")

        # Format output
        if args.format != 'disable':
            result = {
                'instanceName': vm_name,
                'zone': zone,
                'project': project or 'default',
                'status': 'RESCUE_MODE',
                'operation': 'rescue',
                'success': True
            }
            print(OutputFormatter.format_output(result, args.format))

        return 0

    except Exception as e:
        error_msg = str(e)
        suggestion = get_error_suggestion(error_msg)
        if suggestion:
            logger.error(suggestion.format(
                vm_name=vm_name, zone=zone, project=project
            ))
        else:
            logger.error(f"Unexpected error: {error_msg}")
        if debug:
            logger.exception("Full traceback:")
        logger.info("")
        return 1
