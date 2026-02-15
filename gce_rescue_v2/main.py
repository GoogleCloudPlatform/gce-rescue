"""
GCE Rescue V2 - Main Entry Point

Simple, clean entry points for rescue and restore operations.

Usage:
    from gce_rescue_v2.main import rescue_vm, restore_vm

    # Rescue a VM
    success = rescue_vm('my-vm', 'us-central1-a', project='my-project')

    # Restore a VM
    success = restore_vm('my-vm', 'us-central1-a', project='my-project')
"""

from datetime import datetime

from .core.auth import AuthManager
from .core.config import RescueConfig, RestoreConfig, OS_TYPE_WINDOWS
from .core.error_messages import get_error_suggestion
from .utils.logger import setup_logging
from .utils.colors import note_prefix
from .orchestration import RescueOrchestrator, RestoreOrchestrator, RepairOrchestrator


def rescue_vm(vm_name: str, zone: str, project: str = None,
              config: RescueConfig = None, debug: bool = False,
              resume_checkpoint=None, log_file: str = None) -> bool:
    """
    Rescue a VM (enter rescue mode).

    This will:
    1. Validate credentials and permissions
    2. Stop the VM
    3. Create a rescue disk from rescue image
    4. Detach original boot disk
    5. Attach rescue disk as boot
    6. Set rescue metadata and startup script
    7. Start VM in rescue mode
    8. Re-attach original disk as secondary

    On failure, automatically rolls back to original state.
    Supports resuming from interrupted operations via resume_checkpoint.

    Args:
        vm_name: Name of VM to rescue
        zone: GCP zone (e.g., 'us-central1-a')
        project: GCP project ID (optional, uses default if not provided)
        config: Optional RescueConfig for advanced settings
        debug: Enable debug logging (default: False)
        resume_checkpoint: Optional checkpoint data to resume from

    Returns:
        True if rescue succeeded, False if failed

    Example:
        >>> rescue_vm('my-vm', 'us-central1-a', debug=True)
        True
    """

    # Setup logging - use provided log_file (resume) or generate new one
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = f"{vm_name}-rescue-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    if resume_checkpoint:
        logger.debug(f"=== Session Resumed ===")
    logger.debug(f"GCE Rescue V2 - Rescue")
    logger.debug(f"Log file: {log_file}")
    logger.debug(f"VM: {vm_name}, Zone: {zone}" + (f", Project: {project}" if project else ""))
    
    try:
        # Initialize auth
        auth = AuthManager()
        compute, project = auth.get_client(project)
        logger.debug(f"Authenticated to project: {project}")
        
        # Create config if not provided
        if config is None:
            config = RescueConfig()
        
        # Create orchestrator
        orchestrator = RescueOrchestrator(
            compute=compute,
            project=project,
            zone=zone,
            vm_name=vm_name,
            config=config,
            logger=logger,
            log_file=log_file
        )

        # Set resume state if resuming from checkpoint
        if resume_checkpoint:
            orchestrator.set_resume_state(resume_checkpoint)
            logger.debug(f"Resuming from step {resume_checkpoint.current_step + 1}")

        # Step 1: Validate (skip if resuming - already validated)
        if not resume_checkpoint and not orchestrator.validate():
            logger.error("Validation failed. Cannot proceed with rescue.")
            return False

        # Step 2: Execute
        if not orchestrator.execute():
            logger.error("Rescue failed.")
            return False

        # Success! Show output
        logger.info("")
        logger.info(f"Rescue mode enabled for instance [{vm_name}].")
        logger.info("")

        # Show disk info
        mount_path = "D:\\" if orchestrator.os_type == OS_TYPE_WINDOWS else "/mnt/sysroot"
        logger.info(f"Affected disk mounted at: {mount_path}")
        if orchestrator.snapshot_name:
            logger.info(f"Backup snapshot: {orchestrator.snapshot_name}")
        logger.info("")

        # Show startup verification note if it didn't complete in time
        if not orchestrator.verification_succeeded:
            wait_time = "1-2 minutes" if orchestrator.os_type == OS_TYPE_WINDOWS else "30 seconds"
            log_file = "C:\\gce-rescue.log" if orchestrator.os_type == OS_TYPE_WINDOWS else "/var/log/gce-rescue.log"
            logger.info(f"{note_prefix()} Disk mount is still in progress.")
            logger.info(f"      Wait ~{wait_time} before connecting. If disk is not available, check:")
            logger.info(f"      - Rescue log: {log_file}")
            logger.info(f"      - Serial console (mount status{', credentials' if orchestrator.os_type == OS_TYPE_WINDOWS else ''}):")
            logger.info(f"        $ gcloud compute instances get-serial-port-output {vm_name} --zone={zone} --project={project}")
            logger.info("")

        logger.info("Next Steps:")

        # Show OS-specific connection and mount instructions
        if orchestrator.os_type == OS_TYPE_WINDOWS:
            # Get external/internal IP for RDP connection
            vm = compute.instances().get(project=project, zone=zone, instance=vm_name).execute()
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
                logger.info(f"   Tunnel: gcloud compute start-iap-tunnel {vm_name} 3389 --local-host-port=localhost:3389 --zone={zone} --project={project}")
            logger.info(f"   User: rescue_admin")
            logger.info(f"   Password: {orchestrator.windows_rescue_password}")
            logger.info("")
            logger.info("2. Fix the issue (affected boot disk is mounted at D:\\).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name} --zone={zone} --project={project}")
        else:
            logger.info("1. Connect to the instance:")
            logger.info("   a. Using gcloud CLI (add --tunnel-through-iap if needed):")
            logger.info(f"      $ gcloud compute ssh {vm_name} --zone={zone} --project={project}")
            logger.info("   OR")
            logger.info("   b. Using Google Cloud Console:")
            logger.info(f"      https://ssh.cloud.google.com/v2/ssh/projects/{project}/zones/{zone}/instances/{vm_name}?authuser=0&hl=en_US&useAdminProxy=true")
            logger.info("")
            logger.info("2. Fix the issue (affected boot disk is mounted at /mnt/sysroot).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name} --zone={zone} --project={project}")

        logger.info("")
        return True
        
    except Exception as e:
        error_msg = str(e)
        suggestion = get_error_suggestion(error_msg)

        if suggestion:
            logger.error(suggestion.format(vm_name=vm_name, zone=zone, project=project))
        else:
            logger.error(f"Unexpected error: {error_msg}")

        if debug:
            logger.exception("Full traceback:")
        logger.info("")
        return False


