"""Restore command handler."""

import argparse
import sys
from datetime import datetime
from ..core.config import RestoreConfig, OS_TYPE_WINDOWS
from ..core.error_messages import get_error_suggestion
from ..utils.colors import error_prefix, clear_lines
from ..utils.logger import setup_logging
from ..orchestration.checkpoint import CheckpointManager
from ..orchestration import RestoreOrchestrator
from .output import OutputFormatter
from . import preflight
from . import checkpoint_ui


def handle_restore(args: argparse.Namespace) -> int:
    """Handle restore command."""
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

    # Check for incomplete restore operation (interactive mode only)
    checkpoint = None
    resuming = False
    if not args.quiet:
        checkpoint_mgr = CheckpointManager(compute, project, args.zone, args.instance_name)
        checkpoint = checkpoint_mgr.detect_incomplete(operation_type='restore')

        if checkpoint:
            action = checkpoint_ui._prompt_incomplete_operation(checkpoint, 'restore')

            if action == 'abort':
                return 0
            elif action == 'rollback':
                success = checkpoint_ui._handle_restore_checkpoint_rollback(
                    compute, project, args.zone, args.instance_name, checkpoint
                )
                return 0 if success else 1
            resuming = True

    if not resuming:
        # Validate VM exists and is in rescue mode BEFORE confirmation
        valid, vm_info, error_msg = preflight._validate_vm_for_restore(
            compute, project, args.zone, args.instance_name
        )
        if not valid:
            print(f"{error_prefix()} {error_msg}", file=sys.stderr)
            return 1

    # Interactive confirmation (unless --quiet or resuming)
    if not args.quiet and not resuming:
        lines_printed = 0

        print(f"\nYou are about to restore instance [{args.instance_name}] in zone"
              f" [{args.zone}] project [{project}].")
        lines_printed += 2
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
        lines_printed += 1

        if response not in ('y', 'yes'):
            print("\nAborted by user.")
            return 0

        clear_lines(lines_printed)

    # Convert to config
    from . import args_to_restore_config
    config = args_to_restore_config(args)

    # Setup logging
    debug = args.verbosity == 'debug'
    log_file = checkpoint.context.get('log_file') if checkpoint else None
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = f"{args.instance_name}-restore-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    vm_name = args.instance_name
    zone = args.zone

    if checkpoint:
        logger.debug("=== Session Resumed ===")
    logger.debug(f"GCE Rescue V2 - Restore")
    logger.debug(f"Log file: {log_file}")
    logger.debug(f"VM: {vm_name}, Zone: {zone}, Project: {project}")

    try:
        # Create orchestrator
        orchestrator = RestoreOrchestrator(
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
            logger.error("Validation failed. Cannot proceed with restore.")
            logger.error("Is the VM in rescue mode?")
            return 1

        # Execute
        if not orchestrator.execute():
            logger.error("Restore failed.")
            return 1

        # Look up safety snapshot from rescue phase
        snapshot_name = None
        if orchestrator.original_disk_name:
            try:
                snap_resp = compute.snapshots().list(
                    project=project,
                    filter=f'name:pre-rescue-{orchestrator.original_disk_name}-*'
                ).execute()
                snap_items = snap_resp.get('items', [])
                if snap_items:
                    snap_items.sort(
                        key=lambda s: s.get('creationTimestamp', ''),
                        reverse=True
                    )
                    snapshot_name = snap_items[0].get('name')
            except Exception:
                pass

        # Detect OS for appropriate connection instructions
        os_type = None
        vm = None
        try:
            vm = compute.instances().get(
                project=project, zone=zone, instance=vm_name
            ).execute()
            from ..utils.os_detection import detect_os_type
            os_type = detect_os_type(vm)
        except Exception:
            pass

        # Success output
        logger.info("")
        logger.info(f"Instance [{vm_name}] restored to normal operation.")
        logger.info("")

        if os_type == OS_TYPE_WINDOWS:
            external_ip = None
            internal_ip = None
            try:
                for iface in vm.get('networkInterfaces', []):
                    if not internal_ip:
                        internal_ip = iface.get('networkIP')
                    for access in iface.get('accessConfigs', []):
                        if access.get('natIP'):
                            external_ip = access.get('natIP')
                            break
            except Exception:
                pass

            logger.info("Connect via RDP using your original credentials:")
            if external_ip:
                logger.info(f"  IP: {external_ip}")
            elif internal_ip:
                logger.info(f"  IP: {internal_ip} (internal - use IAP tunnel)")
                logger.info(f"  Tunnel: gcloud compute start-iap-tunnel {vm_name}"
                            f" 3389 --local-host-port=localhost:3389"
                            f" --zone={zone} --project={project}")
            else:
                logger.info(f"  $ gcloud compute instances describe {vm_name}"
                            f" --zone={zone} --project={project}"
                            f' --format="get(networkInterfaces[0].networkIP)"')
            logger.info("")
            logger.info("Forgot password? Reset it:")
            logger.info(f"  $ gcloud compute reset-windows-password {vm_name}"
                        f" --zone={zone} --project={project}")
        else:
            logger.info("Connect to the instance:")
            logger.info("  a. Using gcloud CLI (add --tunnel-through-iap if needed):")
            logger.info(f"     $ gcloud compute ssh {vm_name}"
                        f" --zone={zone} --project={project}")
            logger.info("  OR")
            logger.info("  b. Using Google Cloud Console:")
            logger.info(f"     https://ssh.cloud.google.com/v2/ssh/projects/{project}"
                        f"/zones/{zone}/instances/{vm_name}"
                        f"?authuser=0&hl=en_US&useAdminProxy=true")

        if snapshot_name:
            logger.info(f"Safety snapshot still exists: {snapshot_name}")
            logger.info(f"  Delete when no longer needed:")
            logger.info(f"  $ gcloud compute snapshots delete {snapshot_name}"
                        f" --project={project}")

        logger.info("")

        # Format output
        if args.format != 'disable':
            result = {
                'instanceName': vm_name,
                'zone': zone,
                'project': project or 'default',
                'status': 'RUNNING',
                'operation': 'restore',
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
