"""
GCE Rescue - Rescue Orchestrator

Coordinates the rescue workflow:
1. Validates (credentials, permissions, VM state)
2. Executes operations in sequence
3. Tracks state
4. Rolls back on failure
"""

import time
from core.config import RescueConfig
from validators import (
    ValidationRunner,
    CredentialsValidator,
    IAMPermissionsValidator,
    VMStateValidator
)
from operations import (
    StopVMOperation,
    CreateDiskOperation,
    DetachDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    CreateSnapshotOperation
)
from orchestration.state import StateTracker
from orchestration.rollback import RollbackHandler


class RescueOrchestrator:
    """
    Orchestrates the rescue workflow.

    This coordinates all the steps:
    1. Pre-flight validation
    2. Stop VM
    3. Create rescue disk
    4. Detach boot disk
    5. Attach rescue disk as boot
    6. Set rescue metadata
    7. Start VM in rescue mode
    8. Re-attach original disk as secondary

    If anything fails, automatically rolls back to original state.

    Example:
        orchestrator = RescueOrchestrator(
            compute=compute,
            project=project,
            zone=zone,
            vm_name='my-vm',
            config=config,
            logger=logger
        )

        # Validate
        if not orchestrator.validate():
            return False

        # Execute
        if not orchestrator.execute():
            return False
    """

    def __init__(self, compute, project: str, zone: str, vm_name: str,
                 config: RescueConfig = None, logger=None):
        """
        Initialize rescue orchestrator.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            vm_name: Name of VM to rescue
            config: Optional rescue configuration
            logger: Optional logger
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RescueConfig()
        self.logger = logger

        # State tracking
        self.state_tracker = StateTracker()
        self.rollback_handler = RollbackHandler(logger)
        self.operations_map = {}

        # Store original disk info
        self.original_disk_name = None
        self.original_device_name = None

    def _log_info(self, message: str):
        """Log info message."""
        if self.logger:
            self.logger.info(message)

    def _log_debug(self, message: str):
        """Log debug message."""
        if self.logger:
            self.logger.debug(message)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def validate(self) -> bool:
        """
        Run pre-flight validation.

        Checks:
        - Credentials
        - IAM permissions
        - VM state

        Returns:
            True if all validations passed
        """

        self._log_info("Pre-flight Validation:")
        self._log_debug("Creating validation runner")

        runner = ValidationRunner()

        # Add validators
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(self.compute, self.project, self.zone, self.vm_name))
        runner.add(VMStateValidator(self.compute, self.project, self.zone, self.vm_name))

        # Run validations
        results = runner.run_all(self.logger)

        if not results.all_passed():
            self._log_error("")
            self._log_error("Pre-flight validation failed!")
            results.print_failures()
            return False

        self._log_debug("All validations passed")
        return True

    def execute(self) -> bool:
        """
        Execute the rescue workflow.

        Returns:
            True if rescue succeeded, False if failed
        """

        self._log_info("")
        self._log_info("Executing Rescue:")
        self._log_debug(f"Config: {self.config}")

        try:
            # Generate rescue disk name
            rescue_disk_name = f"rescue-disk-{int(time.time())}"
            self._log_debug(f"Rescue disk name: {rescue_disk_name}")

            # Get original disk info
            self._get_original_disk_info()

            # Create operations
            create_snapshot = CreateSnapshotOperation(self.compute, self.project, self.zone, self.logger)
            stop_vm = StopVMOperation(self.compute, self.project, self.zone, self.logger)
            create_disk = CreateDiskOperation(self.compute, self.project, self.zone, self.logger)
            detach_boot = DetachDiskOperation(self.compute, self.project, self.zone, self.logger)
            attach_rescue = AttachDiskOperation(self.compute, self.project, self.zone, self.logger)
            set_metadata = SetMetadataOperation(self.compute, self.project, self.zone, self.logger)
            start_vm = StartVMOperation(self.compute, self.project, self.zone, self.logger)
            attach_original = AttachDiskOperation(self.compute, self.project, self.zone, self.logger)

            # Build operations map for rollback
            self.operations_map = {
                "Create Snapshot": create_snapshot,
                "Stop VM": stop_vm,
                "Create Rescue Disk": create_disk,
                "Detach Boot Disk": detach_boot,
                "Attach Rescue Disk": attach_rescue,
                "Set Metadata": set_metadata,
                "Start VM": start_vm,
                "Attach Original Disk": attach_original
            }

            # Step 0: Create safety snapshot (if enabled)
            if self.config.create_snapshot:
                self._log_info("  Creating safety snapshot...")
                self._log_info("    (This takes 2-5 minutes but ensures data safety)")
                result = create_snapshot.execute(
                    disk_name=self.original_disk_name,
                    snapshot_name=None,  # Auto-generate
                    description=f"Pre-rescue safety snapshot of {self.vm_name}",
                    timeout=self.config.snapshot_timeout
                )
                self.state_tracker.add_operation("Create Snapshot", result.success, result.message, result.rollback_data)

                if not result.success:
                    self._log_error("  Failed to create safety snapshot")
                    if self.config.require_snapshot:
                        self._log_error("  Snapshot required but failed. Aborting.")
                        return False
                    else:
                        self._log_error("  Continuing without snapshot (risky!)")
                else:
                    snapshot_name = result.rollback_data.get('snapshot_name')
                    self._log_info(f"  [OK] {result.message}")
                    self._log_info(f"    Snapshot: {snapshot_name}")

            # Step 1: Stop VM
            self._log_info("  Stopping VM...")
            result = stop_vm.execute(vm_name=self.vm_name, timeout=self.config.vm_stop_timeout)
            self.state_tracker.add_operation("Stop VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 2: Create rescue disk
            self._log_info("  Creating rescue disk...")
            result = create_disk.execute(
                disk_name=rescue_disk_name,
                size_gb=self.config.rescue_disk_size_gb,
                disk_type=self.config.rescue_disk_type,
                source_image=f'projects/{self.config.rescue_image_project}/global/images/family/{self.config.rescue_image_family}',
                timeout=self.config.disk_create_timeout
            )
            self.state_tracker.add_operation("Create Rescue Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 3: Detach boot disk
            self._log_info("  Detaching boot disk...")
            result = detach_boot.execute(vm_name=self.vm_name, device_name=self.original_device_name)
            self.state_tracker.add_operation("Detach Boot Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 4: Attach rescue disk as boot
            self._log_info("  Attaching rescue disk as boot...")
            result = attach_rescue.execute(vm_name=self.vm_name, disk_name=rescue_disk_name, boot=True)
            self.state_tracker.add_operation("Attach Rescue Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 5: Set rescue metadata
            self._log_info("  Setting rescue metadata...")
            startup_script = self._generate_startup_script()
            metadata_items = [
                {'key': 'startup-script', 'value': startup_script},
                {'key': 'rescue-mode', 'value': str(int(time.time()))},
                {'key': 'rescue-original-disk', 'value': self.original_disk_name}
            ]
            result = set_metadata.execute(vm_name=self.vm_name, metadata_items=metadata_items)
            self.state_tracker.add_operation("Set Metadata", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 6: Start VM in rescue mode
            self._log_info("  Starting VM in rescue mode...")
            result = start_vm.execute(vm_name=self.vm_name, timeout=self.config.vm_start_timeout)
            self.state_tracker.add_operation("Start VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 7: Re-attach original disk as secondary
            self._log_info("  Re-attaching original disk...")
            time.sleep(10)  # Wait for VM to fully boot
            result = attach_original.execute(vm_name=self.vm_name, disk_name=self.original_disk_name, boot=False)
            self.state_tracker.add_operation("Attach Original Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Success!
            return True

        except Exception as e:
            self._log_error(f"Unexpected error during rescue: {str(e)}")
            self._rollback()
            return False

    def _get_original_disk_info(self):
        """Get original boot disk information."""
        vm = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        for disk in vm.get('disks', []):
            if disk.get('boot'):
                self.original_disk_name = disk['source'].split('/')[-1]
                self.original_device_name = disk['deviceName']
                self._log_debug(f"Original disk: {self.original_disk_name}")
                break

    def _generate_startup_script(self) -> str:
        """Generate startup script for rescue mode."""
        from pathlib import Path

        # Use V2's own startup script template
        script_file = Path(__file__).parent.parent / 'startup_scripts' / 'rescue_mount.sh'

        if script_file.exists():
            with open(script_file, 'r') as f:
                script = f.read()
                # Replace disk name placeholder
                script = script.replace('DISK_NAME_PLACEHOLDER', self.original_disk_name)
                return script

        # Fallback: inline script if template not found
        self._log_error("Warning: Startup script template not found, using fallback")
        return f"""#!/bin/bash
