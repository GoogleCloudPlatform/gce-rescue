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

from core.auth import AuthManager
from core.config import RescueConfig, RestoreConfig
from utils.logger import setup_logging
from orchestration import RescueOrchestrator, RestoreOrchestrator


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
    
    logger.info("=" * 60)
    logger.info("GCE Rescue V2 - Rescue Mode")
    logger.info("=" * 60)
    logger.info(f"VM: {vm_name}")
    logger.info(f"Zone: {zone}")
    if project:
        logger.info(f"Project: {project}")
    logger.info("")
    
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
        logger.info("")
        if not orchestrator.validate():
            logger.error("")
            logger.error("Validation failed. Cannot proceed with rescue.")
            return False
        
        # Step 2: Execute
        logger.info("")
        if not orchestrator.execute():
            logger.error("")
            logger.error("Rescue failed.")
            return False
        
        # Success!
        logger.info("")
        logger.info("=" * 60)
        logger.info("[OK] Rescue completed successfully!")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Your VM is now in rescue mode.")
        logger.info(f"Connect via SSH: gcloud compute ssh {vm_name} --zone={zone}")
        logger.info(f"Original disk mounted at: /mnt/sysroot")
        logger.info("")
        logger.info("When done, run restore to exit rescue mode:")
        logger.info(f"  restore_vm('{vm_name}', '{zone}')")
        logger.info("")
        
        return True
        
    except Exception as e:
        logger.error("")
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
    
    logger.info("=" * 60)
    logger.info("GCE Rescue V2 - Restore Mode")
    logger.info("=" * 60)
    logger.info(f"VM: {vm_name}")
    logger.info(f"Zone: {zone}")
    if project:
        logger.info(f"Project: {project}")
    logger.info("")
    
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
        logger.info("")
        if not orchestrator.validate():
            logger.error("")
            logger.error("Validation failed. Cannot proceed with restore.")
            logger.error("Is the VM in rescue mode?")
            return False
        
        # Step 2: Execute
        logger.info("")
        if not orchestrator.execute():
            logger.error("")
            logger.error("Restore failed.")
            return False
        
        # Success!
        logger.info("")
        logger.info("=" * 60)
        logger.info("[OK] Restore completed successfully!")
        logger.info("=" * 60)
        logger.info("")
        logger.info("Your VM has been restored to normal operation.")
        logger.info(f"Connect via SSH: gcloud compute ssh {vm_name} --zone={zone}")
        logger.info("")
        
        return True
        
    except Exception as e:
        logger.error("")
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