def restore_vm(vm_name: str, zone: str, project: str = None,
               config: RestoreConfig = None, debug: bool = False,
               resume_checkpoint=None, log_file: str = None) -> bool:
    """
    Restore a VM (exit rescue mode).

    This will:
    1. Validate VM is in rescue mode
    2. Stop the VM
    3. Detach rescue disk
    4. Detach original disk
    5. Re-attach original disk as boot
    6. Remove rescue metadata
    7. Start VM normally
    8. Delete rescue disk (if configured)

    On failure, automatically rolls back to rescue mode.
    Supports resuming from interrupted operations via resume_checkpoint.

    Args:
        vm_name: Name of VM to restore
        zone: GCP zone (e.g., 'us-central1-a')
        project: GCP project ID (optional, uses default if not provided)
        config: Optional RestoreConfig for advanced settings
        debug: Enable debug logging (default: False)
        resume_checkpoint: Optional checkpoint data to resume from

    Returns:
        True if restore succeeded, False if failed

    Example:
        >>> restore_vm('my-vm', 'us-central1-a')
        True
    """

    # Setup logging - use provided log_file (resume) or generate new one
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = f"{vm_name}-restore-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    if resume_checkpoint:
        logger.debug(f"=== Session Resumed ===")
    logger.debug(f"GCE Rescue V2 - Restore")
    logger.debug(f"Log file: {log_file}")
    logger.debug(f"VM: {vm_name}, Zone: {zone}" + (f", Project: {project}" if project else ""))
    
    try:
        # Initialize auth
        auth = AuthManager()
        compute, project = auth.get_client(project)
        logger.debug(f"Authenticated to project: {project}")
        
        # Create config if not provided
        if config is None:
            config = RestoreConfig()
        
        # Create orchestrator
        orchestrator = RestoreOrchestrator(
            compute=compute,
            project=project,
            zone=zone,
            vm_name=vm_name,
            config=config,
            logger=logger,
            log_file=log_file
        )

        # Set resume state if resuming from checkpoint
        if resume_checkpoint:
            orchestrator.set_resume_state(resume_checkpoint)
            logger.debug(f"Resuming from step {resume_checkpoint.current_step + 1}")

        # Step 1: Validate (skip if resuming - already validated)
        if not resume_checkpoint and not orchestrator.validate():
            logger.error("Validation failed. Cannot proceed with restore.")
            logger.error("Is the VM in rescue mode?")
            return False

        # Step 2: Execute
        if not orchestrator.execute():
            logger.error("Restore failed.")
            return False

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
                pass  # Non-critical, skip if lookup fails

        # Success! Show OS-appropriate instructions
        # Check metadata for OS type (stored during rescue)
        os_type = None
        try:
            vm = compute.instances().get(
                project=project,
                zone=zone,
                instance=vm_name
            ).execute()
            # Note: rescue-os-type was removed during restore, so check disk features
            from .utils.os_detection import detect_os_type
            os_type = detect_os_type(vm)
        except Exception:
            pass  # Default to Linux instructions if detection fails

        logger.info("")
        logger.info(f"Instance [{vm_name}] restored to normal operation.")
        logger.info("")
        if os_type == OS_TYPE_WINDOWS:
            # Get external/internal IP for RDP connection
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
                logger.info(f"  Tunnel: gcloud compute start-iap-tunnel {vm_name} 3389 --local-host-port=localhost:3389 --zone={zone} --project={project}")
            else:
                logger.info(f"  $ gcloud compute instances describe {vm_name} --zone={zone} --project={project} --format=\"get(networkInterfaces[0].networkIP)\"")
            logger.info("")
            logger.info("Forgot password? Reset it:")
            logger.info(f"  $ gcloud compute reset-windows-password {vm_name} --zone={zone} --project={project}")
        else:
            logger.info("Connect to the instance:")
            logger.info("  a. Using gcloud CLI (add --tunnel-through-iap if needed):")
            logger.info(f"     $ gcloud compute ssh {vm_name} --zone={zone} --project={project}")
            logger.info("  OR")
            logger.info("  b. Using Google Cloud Console:")
            logger.info(f"     https://ssh.cloud.google.com/v2/ssh/projects/{project}/zones/{zone}/instances/{vm_name}?authuser=0&hl=en_US&useAdminProxy=true")

        if snapshot_name:
            logger.info(f"Safety snapshot still exists: {snapshot_name}")
            logger.info(f"  Delete when no longer needed:")
            logger.info(f"  $ gcloud compute snapshots delete {snapshot_name} --project={project}")

        logger.info("")
        return True

    except Exception as e:
        error_msg = str(e)
        suggestion = get_error_suggestion(error_msg)

        if suggestion:
            logger.error(suggestion.format(vm_name=vm_name, zone=zone, project=project))
        else:
            logger.error(f"Unexpected error: {error_msg}")

        if debug:
            logger.exception("Full traceback:")
        logger.info("")
        return False


