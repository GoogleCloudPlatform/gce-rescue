"""
GCE Rescue - Rescue Orchestrator

Coordinates the rescue workflow:
1. Validates (credentials, permissions, VM state)
2. Detects OS type (Linux or Windows)
3. Executes operations in sequence
4. Tracks state
5. Rolls back on failure
"""

import time
from ..core.config import RescueConfig, OS_TYPE_WINDOWS, OS_TYPE_LINUX, build_user_agent
from ..utils.os_detection import (
    detect_os_type, get_os_display_name, detect_architecture, ARCH_ARM64,
    get_rescue_disk_type, detect_os_flavor
)
from ..validators import (
    ValidationRunner,
    CredentialsValidator,
    IAMPermissionsValidator,
    VMStateValidator
)
from ..operations import (
    StopVMOperation,
    CreateDiskOperation,
    DetachDiskOperation,
    AttachDiskOperation,
    SetMetadataOperation,
    StartVMOperation,
    CreateSnapshotOperation,
    VerifyStartupOperation
)
from .state import StateTracker
from .rollback import RollbackHandler
from .checkpoint import CheckpointManager, CheckpointData


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
                 config: RescueConfig = None, logger=None, log_file: str = None,
                 startup_script_override: str = None, suppress_progress: bool = False,
                 progress_callback=None, session_id: str = None, mode: str = None):
        """
        Initialize rescue orchestrator.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            vm_name: Name of VM to rescue
            config: Optional rescue configuration
            logger: Optional logger
            log_file: Log file path (for checkpoint persistence)
            startup_script_override: Custom startup script (replaces default)
            suppress_progress: Suppress progress spinner (for embedding in repair)
            progress_callback: Optional callback(phase_label) invoked on each step
            session_id: Analytics session UUID (12-char hex)
            mode: Execution mode ('interactive' or 'auto')
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RescueConfig()
        self.logger = logger
        self.log_file = log_file
        self._startup_script_override = startup_script_override
        self._suppress_progress = suppress_progress
        self._progress_callback = progress_callback
        self.session_id = session_id
        self.mode = mode

        # State tracking
        self.state_tracker = StateTracker()
        self.rollback_handler = RollbackHandler(logger)
        self.operations_map = {}

        # Store original disk info
        self.original_disk_name = None
        self.original_device_name = None

        # OS and architecture detection (will be set during execution)
        self.os_type = None
        self.architecture = None
        self.os_flavor = None
        self.vm_info = None

        # Windows rescue credentials (generated for RDP access)
        self.windows_rescue_password = None

        # Verification status (set after startup verification)
        self.verification_succeeded = None

        # Snapshot name (set if snapshot created successfully)
        self.snapshot_name = None

        # Rescue disk name (set during execution)
        self.rescue_disk_name = None

        # Progress tracking for spinner with phases
        import threading
        self._spinner_thread = None
        self._spinner_stop = False
        self._is_debug_mode = False
        self._progress_started = False
        self._progress_phases = []
        self._progress_lock = threading.Lock()
        self._total_steps = 5  # Stopping, Snapshotting, Creating rescue disk, Starting, Attaching

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
            # Step 1: Stopping, Step 3: Snapshotting, Step 4: Creating rescue disk,
            # Step 7: Starting, Step 9: Attaching affected disk
            step_to_phase = {
                1: "Stopping",
                3: "Snapshotting",
                4: "Creating rescue disk",
                7: "Starting",
                9: "Attaching affected disk"
            }
            for step in sorted(step_to_phase.keys()):
                if step <= self._resume_from_step:
                    self._progress_phases.append(step_to_phase[step])

        if not self._is_debug_mode:
            # Print header line
            sys.stdout.write(f"Rescuing instance [{self.vm_name}]:\n")
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
            self.logger.debug(f"[Rescue] {message}", stacklevel=2)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def _ua(self, step: str) -> str:
        """Build User-Agent string for the given step."""
        return build_user_agent(
            session_id=self.session_id, command='rescue',
            os_type=self.os_type, arch=self.architecture,
            flavor=self.os_flavor, mode=self.mode, step=step
        )

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
            self.original_disk_name = self._resumed_context.get('original_disk_name')
            self.original_device_name = self._resumed_context.get('original_device_name')
            self.rescue_disk_name = self._resumed_context.get('rescue_disk_name')
            self.os_type = self._resumed_context.get('os_type')
            self.architecture = self._resumed_context.get('architecture')

        self._log_debug(f"Resume state set: continuing from step {self._resume_from_step + 1}")

    def _should_skip_step(self, step: int) -> bool:
        """Check if a step should be skipped (already completed in previous session)."""
        return step <= self._resume_from_step

    def _is_disk_attached(self, disk_name: str) -> bool:
        """Check if a disk is already attached to the VM."""
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
            'original_disk_name': self.original_disk_name,
            'original_device_name': self.original_device_name,
            'rescue_disk_name': self.rescue_disk_name,
            'os_type': self.os_type,
            'architecture': self.architecture,
            'log_file': self.log_file
        }

    def validate(self) -> bool:
        """
        Run pre-flight validation.

        Checks:
        - Credentials
        - IAM permissions
        - VM state
        - Local SSD warning (requires --force)

        Returns:
            True if all validations passed
        """

        self._log_debug("Creating validation runner")

        runner = ValidationRunner()

        # Add validators with tracking labels
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label=self._ua('val-iam')))
        vm_validator = VMStateValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label=self._ua('val-vm-state'))
        runner.add(vm_validator)

        # Run validations
        results = runner.run_all(self.logger)

        if not results.all_passed():
            results.print_failures()
            return False

        # Check for Local SSDs (after validation passes)
        # Store in instance for CLI to access during confirmation
        vm_result = results.get_result("VM State")
        if vm_result and vm_result.details.get("has_local_ssd"):
            self.has_local_ssd = True
            self.local_ssds = vm_result.details.get("local_ssds", [])
        else:
            self.has_local_ssd = False
            self.local_ssds = []

        self._log_debug("All validations passed")
        return True

    def execute(self) -> bool:
        """
        Execute the rescue workflow.

        Returns:
            True if rescue succeeded, False if failed
        """

        self._log_debug(f"Rescuing instance '{self.vm_name}'...")
        self._log_debug(f"Config: {self.config}")

        # Initialize progress display
        self._init_progress()

        # Total internal steps for checkpoint tracking
        total_checkpoint_steps = 9  # Stop, Detach, Snapshot, CreateDisk, AttachRescue, SetMeta, Start, AttachOrig, Verify

        try:
            # Get original disk info if not resuming (or if not already set)
            if not self._resume_from_step or not self.original_disk_name:
                self._get_original_disk_info()

            # Generate rescue disk name if not resuming
            if not self._resume_from_step or not self.rescue_disk_name:
                rescue_disk_name = f"rescue-disk-{int(time.time())}"
                self.rescue_disk_name = rescue_disk_name
            else:
                rescue_disk_name = self.rescue_disk_name
            self._log_debug(f"Rescue disk name: {rescue_disk_name}")

            # Create initial checkpoint if not resuming
            if not self._resume_from_step:
                self.checkpoint_manager.create_checkpoint(
                    operation_type='rescue',
                    total_steps=total_checkpoint_steps,
                    context=self._get_checkpoint_context()
                )

            # Create operations (in execution order)
            stop_vm = StopVMOperation(self.compute, self.project, self.zone, self.logger)
            detach_boot = DetachDiskOperation(self.compute, self.project, self.zone, self.logger)
            create_snapshot = CreateSnapshotOperation(self.compute, self.project, self.zone, self.logger)
            create_disk = CreateDiskOperation(self.compute, self.project, self.zone, self.logger)
            attach_rescue = AttachDiskOperation(self.compute, self.project, self.zone, self.logger)
            set_metadata = SetMetadataOperation(self.compute, self.project, self.zone, self.logger)
            start_vm = StartVMOperation(self.compute, self.project, self.zone, self.logger)
            attach_original = AttachDiskOperation(self.compute, self.project, self.zone, self.logger)

            # Build operations map for rollback (in execution order)
            self.operations_map = {
                "Stop VM": stop_vm,
                "Detach Boot Disk": detach_boot,
                "Create Snapshot": create_snapshot,
                "Create Rescue Disk": create_disk,
                "Attach Rescue Disk": attach_rescue,
                "Set Metadata": set_metadata,
                "Start VM": start_vm,
                "Attach Original Disk": attach_original
            }

            # Step 1: Stop VM
            if not self._should_skip_step(1):
                self._update_progress("Stopping")
                self._log_debug(f"Stopping instance {self.vm_name}...")
                result = stop_vm.execute(
                    vm_name=self.vm_name,
                    timeout=self.config.vm_stop_timeout,
                    discard_local_ssd=self.config.force,
                    tracking_label=self._ua('vm-stop')
                )
                self.state_tracker.add_operation("Stop VM", result.success, result.message, result.rollback_data, step_number=1)
                if not result.success:
                    self._finish_progress(False)
                    self._rollback()
                    return False
                # Update checkpoint after successful step
                self.checkpoint_manager.update_checkpoint(
                    step=1, operation_name="Stop VM",
                    rollback_data=result.rollback_data,
                    context_updates=self._get_checkpoint_context()
                )
            else:
                self._log_debug("Skipping Step 1 (Stop VM) - already completed")

            # Step 2: Detach boot disk
            if not self._should_skip_step(2):
                self._log_debug("Detaching boot disk...")
                result = detach_boot.execute(
                    vm_name=self.vm_name,
                    device_name=self.original_device_name,
                    tracking_label=self._ua('disk-detach-orig')
                )
                self.state_tracker.add_operation("Detach Boot Disk", result.success, result.message, result.rollback_data, step_number=2)
                if not result.success:
                    self._finish_progress(False)
                    self._rollback()
                    return False
                # Update checkpoint
                self.checkpoint_manager.update_checkpoint(
                    step=2, operation_name="Detach Boot Disk",
                    rollback_data=result.rollback_data
                )
            else:
                self._log_debug("Skipping Step 2 (Detach Boot Disk) - already completed")

            # Step 3: Create safety snapshot
            pending_snapshot_name = None
            if self.config.create_snapshot and not self._should_skip_step(3):
                self._update_progress("Snapshotting")
                self._log_debug("Creating snapshot...")
                result = create_snapshot.execute(
                    disk_name=self.original_disk_name,
                    snapshot_name=None,
                    description=f"Pre-rescue safety snapshot of {self.vm_name}",
                    timeout=self.config.snapshot_timeout,
                    wait=not self.config.async_snapshot,
                    tracking_label=self._ua('disk-snapshot')
                )
                self.state_tracker.add_operation("Create Snapshot", result.success, result.message, result.rollback_data, step_number=3)

                if not result.success:
                    if self.config.require_snapshot:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                else:
                    pending_snapshot_name = result.rollback_data.get('snapshot_name')
                    self.snapshot_name = pending_snapshot_name  # Store for output
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=3, operation_name="Create Snapshot",
                        rollback_data=result.rollback_data,
                        context_updates={'snapshot_name': pending_snapshot_name}
                    )
            elif self._should_skip_step(3):
                self._log_debug("Skipping Step 3 (Create Snapshot) - already completed")
                # Restore snapshot name from context if resuming
                if self._resumed_context:
                    pending_snapshot_name = self._resumed_context.get('snapshot_name')
                    self.snapshot_name = pending_snapshot_name

            # Step 4: Create rescue disk
            if not self._should_skip_step(4):
                self._update_progress("Creating rescue disk")
                # Idempotency check: skip if rescue disk already exists
                if self._disk_exists(rescue_disk_name):
                    self._log_debug("Skipping Step 4 (Create Rescue Disk) - disk already exists")
                    # Update checkpoint to record this step as done
                    self.checkpoint_manager.update_checkpoint(
                        step=4, operation_name="Create Rescue Disk",
                        rollback_data={'disk_name': rescue_disk_name},
                        context_updates={'rescue_disk_name': rescue_disk_name}
                    )
                else:
                    if self.config.custom_rescue_image:
                        # User-supplied image URL (overrides OS/arch auto-selection)
                        source_image = self.config.custom_rescue_image
                        rescue_disk_size = self.config.rescue_disk_size_gb
                        self._log_debug(f"Creating rescue disk ({rescue_disk_size}GB, custom image: {source_image})...")
                    elif self.os_type == OS_TYPE_WINDOWS:
                        rescue_image_project = self.config.windows_rescue_image_project
                        rescue_image_family = self.config.windows_rescue_image_family
                        rescue_disk_size = self.config.windows_rescue_disk_size_gb
                        source_image = f'projects/{rescue_image_project}/global/images/family/{rescue_image_family}'
                        self._log_debug(f"Creating rescue disk ({rescue_disk_size}GB, {rescue_image_family})...")
                    elif self.architecture == ARCH_ARM64:
                        # ARM64 Linux (T2A instances)
                        rescue_image_project = self.config.arm64_rescue_image_project
                        rescue_image_family = self.config.arm64_rescue_image_family
                        rescue_disk_size = self.config.rescue_disk_size_gb
                        source_image = f'projects/{rescue_image_project}/global/images/family/{rescue_image_family}'
                        self._log_debug(f"Creating rescue disk ({rescue_disk_size}GB, {rescue_image_family})...")
                    else:
                        # x86_64 Linux (default)
                        rescue_image_project = self.config.rescue_image_project
                        rescue_image_family = self.config.rescue_image_family
                        rescue_disk_size = self.config.rescue_disk_size_gb
                        source_image = f'projects/{rescue_image_project}/global/images/family/{rescue_image_family}'
                        self._log_debug(f"Creating rescue disk ({rescue_disk_size}GB, {rescue_image_family})...")

                    result = create_disk.execute(
                        disk_name=rescue_disk_name,
                        size_gb=rescue_disk_size,
                        disk_type=self.config.rescue_disk_type,
                        source_image=source_image,
                        timeout=self.config.disk_create_timeout,
                        tracking_label=self._ua('disk-create-rescue')
                    )
                    self.state_tracker.add_operation("Create Rescue Disk", result.success, result.message, result.rollback_data, step_number=4)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=4, operation_name="Create Rescue Disk",
                        rollback_data=result.rollback_data,
                        context_updates={'rescue_disk_name': rescue_disk_name}
                    )
            else:
                self._log_debug("Skipping Step 4 (Create Rescue Disk) - already completed")

            # Step 5: Attach rescue disk as boot
            if not self._should_skip_step(5):
                # Idempotency check: skip if rescue disk already attached
                if self._is_disk_attached(rescue_disk_name):
                    self._log_debug("Skipping Step 5 (Attach Rescue Disk) - disk already attached")
                    # Update checkpoint to record this step as done
                    self.checkpoint_manager.update_checkpoint(
                        step=5, operation_name="Attach Rescue Disk",
                        rollback_data={'vm_name': self.vm_name, 'disk_name': rescue_disk_name}
                    )
                else:
                    self._log_debug("Attaching rescue disk as boot...")
                    result = attach_rescue.execute(
                        vm_name=self.vm_name,
                        disk_name=rescue_disk_name,
                        boot=True,
                        tracking_label=self._ua('disk-attach-rescue')
                    )
                    self.state_tracker.add_operation("Attach Rescue Disk", result.success, result.message, result.rollback_data, step_number=5)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=5, operation_name="Attach Rescue Disk",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 5 (Attach Rescue Disk) - already completed")

            # Step 6: Set rescue metadata
            if not self._should_skip_step(6):
                self._log_debug("Setting rescue metadata...")
                startup_script = self._generate_startup_script()

                if self.os_type == OS_TYPE_WINDOWS:
                    script_key = 'windows-startup-script-ps1'
                else:
                    script_key = 'startup-script'

                metadata_items = [
                    {'key': script_key, 'value': startup_script},
                    {'key': 'rescue-mode', 'value': str(int(time.time()))},
                    {'key': 'rescue-original-disk', 'value': self.original_disk_name},
                    {'key': 'rescue-os-type', 'value': self.os_type}
                ]
                result = set_metadata.execute(
                    vm_name=self.vm_name,
                    metadata_items=metadata_items,
                    tracking_label=self._ua('meta-set-rescue-keys')
                )
                self.state_tracker.add_operation("Set Metadata", result.success, result.message, result.rollback_data, step_number=6)
                if not result.success:
                    self._finish_progress(False)
                    self._rollback()
                    return False
                # Update checkpoint - include Windows password if set
                context_updates = {}
                if self.os_type == OS_TYPE_WINDOWS and self.windows_rescue_password:
                    context_updates['windows_rescue_password'] = self.windows_rescue_password
                self.checkpoint_manager.update_checkpoint(
                    step=6, operation_name="Set Metadata",
                    rollback_data=result.rollback_data,
                    context_updates=context_updates if context_updates else None
                )
            else:
                self._log_debug("Skipping Step 6 (Set Metadata) - already completed")
                # Restore Windows password from checkpoint context if resuming
                if self._resumed_context and self.os_type == OS_TYPE_WINDOWS:
                    self.windows_rescue_password = self._resumed_context.get('windows_rescue_password')

            # Step 7: Start VM in rescue mode
            if not self._should_skip_step(7):
                self._update_progress("Starting")
                # Idempotency check: skip if VM is already running
                vm_status = self._get_vm_status()
                if vm_status == 'RUNNING':
                    self._log_debug("Skipping Step 7 (Start VM) - VM already running")
                    # Update checkpoint to record this step as done
                    self.checkpoint_manager.update_checkpoint(
                        step=7, operation_name="Start VM",
                        rollback_data={'vm_name': self.vm_name, 'original_status': 'TERMINATED'}
                    )
                else:
                    self._log_debug(f"Starting instance {self.vm_name}...")
                    result = start_vm.execute(
                        vm_name=self.vm_name,
                        timeout=self.config.vm_start_timeout,
                        tracking_label=self._ua('vm-start')
                    )
                    self.state_tracker.add_operation("Start VM", result.success, result.message, result.rollback_data, step_number=7)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=7, operation_name="Start VM",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 7 (Start VM) - already completed")

            # Wait for snapshot completion (if async mode) - not a numbered step
            if pending_snapshot_name and self.config.async_snapshot:
                self._log_debug("Waiting for async snapshot to complete...")
                snapshot_start_time = time.time()
                snapshot_timeout = self.config.snapshot_timeout

                while True:
                    elapsed = time.time() - snapshot_start_time
                    if elapsed > snapshot_timeout:
                        self._log_debug(f"Snapshot timeout after {snapshot_timeout}s")
                        if self.config.require_snapshot:
                            self._log_error("Snapshot required but timed out. Aborting.")
                            self._rollback()
                            return False
                        else:
                            self._log_debug("Continuing without verified snapshot.")
                            break

                    try:
                        snapshot = self.compute.snapshots().get(
                            project=self.project,
                            snapshot=pending_snapshot_name
                        ).execute()

                        status = snapshot.get('status', 'UNKNOWN')

                        if status == 'READY':
                            self._log_debug(f"Snapshot ready: {pending_snapshot_name}")
                            break
                        elif status == 'FAILED':
                            self._log_debug(f"Snapshot failed: {pending_snapshot_name}")
                            if self.config.require_snapshot:
                                self._rollback()
                                return False
                            break
                        else:
                            # Still creating, show progress
                            self._log_debug(f"Snapshot status: {status} ({elapsed:.0f}s)")
                    except Exception as e:
                        self._log_debug(f"Checking snapshot... ({elapsed:.0f}s)")

                    time.sleep(5)

            # Step 8: Re-attach original disk as secondary
            if not self._should_skip_step(8):
                # Idempotency check: skip if original disk already attached
                if self._is_disk_attached(self.original_disk_name):
                    self._log_debug("Skipping Step 8 (Attach Original Disk) - disk already attached")
                    # Update checkpoint to record this step as done
                    self.checkpoint_manager.update_checkpoint(
                        step=8, operation_name="Attach Original Disk",
                        rollback_data={'vm_name': self.vm_name, 'disk_name': self.original_disk_name}
                    )
                else:
                    self._log_debug("Attaching original boot disk as secondary...")
                    time.sleep(5)  # Brief wait for VM stability
                    result = attach_original.execute(
                        vm_name=self.vm_name,
                        disk_name=self.original_disk_name,
                        boot=False,
                        tracking_label=self._ua('disk-attach-orig')
                    )
                    self.state_tracker.add_operation("Attach Original Disk", result.success, result.message, result.rollback_data, step_number=8)
                    if not result.success:
                        self._finish_progress(False)
                        self._rollback()
                        return False
                    # Update checkpoint
                    self.checkpoint_manager.update_checkpoint(
                        step=8, operation_name="Attach Original Disk",
                        rollback_data=result.rollback_data
                    )
            else:
                self._log_debug("Skipping Step 8 (Attach Original Disk) - already completed")

            # Step 9: Verify startup script completion (disk mounting)
            if not self._should_skip_step(9):
                self._update_progress("Attaching affected disk")
                self._log_debug("Verifying startup script...")
                verify_startup = VerifyStartupOperation(self.compute, self.project, self.zone, self.logger)
                result = verify_startup.execute(
                    vm_name=self.vm_name,
                    timeout=self.config.startup_verification_timeout,
                    tracking_label=self._ua('vm-verify-startup')
                )
                self.verification_succeeded = result.success
                # Don't fail on verification timeout - continue
                # Update checkpoint (final step)
                self.checkpoint_manager.update_checkpoint(
                    step=9, operation_name="Verify Startup",
                    rollback_data={}
                )
            else:
                self._log_debug("Skipping Step 9 (Verify Startup) - already completed")
                self.verification_succeeded = True  # Assume success if resuming past this step

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
            self._log_error(f"Unexpected error during rescue: {str(e)}")
            self._rollback()
            return False

    def _get_original_disk_info(self):
        """Get original boot disk information and detect OS type."""
        self.vm_info = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        # Detect OS type and architecture
        self.os_type = detect_os_type(self.vm_info)
        self.architecture = detect_architecture(self.vm_info)
        self.os_flavor = detect_os_flavor(self.vm_info)
        self._log_debug(f"Detected OS: {get_os_display_name(self.os_type)}, Architecture: {self.architecture}")

        # Auto-detect rescue disk type based on machine family
        detected_disk_type = get_rescue_disk_type(self.vm_info, self.config.rescue_disk_type)
        if detected_disk_type != self.config.rescue_disk_type:
            self._log_debug(f"Machine type requires {detected_disk_type} (overriding {self.config.rescue_disk_type})")
            self.config.rescue_disk_type = detected_disk_type

        for disk in self.vm_info.get('disks', []):
            if disk.get('boot'):
                self.original_disk_name = disk['source'].split('/')[-1]
                self.original_device_name = disk['deviceName']
                self._log_debug(f"Original disk: {self.original_disk_name}")
                break

    def _generate_startup_script(self) -> str:
        """Generate startup script for rescue mode based on OS type."""
        import re
        import secrets
        import string
        from pathlib import Path

        # Use override if provided (e.g., repair command injects fix scripts)
        if self._startup_script_override:
            return self._startup_script_override

        # Validate disk name to prevent injection (defense in depth)
        # GCP disk names must match: [a-z]([-a-z0-9]*[a-z0-9])?
        if not re.match(r'^[a-z]([-a-z0-9]*[a-z0-9])?$', self.original_disk_name):
            self._log_error(f"Invalid disk name format: {self.original_disk_name}")
            raise ValueError(f"Disk name contains invalid characters: {self.original_disk_name}")

        # Select script based on OS type
        if self.os_type == OS_TYPE_WINDOWS:
            script_file = Path(__file__).parent.parent / 'startup_scripts' / 'rescue_mount_windows.ps1'
            fallback_script = self._get_windows_fallback_script()

            # Generate secure password for Windows rescue admin account
            # 16 chars: uppercase, lowercase, digits, special chars (Windows complexity)
            alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
            self.windows_rescue_password = ''.join(secrets.choice(alphabet) for _ in range(16))
        else:
            script_file = Path(__file__).parent.parent / 'startup_scripts' / 'rescue_mount.sh'
            fallback_script = self._get_linux_fallback_script()

        if script_file.exists():
            with open(script_file, 'r') as f:
                script = f.read()
                # Replace disk name placeholder
                script = script.replace('DISK_NAME_PLACEHOLDER', self.original_disk_name)
                # Replace password placeholder for Windows
                if self.os_type == OS_TYPE_WINDOWS and self.windows_rescue_password:
                    script = script.replace('PASSWORD_PLACEHOLDER', self.windows_rescue_password)
                return script

        # Fallback: inline script if template not found
        self._log_error("Warning: Startup script template not found, using fallback")
        return fallback_script

    def _get_windows_fallback_script(self) -> str:
        """Generate fallback PowerShell script for Windows rescue mode."""
        return f'''# GCE Rescue Mode - Windows Fallback Script
$ErrorActionPreference = "Continue"
$diskName = "{self.original_disk_name}"
$logFile = "C:\\gce-rescue.log"

function Write-Log {{
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $logMessage = "[$timestamp] $Message"
    Write-Host $logMessage
    Add-Content -Path $logFile -Value $logMessage
}}

Write-Log "=== GCE Rescue Mode - Windows ==="
Write-Log "Affected disk: $diskName"

# Wait for disk
$maxAttempts = 60
$attempt = 0
$diskFound = $false

while ($attempt -lt $maxAttempts) {{
    $attempt++
    $disks = Get-Disk | Where-Object {{ $_.Number -ne 0 }}
    if ($disks) {{
        Write-Log "Found $($disks.Count) additional disk(s)"
        $diskFound = $true
        break
    }}
    Write-Log "Waiting for disk... attempt $attempt/$maxAttempts"
    Start-Sleep -Seconds 5
}}

if (-not $diskFound) {{
    Write-Log "ERROR: Affected disk not found"
    exit 1
}}

# Process disks
foreach ($disk in $disks) {{
    Write-Log "Processing Disk $($disk.Number)"
    try {{
        if ($disk.OperationalStatus -eq 'Offline') {{
            Set-Disk -Number $disk.Number -IsOffline $false
        }}
        if ($disk.IsReadOnly) {{
            Set-Disk -Number $disk.Number -IsReadOnly $false
        }}
        $partitions = Get-Partition -DiskNumber $disk.Number | Where-Object {{ $_.Type -ne 'Reserved' -and $_.Size -gt 1GB }}
        foreach ($partition in $partitions) {{
            if (-not $partition.DriveLetter) {{
                $usedLetters = (Get-Partition | Where-Object {{ $_.DriveLetter }}).DriveLetter
                foreach ($letter in 'D', 'E', 'F', 'G', 'H') {{
                    if ($letter -notin $usedLetters) {{
                        Set-Partition -DiskNumber $disk.Number -PartitionNumber $partition.PartitionNumber -NewDriveLetter $letter
                        Write-Log "Mounted partition at $letter`:"
                        break
                    }}
                }}
            }}
        }}
    }} catch {{
        Write-Log "ERROR: $_"
    }}
}}

Write-Log "=== GCE Rescue Ready ==="
'''

    def _get_linux_fallback_script(self) -> str:
        """Generate fallback Bash script for Linux rescue mode."""
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
            self._log_error(f"  3. Rescue disk: gcloud compute disks list --filter='name~rescue-disk'")
            self._log_error("")
            self._log_error("You may need to manually:")
            self._log_error("  - Detach/attach disks")
            self._log_error("  - Delete rescue disk")
            self._log_error("  - Start/stop VM")
            self._log_error("=" * 60)

    def rollback(self) -> bool:
        """Public rollback method."""
        return self.rollback_handler.rollback(self.state_tracker, self.operations_map)
