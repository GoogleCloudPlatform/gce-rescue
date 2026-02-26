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
from .checkpoint import CheckpointManager, CheckpointData


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
                 config: RestoreConfig = None, logger=None, log_file: str = None,
                 suppress_progress: bool = False, progress_callback=None):
        """
        Initialize restore orchestrator.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            vm_name: Name of VM to restore
            config: Optional restore configuration
            logger: Optional logger
            log_file: Log file path (for checkpoint persistence)
            suppress_progress: Suppress progress spinner (for embedding in repair)
            progress_callback: Optional callback(phase_label) invoked on each step
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RestoreConfig()
        self.logger = logger
        self.log_file = log_file
        self._suppress_progress = suppress_progress
        self._progress_callback = progress_callback

        # State tracking
        self.state_tracker = StateTracker()
        self.rollback_handler = RollbackHandler(logger)
        self.operations_map = {}

        # Store disk info
        self.rescue_disk_name = None
        self.rescue_device_name = None
        self.original_disk_name = None
        self.original_device_name = None

        # Progress tracking for spinner with phases
        import threading
        self._spinner_thread = None
        self._spinner_stop = False
        self._is_debug_mode = False
        self._progress_started = False
        self._progress_phases = []
        self._progress_lock = threading.Lock()
        self._total_steps = 3  # Stopping, Restoring affected disk, Starting

        # Checkpoint manager for resumable operations
        self.checkpoint_manager = CheckpointManager(
            compute, project, zone, vm_name, logger
        )

        # Resume state (set when resuming from checkpoint)
        self._resume_from_step = 0  # 0 = fresh start, >0 = resume from this step
        self._resumed_context = None  # Context from checkpoint

    def _init_progress(self):
        """Initialize spinner with phases display."""
        import sys
        import logging
        import threading

        if self._suppress_progress:
            self._progress_started = False
            return

        # Check if we're in debug mode (use console_level if available, fallback to logger.level)
        console_level = getattr(self.logger, 'console_level', self.logger.level) if self.logger else logging.INFO
        self._is_debug_mode = console_level <= logging.DEBUG
        self._progress_phases = []
        self._progress_lock = threading.Lock()

        # If resuming, initialize phases from completed steps
        if self._resume_from_step > 0:
            # Map step numbers to phases (phases are added at these steps)
            # Step 1: Stopping, Step 2: Restoring affected disk, Step 6: Starting
            step_to_phase = {
                1: "Stopping",
                2: "Restoring affected disk",
                6: "Starting"
            }
            for step in sorted(step_to_phase.keys()):
                if step <= self._resume_from_step:
                    self._progress_phases.append(step_to_phase[step])

        if not self._is_debug_mode:
            # Print header line
            sys.stdout.write(f"Restoring instance [{self.vm_name}]:\n")
            sys.stdout.flush()
            # Start spinner thread
            self._spinner_stop = False
            self._spinner_thread = threading.Thread(target=self._run_spinner, daemon=True)
            self._spinner_thread.start()

        self._progress_started = True

    def _run_spinner(self):
        """Run spinner animation with phases in background thread."""
        import sys
        import time

        dots = ['.  ', '.. ', '...']
        idx = 0

        while not self._spinner_stop:
            with self._progress_lock:
                current_step = len(self._progress_phases)
                if self._progress_phases:
                    # Show completed phases, then current phase with dots
                    completed = self._progress_phases[:-1]
                    current = self._progress_phases[-1]
                    if completed:
                        phases_str = " -> ".join(completed) + " -> " + current + dots[idx]
                    else:
                        phases_str = current + dots[idx]
                else:
                    phases_str = dots[idx]

            line = f"\r ({current_step}/{self._total_steps}) [{phases_str}"
            sys.stdout.write(line)
            sys.stdout.flush()
            idx = (idx + 1) % len(dots)
            time.sleep(0.4)

    def _update_progress(self, phase: str):
        """Add a new phase to the progress display."""
        with self._progress_lock:
            self._progress_phases.append(phase)
        if self._progress_callback:
            self._progress_callback(phase)
        self._log_debug(f"Phase: {phase}")

    def _finish_progress(self, success: bool = True):
        """Finish spinner display with final status."""
        import sys

        if not self._progress_started:
            return

        # Stop spinner thread
        self._spinner_stop = True
        if self._spinner_thread:
            self._spinner_thread.join(timeout=0.5)

        if not self._is_debug_mode:
            with self._progress_lock:
                phases_str = " -> ".join(self._progress_phases)
                current_step = len(self._progress_phases)

            if success:
                sys.stdout.write(f"\r ({self._total_steps}/{self._total_steps}) [{phases_str}] done.\n")
            else:
                sys.stdout.write(f"\r ({current_step}/{self._total_steps}) [{phases_str}] FAILED.\n")
            sys.stdout.flush()

    def _log_info(self, message: str):
        """Log info message."""
        if self.logger:
            self.logger.info(message)

    def _log_debug(self, message: str):
        """Log debug message with component prefix."""
        if self.logger:
            self.logger.debug(f"[Restore] {message}", stacklevel=2)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def set_resume_state(self, checkpoint: CheckpointData):
        """
        Set resume state from a checkpoint.

        Call this before execute() to resume from an interrupted operation.

        Args:
            checkpoint: Checkpoint data from previous interrupted operation
        """
        self._resume_from_step = checkpoint.current_step
        self._resumed_context = checkpoint.context
        self.checkpoint_manager.set_session_id(checkpoint.session_id)

        # Restore context from checkpoint
        if self._resumed_context:
            self.rescue_disk_name = self._resumed_context.get('rescue_disk_name')
            self.rescue_device_name = self._resumed_context.get('rescue_device_name')
            self.original_disk_name = self._resumed_context.get('original_disk_name')
            self.original_device_name = self._resumed_context.get('original_device_name')

        self._log_debug(f"Resume state set: continuing from step {self._resume_from_step + 1}")

    def _should_skip_step(self, step: int) -> bool:
        """Check if a step should be skipped (already completed in previous session)."""
        return step <= self._resume_from_step

    def _is_disk_attached(self, disk_name: str) -> bool:
        """Check if a disk is attached to the VM."""
        try:
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()
            for disk in vm.get('disks', []):
                if disk.get('source', '').endswith(f'/disks/{disk_name}'):
                    return True
            return False
        except Exception:
            return False

    def _is_disk_boot(self, disk_name: str) -> bool:
        """Check if a disk is attached as boot disk."""
        try:
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()
            for disk in vm.get('disks', []):
                if disk.get('source', '').endswith(f'/disks/{disk_name}') and disk.get('boot'):
                    return True
            return False
        except Exception:
            return False

    def _disk_exists(self, disk_name: str) -> bool:
        """Check if a disk exists."""
        try:
            self.compute.disks().get(
                project=self.project,
                zone=self.zone,
                disk=disk_name
            ).execute()
            return True
        except Exception:
            return False

    def _get_vm_status(self) -> str:
        """Get current VM status."""
        try:
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()
            return vm.get('status', 'UNKNOWN')
        except Exception:
            return 'UNKNOWN'

    def _get_checkpoint_context(self) -> dict:
        """Get context dict for checkpoint."""
        return {
            'vm_name': self.vm_name,
            'zone': self.zone,
            'rescue_disk_name': self.rescue_disk_name,
            'rescue_device_name': self.rescue_device_name,
            'original_disk_name': self.original_disk_name,
            'original_device_name': self.original_device_name,
            'log_file': self.log_file
        }

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

        runner = ValidationRunner()

        # Add validators with tracking labels
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label='restore-val-iam'))
        runner.add(VMRestoreStateValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label='restore-val-vm-state'))

        # Run validations
        results = runner.run_all(self.logger)

        if not results.all_passed():
            results.print_failures()
            return False

        self._log_debug("All validations passed")
        return True

    def execute(self) -> bool:
        """
        Execute the restore workflow.

        Returns:
            True if restore succeeded
        """

        self._log_debug(f"Restoring instance '{self.vm_name}'...")

        # Initialize progress display
        self._init_progress()

        # Total internal steps for checkpoint tracking
        total_checkpoint_steps = 7  # Stop, DetachRescue, DetachOrig, AttachOrig, SetMeta, Start, DeleteRescue

        try:
            # Get disk info (before progress display) if not resuming
            if not self._resume_from_step or not self.rescue_disk_name:
                self._get_disk_info()

            # Check snapshot status (warn if failed or missing)
            if not self._resume_from_step:
                self._check_snapshot_status()

            # Create initial checkpoint if not resuming
            if not self._resume_from_step:
                self.checkpoint_manager.create_checkpoint(
                    operation_type='restore',
                    total_steps=total_checkpoint_steps,
                    context=self._get_checkpoint_context()
                )

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
            if not self._should_skip_step(1):
                self._update_progress("Stopping")
                self._log_debug(f"Stopping instance {self.vm_name}...")
                # Auto-detect Local SSDs and handle automatically
                has_local_ssd = any(
                    disk.get('type') == 'SCRATCH'
                    for disk in self.compute.instances().get(
                        project=self.project, zone=self.zone, instance=self.vm_name
                    ).execute().get('disks', [])
                )
                result = stop_vm.execute(
                    vm_name=self.vm_name,
                    timeout=self.config.vm_stop_timeout,
                    discard_local_ssd=has_local_ssd,
                    tracking_label='restore-vm-stop'
                )
                self.state_tracker.add_operation("Stop VM", result.success, result.message, result.rollback_data, step_number=1)
                if not result.success:
                    self._finish_progress(False)
                    self._rollback()
                    return False
                # Update checkpoint
                self.checkpoint_manager.update_checkpoint(
                    step=1, operation_name="Stop VM",
                    rollback_data=result.rollback_data,
                    context_updates=self._get_checkpoint_context()
                )
            else:
                self._log_debug("Skipping Step 1 (Stop VM) - already completed")

            # Step 2: Detach rescue disk
            if not self._should_skip_step(2):
                self._update_progress("Restoring affected disk")
                # Idempotency check: skip if rescue disk already detached
                if not self._is_disk_attached(self.rescue_disk_name):
                    self._log_debug("Skipping Step 2 (Detach Rescue Disk) - disk already detached")
                    self.checkpoint_manager.update_checkpoint(
                        step=2, operation_name="Detach Rescue Disk",
                        rollback_data={'vm_name': self.vm_name, 'disk_name': self.rescue_disk_name}
                    )
                else:
                    self._log_debug("Detaching rescue disk...")
                    result = detach_rescue.execute(
                        vm_name=self.vm_name,
                        device_name=self.rescue_device_name,
                        tracking_label='restore-disk-detach-rescue'
                    )
                    self.state_tracker.add_operation("Detach Rescue Disk", result.success, result.message, result.rollback_data, step_number=2)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=2, operation_name="Detach Rescue Disk",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 2 (Detach Rescue Disk) - already completed")

            # Step 3: Detach original disk
            if not self._should_skip_step(3):
                # Idempotency check: skip if original disk already detached
                if not self._is_disk_attached(self.original_disk_name):
                    self._log_debug("Skipping Step 3 (Detach Original Disk) - disk already detached")
                    self.checkpoint_manager.update_checkpoint(
                        step=3, operation_name="Detach Original Disk",
                        rollback_data={'vm_name': self.vm_name, 'disk_name': self.original_disk_name}
                    )
                else:
                    self._log_debug("Detaching original boot disk...")
                    result = detach_original.execute(
                        vm_name=self.vm_name,
                        device_name=self.original_device_name,
                        tracking_label='restore-disk-detach-orig'
                    )
                    self.state_tracker.add_operation("Detach Original Disk", result.success, result.message, result.rollback_data, step_number=3)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=3, operation_name="Detach Original Disk",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 3 (Detach Original Disk) - already completed")

            # Step 4: Re-attach original disk as boot
            if not self._should_skip_step(4):
                # Idempotency check: skip if original disk already attached as boot
                if self._is_disk_boot(self.original_disk_name):
                    self._log_debug("Skipping Step 4 (Attach Original Disk) - disk already attached as boot")
                    self.checkpoint_manager.update_checkpoint(
                        step=4, operation_name="Attach Original Disk",
                        rollback_data={'vm_name': self.vm_name, 'disk_name': self.original_disk_name}
                    )
                else:
                    self._log_debug("Attaching boot disk...")
                    result = attach_original.execute(
                        vm_name=self.vm_name,
                        disk_name=self.original_disk_name,
                        boot=True,
                        tracking_label='restore-disk-attach-orig'
                    )
                    self.state_tracker.add_operation("Attach Original Disk", result.success, result.message, result.rollback_data, step_number=4)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=4, operation_name="Attach Original Disk",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 4 (Attach Original Disk) - already completed")

            # Step 5: Remove rescue metadata and restore backed up keys
            if not self._should_skip_step(5):
                self._log_debug("Removing rescue metadata...")
                clean_metadata = self._get_clean_metadata()
                result = set_metadata.execute(
                    vm_name=self.vm_name,
                    metadata_items=clean_metadata,
                    preserve_existing=False,
                    tracking_label='restore-meta-restore-orig'
                )
                self.state_tracker.add_operation("Set Metadata", result.success, result.message, result.rollback_data, step_number=5)
                if not result.success:
                    self._finish_progress(False)
                    self._rollback()
                    return False
                # Update checkpoint
                self.checkpoint_manager.update_checkpoint(
                    step=5, operation_name="Set Metadata",
                    rollback_data=result.rollback_data
                )
            else:
                self._log_debug("Skipping Step 5 (Set Metadata) - already completed")

            # Step 6: Start VM
            if not self._should_skip_step(6):
                self._update_progress("Starting")
                # Idempotency check: skip if VM is already running
                vm_status = self._get_vm_status()
                if vm_status == 'RUNNING':
                    self._log_debug("Skipping Step 6 (Start VM) - VM already running")
                    self.checkpoint_manager.update_checkpoint(
                        step=6, operation_name="Start VM",
                        rollback_data={'vm_name': self.vm_name, 'original_status': 'TERMINATED'}
                    )
                else:
                    self._log_debug(f"Starting instance {self.vm_name}...")
                    result = start_vm.execute(
                        vm_name=self.vm_name,
                        timeout=self.config.vm_start_timeout,
                        tracking_label='restore-vm-start'
                    )
                    self.state_tracker.add_operation("Start VM", result.success, result.message, result.rollback_data, step_number=6)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=6, operation_name="Start VM",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 6 (Start VM) - already completed")

            # Step 7: Delete rescue disk (only if config allows)
            if self.config.delete_rescue_disk and not self._should_skip_step(7):
                # Idempotency check: skip if rescue disk doesn't exist
                if not self._disk_exists(self.rescue_disk_name):
                    self._log_debug("Skipping Step 7 (Delete Rescue Disk) - disk already deleted")
                else:
                    self._log_debug("Deleting rescue disk...")
                    result = delete_rescue.execute(
                        disk_name=self.rescue_disk_name,
                        tracking_label='restore-disk-delete-rescue'
                    )
                    # Note: Don't add to state tracker (can't rollback deletion)
                    if not result.success:
                        self._log_debug("Rescue disk deletion failed (can delete manually)")
                # Update checkpoint (final step)
                self.checkpoint_manager.update_checkpoint(
                    step=7, operation_name="Delete Rescue Disk",
                    rollback_data={}
                )
            elif self._should_skip_step(7):
                self._log_debug("Skipping Step 7 (Delete Rescue Disk) - already completed")

            # Success! Clear checkpoint
            self.checkpoint_manager.clear_checkpoint()
            self._finish_progress(True)
            return True

        except KeyboardInterrupt:
            self._finish_progress(False)
            self._log_error(
                "\nOperation interrupted. Progress has been saved."
            )
            self._log_error(
                "Run the same command again to resume or rollback."
            )
            raise
        except Exception as e:
            self._finish_progress(False)
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
                self._log_debug(f"Safety snapshot verified: {snapshot_name}")
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

        # Clear checkpoint after rollback regardless of success/failure.
        # Success: VM is back to original state, no checkpoint needed.
        # Failure: state is inconsistent, stale checkpoint won't help recovery.
        self.checkpoint_manager.clear_checkpoint()

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
