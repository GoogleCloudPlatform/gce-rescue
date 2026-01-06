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
from ..core.config import RestoreConfig
from ..validators import (
    ValidationRunner,
    CredentialsValidator,
    IAMPermissionsValidator,
    VMRestoreStateValidator
)
from ..operations import (
    StopVMOperation,
    DetachDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    DeleteDiskOperation
)
from .state import StateTracker
from .rollback import RollbackHandler


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

            # Check snapshot status (warn if failed or missing)
            self._check_snapshot_status()

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
            result = stop_vm.execute(
                vm_name=self.vm_name,
                timeout=self.config.vm_stop_timeout,
                discard_local_ssd=self.config.force  # Allow stopping VMs with Local SSDs if --force
            )
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
            self._log_info("  Detaching affected disk...")
            result = detach_original.execute(vm_name=self.vm_name, device_name=self.original_device_name)
            self.state_tracker.add_operation("Detach Original Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 4: Re-attach original disk as boot
            self._log_info("  Re-attaching affected disk as boot...")
            result = attach_original.execute(vm_name=self.vm_name, disk_name=self.original_disk_name, boot=True)
            self.state_tracker.add_operation("Attach Original Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 5: Remove rescue metadata and restore backed up keys
            self._log_info("  Restoring original metadata...")
            clean_metadata = self._get_clean_metadata()
            # Use preserve_existing=False to REPLACE metadata (not merge)
            # because clean_metadata already contains the correct final state
            result = set_metadata.execute(vm_name=self.vm_name, metadata_items=clean_metadata, preserve_existing=False)
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
        """
        Get rescue and original disk information.

        Reads VM metadata to identify the original disk name, then finds both
        rescue and original disks from the VM's attached disks list.

        Raises:
            ValueError: If required disk information cannot be determined
        """
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

        # Validate we found the original disk name in metadata
        if not self.original_disk_name:
            self._log_error("Could not find 'rescue-original-disk' in VM metadata")
            self._log_error("The VM may not be in proper rescue mode, or metadata was corrupted.")
            self._log_error("")
            self._log_error("To identify your original boot disk manually:")
            self._log_error(f"  gcloud compute instances describe {self.vm_name} --zone={self.zone}")
            self._log_error("")
            self._log_error("Look for a disk that is NOT the rescue disk (doesn't contain 'rescue-disk' in name)")
            raise ValueError("Missing 'rescue-original-disk' metadata - cannot identify original boot disk")

        # Find rescue and original disks
        for disk in vm.get('disks', []):
            # Skip Local SSDs (type='SCRATCH') - they don't have a 'source' field
            if disk.get('type') == 'SCRATCH':
                continue
            source = disk.get('source', '')
            if not source:
                continue
            disk_name = source.split('/')[-1]
            device_name = disk['deviceName']

            if 'rescue-disk' in disk_name:
                self.rescue_disk_name = disk_name
                self.rescue_device_name = device_name
                self._log_debug(f"Rescue disk: {disk_name}")
            elif disk_name == self.original_disk_name:
                self.original_device_name = device_name
                self._log_debug(f"Original disk: {disk_name}")

        # Validate we found both disks
        if not self.rescue_disk_name:
            self._log_error("Could not find rescue disk attached to VM")
            self._log_error("Expected a disk with 'rescue-disk' in its name")
            raise ValueError("Rescue disk not found - VM may not be in rescue mode")

        if not self.original_device_name:
            self._log_error(f"Could not find original disk '{self.original_disk_name}' attached to VM")
            self._log_error("The original boot disk may have been detached or deleted")
            self._log_error("")
            self._log_error("Check attached disks:")
            self._log_error(f"  gcloud compute instances describe {self.vm_name} --zone={self.zone} --format='value(disks)'")
            raise ValueError(f"Original disk '{self.original_disk_name}' not found on VM")

    def _get_clean_metadata(self) -> list:
        """Get metadata with rescue items removed and backed up items restored.

        This method:
        1. Removes all rescue-related keys (rescue-mode, rescue-*, startup-script)
        2. Restores backed up keys (rescue-backup-* -> original key name)

        Example:
            Before: [rescue-backup-startup-script="user script", startup-script="rescue script", rescue-mode="123"]
            After:  [startup-script="user script"]
        """
        vm = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        metadata = vm.get('metadata', {})
        items = metadata.get('items', [])

        # Keys to remove (rescue-related)
        rescue_keys = [
            'rescue-mode',
            'startup-script',
            'windows-startup-script-ps1',
            'rescue-original-disk',
            'rescue-os-type'
        ]

        # Prefix for backed up keys
        backup_prefix = 'rescue-backup-'

        # Build clean metadata
        result = {}

        for item in items:
            key = item['key']
            value = item['value']

            if key in rescue_keys:
                # Skip rescue-related keys
                continue
            elif key.startswith(backup_prefix):
                # Restore backed up key to original name
                original_key = key[len(backup_prefix):]
                result[original_key] = value
                self._log_debug(f"  Restoring '{key}' as '{original_key}'")
            else:
                # Keep other keys as-is
                result[key] = value

        # Convert back to list format
        return [{'key': k, 'value': v} for k, v in result.items()]

    def _check_snapshot_status(self):
        """Check if safety snapshot was created successfully during rescue."""
        try:
            # List snapshots with rescue prefix for this VM
            snapshots = self.compute.snapshots().list(
                project=self.project,
                filter=f'name:pre-rescue-{self.vm_name}-*'
            ).execute()

            if not snapshots.get('items'):
                self._log_info("")
                self._log_info("  " + "=" * 56)
                self._log_info("  WARNING: No safety snapshot found!")
                self._log_info("  " + "=" * 56)
                self._log_info("  No backup was created during rescue.")
                self._log_info("  Proceed with caution - there is no recovery point.")
                self._log_info("  " + "=" * 56)
                self._log_info("")
                return

            # Get the most recent snapshot (sorted by creation time)
            snapshot_items = snapshots.get('items', [])
            most_recent = sorted(
                snapshot_items,
                key=lambda x: x.get('creationTimestamp', ''),
                reverse=True
            )[0]

            snapshot_name = most_recent.get('name')
            status = most_recent.get('status', 'UNKNOWN')

            if status == 'READY':
                self._log_info(f"  Safety snapshot verified: {snapshot_name}")
            elif status == 'CREATING':
                self._log_info("")
                self._log_info("  " + "=" * 56)
                self._log_info("  WARNING: Safety snapshot still creating!")
                self._log_info("  " + "=" * 56)
                self._log_info(f"  Snapshot: {snapshot_name}")
                self._log_info("  Status: CREATING (not yet complete)")
                self._log_info("  The snapshot may still complete successfully.")
                self._log_info("  " + "=" * 56)
                self._log_info("")
            elif status == 'FAILED':
                self._log_info("")
                self._log_info("  " + "=" * 56)
                self._log_info("  WARNING: Safety snapshot FAILED!")
                self._log_info("  " + "=" * 56)
                self._log_info(f"  Snapshot: {snapshot_name}")
                self._log_info("  Your VM will be restored, but NO BACKUP exists!")
                self._log_info("  Consider creating a manual snapshot before proceeding.")
                self._log_info("  " + "=" * 56)
                self._log_info("")
            else:
                self._log_info(f"  Safety snapshot status: {status}")

        except Exception as e:
            self._log_debug(f"Could not check snapshot status: {str(e)}")
            # Don't fail restore if we can't check snapshot status

    def _rollback(self):
        """Rollback to rescue mode."""
        self._log_error("")
        self._log_error("Operation failed, rolling back to rescue mode...")
        rollback_success = self.rollback_handler.rollback(self.state_tracker, self.operations_map)

        if not rollback_success:
            self._log_error("")
            self._log_error("=" * 60)
            self._log_error("CRITICAL: ROLLBACK FAILED!")
            self._log_error("=" * 60)
            self._log_error("The system may be in an inconsistent state.")
            self._log_error("Manual intervention is required!")
            self._log_error("")
            self._log_error("Check the following:")
            self._log_error(f"  1. VM state: gcloud compute instances describe {self.vm_name} --zone={self.zone}")
            self._log_error(f"  2. Attached disks: gcloud compute disks list --filter='users:{self.vm_name}'")
            self._log_error("")
            self._log_error("You may need to manually:")
            self._log_error("  - Reattach disks in correct order")
            self._log_error("  - Set correct boot disk")
            self._log_error("  - Start VM")
            self._log_error("=" * 60)

    def rollback(self) -> bool:
        """Public rollback method."""
        return self.rollback_handler.rollback(self.state_tracker, self.operations_map)
