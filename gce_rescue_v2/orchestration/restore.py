"""
GCE Rescue - Restore Orchestrator

Coordinates the restore workflow (exit rescue mode):
1. Validates VM is in rescue mode
2. Stops VM
3. Detaches rescue disk
4. Detaches original disk
5. Re-attaches original disk as boot
6. Removes rescue metadata
7. Starts VM
8. Deletes rescue disk
"""

import time
from core.config import RestoreConfig
from validators import (
    ValidationRunner,
    CredentialsValidator,
    IAMPermissionsValidator,
    VMRestoreStateValidator
)
from operations import (
    StopVMOperation,
    DetachDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    DeleteDiskOperation
)
from orchestration.state import StateTracker
from orchestration.rollback import RollbackHandler


class RestoreOrchestrator:
    """
    Orchestrates the restore workflow.

    This coordinates all the steps to exit rescue mode:
    1. Stop VM
    2. Detach rescue disk
    3. Detach original disk
    4. Re-attach original disk as boot
    5. Remove rescue metadata
    6. Start VM
    7. Delete rescue disk

    If anything fails, rolls back to rescue mode.

    Example:
        orchestrator = RestoreOrchestrator(
            compute=compute,
            project=project,
            zone=zone,
            vm_name='my-vm',
            config=config,
            logger=logger
        )

        if orchestrator.validate() and orchestrator.execute():
            print("Restored!")
    """

    def __init__(self, compute, project: str, zone: str, vm_name: str,
                 config: RestoreConfig = None, logger=None):
        """
        Initialize restore orchestrator.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            vm_name: Name of VM to restore
            config: Optional restore configuration
            logger: Optional logger
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RestoreConfig()
        self.logger = logger

        # State tracking
        self.state_tracker = StateTracker()
        self.rollback_handler = RollbackHandler(logger)
        self.operations_map = {}

        # Store disk info
        self.rescue_disk_name = None
        self.rescue_device_name = None
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
        - VM is in rescue mode

        Returns:
            True if all validations passed
        """

        self._log_info("Pre-flight Validation:")

        runner = ValidationRunner()

        # Add validators
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(self.compute, self.project, self.zone, self.vm_name))
        runner.add(VMRestoreStateValidator(self.compute, self.project, self.zone, self.vm_name))

        # Run validations
        results = runner.run_all(self.logger)

        if not results.all_passed():
            self._log_error("")
            self._log_error("Pre-flight validation failed!")
            results.print_failures()
            return False

        return True

    def execute(self) -> bool:
        """
        Execute the restore workflow.

        Returns:
            True if restore succeeded
        """

        self._log_info("")
        self._log_info("Executing Restore:")

        try:
            # Get disk info
            self._get_disk_info()

            # Create operations
            stop_vm = StopVMOperation(self.compute, self.project, self.zone, self.logger)
            detach_rescue = DetachDiskOperation(self.compute, self.project, self.zone, self.logger)
            detach_original = DetachDiskOperation(self.compute, self.project, self.zone, self.logger)
            attach_original = AttachDiskOperation(self.compute, self.project, self.zone, self.logger)
            set_metadata = SetMetadataOperation(self.compute, self.project, self.zone, self.logger)
            start_vm = StartVMOperation(self.compute, self.project, self.zone, self.logger)
            delete_rescue = DeleteDiskOperation(self.compute, self.project, self.zone, self.logger)

            # Build operations map
            self.operations_map = {
                "Stop VM": stop_vm,
                "Detach Rescue Disk": detach_rescue,
                "Detach Original Disk": detach_original,
                "Attach Original Disk": attach_original,
                "Set Metadata": set_metadata,
                "Start VM": start_vm
                # Note: DeleteDiskOperation NOT in map (can't rollback deletion)
            }

            # Step 1: Stop VM
            self._log_info("  Stopping VM...")
            result = stop_vm.execute(vm_name=self.vm_name, timeout=self.config.vm_stop_timeout)
            self.state_tracker.add_operation("Stop VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 2: Detach rescue disk
            self._log_info("  Detaching rescue disk...")
            result = detach_rescue.execute(vm_name=self.vm_name, device_name=self.rescue_device_name)
            self.state_tracker.add_operation("Detach Rescue Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 3: Detach original disk
            self._log_info("  Detaching original disk...")
            result = detach_original.execute(vm_name=self.vm_name, device_name=self.original_device_name)
            self.state_tracker.add_operation("Detach Original Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 4: Re-attach original disk as boot
            self._log_info("  Re-attaching original disk as boot...")
            result = attach_original.execute(vm_name=self.vm_name, disk_name=self.original_disk_name, boot=True)
            self.state_tracker.add_operation("Attach Original Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 5: Remove rescue metadata
            self._log_info("  Removing rescue metadata...")
            clean_metadata = self._get_clean_metadata()
            result = set_metadata.execute(vm_name=self.vm_name, metadata_items=clean_metadata)
            self.state_tracker.add_operation("Set Metadata", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 6: Start VM
            self._log_info("  Starting VM...")
            result = start_vm.execute(vm_name=self.vm_name, timeout=self.config.vm_start_timeout)
            self.state_tracker.add_operation("Start VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 7: Delete rescue disk (only if config allows)
            if self.config.delete_rescue_disk:
                self._log_info(f"  Deleting rescue disk...")
                result = delete_rescue.execute(disk_name=self.rescue_disk_name)
                # Note: Don't add to state tracker (can't rollback deletion)
                if result.success:
                    self._log_info(f"  [OK] {result.message}")
                else:
                    self._log_error(f"  [X] Failed to delete rescue disk: {result.error}")
                    self._log_error("  You can delete it manually later")

            return True

        except Exception as e:
            self._log_error(f"Unexpected error during restore: {str(e)}")
            self._rollback()
            return False

    def _get_disk_info(self):
        """Get rescue and original disk information."""
        vm = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        # Get original disk from metadata (more reliable)
        metadata = vm.get('metadata', {})
        for item in metadata.get('items', []):
            if item['key'] == 'rescue-original-disk':
                self.original_disk_name = item['value']
                break

        # Find rescue and original disks
        for disk in vm.get('disks', []):
            disk_name = disk['source'].split('/')[-1]
            device_name = disk['deviceName']

            if 'rescue-disk' in disk_name:
                self.rescue_disk_name = disk_name
                self.rescue_device_name = device_name
                self._log_debug(f"Rescue disk: {disk_name}")
            elif disk_name == self.original_disk_name:
                self.original_device_name = device_name
                self._log_debug(f"Original disk: {disk_name}")

    def _get_clean_metadata(self) -> list:
        """Get metadata with rescue items removed."""
        vm = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        metadata = vm.get('metadata', {})
        items = metadata.get('items', [])

        # Remove rescue-related metadata
        clean_items = [
            item for item in items
            if item['key'] not in ['rescue-mode', 'startup-script', 'rescue-original-disk']
        ]

        return clean_items

    def _rollback(self):
        """Rollback to rescue mode."""
        self._log_error("")
        self._log_error("Operation failed, rolling back to rescue mode...")
        self.rollback_handler.rollback(self.state_tracker, self.operations_map)

    def rollback(self) -> bool:
        """Public rollback method."""
        return self.rollback_handler.rollback(self.state_tracker, self.operations_map)
