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

from .core.auth import AuthManager
from .core.config import RescueConfig, RestoreConfig, OS_TYPE_WINDOWS
from .utils.logger import setup_logging
from .orchestration import RescueOrchestrator, RestoreOrchestrator


def rescue_vm(vm_name: str, zone: str, project: str = None,
              config: RescueConfig = None, debug: bool = False) -> bool:
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
    
    Args:
        vm_name: Name of VM to rescue
        zone: GCP zone (e.g., 'us-central1-a')
        project: GCP project ID (optional, uses default if not provided)
        config: Optional RescueConfig for advanced settings
        debug: Enable debug logging (default: False)
    
    Returns:
        True if rescue succeeded, False if failed
    
    Example:
        >>> rescue_vm('my-vm', 'us-central1-a', debug=True)
        True
    """
    
    # Setup logging
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        debug=debug
    )
    
    logger.debug(f"GCE Rescue V2 - Rescue")
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
            logger=logger
        )
        
        # Step 1: Validate
        if not orchestrator.validate():
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

        # Show verification timeout warning if applicable
        if not orchestrator.verification_succeeded:
            logger.info("Note: Startup verification timed out. Check rescue logs:")
            logger.info(f"  $ gcloud compute instances get-serial-port-output {vm_name} --zone={zone}")
            logger.info("")

        logger.info("Next Steps:")

        # Show OS-specific connection and mount instructions
        if orchestrator.os_type == OS_TYPE_WINDOWS:
            # Get external IP for RDP connection
            vm = compute.instances().get(project=project, zone=zone, instance=vm_name).execute()
            external_ip = None
            for iface in vm.get('networkInterfaces', []):
                for access in iface.get('accessConfigs', []):
                    if access.get('natIP'):
                        external_ip = access.get('natIP')
                        break

            ip_str = external_ip if external_ip else "N/A"
            logger.info("1. Connect via RDP:")
            logger.info(f"   IP: {ip_str}")
            logger.info(f"   User: rescue_admin")
            logger.info(f"   Pass: {orchestrator.windows_rescue_password}")
            logger.info("")
            logger.info("2. Fix the issue (affected disk is mounted at D:\\).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name} --zone={zone}")
        else:
            logger.info("1. Connect to the instance:")
            logger.info(f"   $ gcloud compute ssh {vm_name} --zone={zone}")
            logger.info("")
            logger.info("2. Fix the issue (affected disk is mounted at /mnt/sysroot).")
            logger.info("")
            logger.info("3. Restore original configuration:")
            logger.info(f"   $ gce-rescue-v2 restore {vm_name} --zone={zone}")

        return True
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        if debug:
            logger.exception("Full traceback:")
        return False


def restore_vm(vm_name: str, zone: str, project: str = None,
               config: RestoreConfig = None, debug: bool = False) -> bool:
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
    
    Args:
        vm_name: Name of VM to restore
        zone: GCP zone (e.g., 'us-central1-a')
        project: GCP project ID (optional, uses default if not provided)
        config: Optional RestoreConfig for advanced settings
        debug: Enable debug logging (default: False)
    
    Returns:
        True if restore succeeded, False if failed
    
    Example:
        >>> restore_vm('my-vm', 'us-central1-a')
        True
    """
    
    # Setup logging
    logger = setup_logging(
        level='DEBUG' if debug else 'INFO',
        debug=debug
    )
    
    logger.debug(f"GCE Rescue V2 - Restore")
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
            logger=logger
        )
        
        # Step 1: Validate
        if not orchestrator.validate():
            logger.error("Validation failed. Cannot proceed with restore.")
            logger.error("Is the VM in rescue mode?")
            return False

        # Step 2: Execute
        if not orchestrator.execute():
            logger.error("Restore failed.")
            return False

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
        logger.info("Connect to the instance:")
        if os_type == OS_TYPE_WINDOWS:
            logger.info(f"  $ gcloud compute reset-windows-password {vm_name} --zone={zone}")
        else:
            logger.info(f"  $ gcloud compute ssh {vm_name} --zone={zone}")

        return True
        
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        if debug:
            logger.exception("Full traceback:")
        return False


if __name__ == '__main__':
    # Quick test - you can run this file directly for testing
    import sys
    
    if len(sys.argv) < 4:
        print("Usage: python main.py <rescue|restore> <vm_name> <zone> [project]")
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
    else:
        print(f"Unknown mode: {mode}. Use 'rescue' or 'restore'")
        sys.exit(1)
    
    sys.exit(0 if success else 1)