# GCE Rescue Mode - Fallback Script
LOGFILE="/var/log/gce-rescue.log"

log() {{
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOGFILE"
}}

log "=== GCE Rescue Auto-Mount Started ==="

# Change hostname
hostname $(hostname)-rescue
log "Hostname changed to: $(hostname)"

# Mount disk
disk={self.original_disk_name}
mkdir -p /mnt/sysroot

attempt=0
while [ ! -e /dev/disk/by-id/google-${{disk}} ]; do
    attempt=$((attempt + 1))
    log "Waiting for disk... attempt $attempt"
    sleep 5
    [ $attempt -gt 60 ] && log "ERROR: Disk not found" && exit 1
done

log "Disk found, mounting..."
disk_p=$(lsblk -rf /dev/disk/by-id/google-${{disk}} | egrep -i 'ext[3-4]|xfs|microsoft' | head -1)

if [ -n "$disk_p" ]; then
    mount /dev/${{disk_p%% *}} /mnt/sysroot && log "SUCCESS: Mounted" || log "ERROR: Mount failed"

    # Mount for chroot
    [ -d /mnt/sysroot/proc ] && mount -t proc proc /mnt/sysroot/proc
    [ -d /mnt/sysroot/sys ] && mount -t sysfs sys /mnt/sysroot/sys
    [ -d /mnt/sysroot/dev ] && mount -o bind /dev /mnt/sysroot/dev

    log "=== Mount Complete ==="
else
    log "ERROR: No filesystem found"
    exit 1
fi
"""

    def _rollback(self):
        """Rollback all operations."""
        self._log_error("")
        self._log_error("Operation failed, rolling back...")
        self.rollback_handler.rollback(self.state_tracker, self.operations_map)

    def rollback(self) -> bool:
        """Public rollback method."""
        return self.rollback_handler.rollback(self.state_tracker, self.operations_map)