def repair_vm(vm_name: str, zone: str, project: str = None,
              config: RescueConfig = None, debug: bool = False,
              log_file: str = None) -> bool:
    """
    Repair a VM (diagnose + rescue with fix + restore).

    This will:
    1. Diagnose boot issues via serial console
    2. Enter rescue mode with embedded fix script
    3. Fix detected issues (e.g., invalid fstab entries)
    4. Restore VM to normal operation

    Linux only. On failure during rescue/restore, automatic rollback applies.

    Args:
        vm_name: Name of VM to repair
        zone: GCP zone (e.g., 'us-central1-a')
        project: GCP project ID (optional, uses default if not provided)
        config: Optional RescueConfig for advanced settings
        debug: Enable debug logging (default: False)
        log_file: Optional log file path

    Returns:
        True if repair succeeded, False if failed
    """
    # Setup logging
    if not log_file:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = f"{vm_name}-repair-{timestamp}.log"
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        log_file=log_file,
        debug=debug
    )

    logger.debug(f"GCE Rescue V2 - Repair")
    logger.debug(f"Log file: {log_file}")
    logger.debug(
        f"VM: {vm_name}, Zone: {zone}"
        + (f", Project: {project}" if project else "")
    )

    try:
        auth = AuthManager()
        compute, project = auth.get_client(project)
        logger.debug(f"Authenticated to project: {project}")

        if config is None:
            config = RescueConfig()

        orchestrator = RepairOrchestrator(
            compute=compute, project=project, zone=zone,
            vm_name=vm_name, config=config, logger=logger,
            log_file=log_file
        )

        if not orchestrator.validate():
            logger.error("Validation failed.")
            return False

        diagnosis = orchestrator.diagnose()
        if diagnosis is None:
            logger.error("Diagnosis failed.")
            return False

        boot_errors = diagnosis.get('boot_errors', [])
        if not boot_errors:
            logger.info("No boot issues found. Repair not needed.")
            return True

        fixable = orchestrator.get_fixable_categories(diagnosis)
        if not fixable:
            logger.info("No automated fix available for detected issues.")
            return False

        result = orchestrator.execute(diagnosis)
        return result.get('status') in ('success', 'no_issues')

    except Exception as e:
        error_msg = str(e)
        suggestion = get_error_suggestion(error_msg)

        if suggestion:
            logger.error(
                suggestion.format(vm_name=vm_name, zone=zone, project=project)
            )
        else:
            logger.error(f"Unexpected error: {error_msg}")

        if debug:
            logger.exception("Full traceback:")
        return False


if __name__ == '__main__':
    # Quick test - you can run this file directly for testing
    import sys

    if len(sys.argv) < 4:
        print("Usage: python main.py <rescue|restore|repair> <vm_name> <zone> [project]")
        print("Example: python main.py rescue my-vm us-central1-a my-project")
        sys.exit(1)

    mode = sys.argv[1]
    vm_name = sys.argv[2]
    zone = sys.argv[3]
    project = sys.argv[4] if len(sys.argv) > 4 else None

    if mode == 'rescue':
        success = rescue_vm(vm_name, zone, project, debug=True)
    elif mode == 'restore':
        success = restore_vm(vm_name, zone, project, debug=True)
    elif mode == 'repair':
        success = repair_vm(vm_name, zone, project, debug=True)
    else:
        print(f"Unknown mode: {mode}. Use 'rescue', 'restore', or 'repair'")
        sys.exit(1)

    sys.exit(0 if success else 1)
