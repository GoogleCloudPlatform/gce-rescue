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
from ..core.config import RescueConfig, OS_TYPE_WINDOWS, OS_TYPE_LINUX
from ..utils.os_detection import detect_os_type, get_os_display_name
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
    CreateSnapshotOperation
)
from .state import StateTracker
from .rollback import RollbackHandler


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

        # OS detection (will be set during execution)
        self.os_type = None
        self.vm_info = None

        # Windows rescue credentials (generated for RDP access)
        self.windows_rescue_password = None

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
        - Local SSD warning (requires --force)

        Returns:
            True if all validations passed
        """

        self._log_info("Pre-flight Validation:")
        self._log_debug("Creating validation runner")

        runner = ValidationRunner()

        # Add validators with tracking labels
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label='rescue-val-iam'))
        vm_validator = VMStateValidator(self.compute, self.project, self.zone, self.vm_name, tracking_label='rescue-val-vm-state')
        runner.add(vm_validator)

        # Run validations
        results = runner.run_all(self.logger)

        if not results.all_passed():
            self._log_error("")
            self._log_error("Pre-flight validation failed!")
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

        self._log_info("")
        self._log_info("Executing Rescue:")
        self._log_debug(f"Config: {self.config}")

        try:
            # Generate rescue disk name
            rescue_disk_name = f"rescue-disk-{int(time.time())}"
            self._log_debug(f"Rescue disk name: {rescue_disk_name}")

            # Get original disk info
            self._get_original_disk_info()

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
                "Stop VM": stop_vm,                       # Step 1
                "Detach Boot Disk": detach_boot,          # Step 2
                "Create Snapshot": create_snapshot,       # Step 3
                "Create Rescue Disk": create_disk,        # Step 4
                "Attach Rescue Disk": attach_rescue,      # Step 5
                "Set Metadata": set_metadata,             # Step 6
                "Start VM": start_vm,                     # Step 7
                "Attach Original Disk": attach_original   # Step 9
            }

            # Step 1: Stop VM
            self._log_info("  Stopping VM...")
            result = stop_vm.execute(
                vm_name=self.vm_name,
                timeout=self.config.vm_stop_timeout,
                discard_local_ssd=self.config.force,  # Allow stopping VMs with Local SSDs if --force
                tracking_label='rescue-vm-stop'
            )
            self.state_tracker.add_operation("Stop VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 2: Detach boot disk (EARLY - enables snapshot immediately)
            self._log_info("  Detaching boot disk...")
            result = detach_boot.execute(
                vm_name=self.vm_name,
                device_name=self.original_device_name,
                tracking_label='rescue-disk-detach-orig'
            )
            self.state_tracker.add_operation("Detach Boot Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 3: Create safety snapshot (immediately after detach)
            # Track snapshot name for later wait
            pending_snapshot_name = None
            if self.config.create_snapshot:
                if self.config.async_snapshot:
                    self._log_info("  Creating safety snapshot (async - will wait before attaching disk)...")
                else:
                    self._log_info("  Creating safety snapshot...")
                    self._log_info("    (This takes 2-5 minutes but ensures data safety)")

                result = create_snapshot.execute(
                    disk_name=self.original_disk_name,
                    snapshot_name=None,  # Auto-generate
                    description=f"Pre-rescue safety snapshot of {self.vm_name}",
                    timeout=self.config.snapshot_timeout,
                    wait=not self.config.async_snapshot,  # Don't wait if async mode
                    tracking_label='rescue-disk-snapshot'
                )
                self.state_tracker.add_operation("Create Snapshot", result.success, result.message, result.rollback_data)

                if not result.success:
                    self._log_error("  Failed to create safety snapshot")
                    if self.config.require_snapshot:
                        self._log_error("  Snapshot required but failed. Aborting.")
                        self._rollback()
                        return False
                    else:
                        self._log_error("  Continuing without snapshot (risky!)")
                else:
                    pending_snapshot_name = result.rollback_data.get('snapshot_name')
                    self._log_info(f"  [OK] {result.message}")
                    if not self.config.async_snapshot:
                        self._log_info(f"    Snapshot: {pending_snapshot_name}")

            # Step 4: Create rescue disk (runs while snapshot progresses in background)
            # Select rescue image based on OS type
            if self.os_type == OS_TYPE_WINDOWS:
                rescue_image_project = self.config.windows_rescue_image_project
                rescue_image_family = self.config.windows_rescue_image_family
                rescue_disk_size = self.config.windows_rescue_disk_size_gb
                self._log_info(f"  Creating Windows rescue disk ({rescue_disk_size}GB)...")
            else:
                rescue_image_project = self.config.rescue_image_project
                rescue_image_family = self.config.rescue_image_family
                rescue_disk_size = self.config.rescue_disk_size_gb
                self._log_info(f"  Creating Linux rescue disk ({rescue_disk_size}GB)...")

            result = create_disk.execute(
                disk_name=rescue_disk_name,
                size_gb=rescue_disk_size,
                disk_type=self.config.rescue_disk_type,
                source_image=f'projects/{rescue_image_project}/global/images/family/{rescue_image_family}',
                timeout=self.config.disk_create_timeout,
                tracking_label='rescue-disk-create-rescue'
            )
            self.state_tracker.add_operation("Create Rescue Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 5: Attach rescue disk as boot
            self._log_info("  Attaching rescue disk as boot...")
            result = attach_rescue.execute(
                vm_name=self.vm_name,
                disk_name=rescue_disk_name,
                boot=True,
                tracking_label='rescue-disk-attach-rescue'
            )
            self.state_tracker.add_operation("Attach Rescue Disk", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 6: Set rescue metadata
            self._log_info("  Setting rescue metadata...")
            startup_script = self._generate_startup_script()

            # Use appropriate startup script key based on OS type
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
                tracking_label='rescue-meta-set-rescue-keys'
            )
            self.state_tracker.add_operation("Set Metadata", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 7: Start VM in rescue mode
            self._log_info("  Starting VM in rescue mode...")
            result = start_vm.execute(
                vm_name=self.vm_name,
                timeout=self.config.vm_start_timeout,
                tracking_label='rescue-vm-start'
            )
            self.state_tracker.add_operation("Start VM", result.success, result.message, result.rollback_data)
            if not result.success:
                self._rollback()
                return False
            self._log_info(f"  [OK] {result.message}")

            # Step 8: Wait for snapshot completion (if async mode)
            if pending_snapshot_name and self.config.async_snapshot:
                self._log_info("  Waiting for snapshot to complete...")
                snapshot_start_time = time.time()
                snapshot_timeout = self.config.snapshot_timeout

                while True:
                    elapsed = time.time() - snapshot_start_time
                    if elapsed > snapshot_timeout:
                        self._log_error(f"  Snapshot timeout after {snapshot_timeout}s")
                        if self.config.require_snapshot:
                            self._log_error("  Snapshot required but timed out. Aborting.")
                            self._rollback()
                            return False
                        else:
                            self._log_error("  Continuing without verified snapshot (risky!)")
                            break

                    try:
                        snapshot = self.compute.snapshots().get(
                            project=self.project,
                            snapshot=pending_snapshot_name
                        ).execute()

                        status = snapshot.get('status', 'UNKNOWN')

                        if status == 'READY':
                            self._log_info(f"  [OK] Snapshot ready: {pending_snapshot_name}")
                            break
                        elif status == 'FAILED':
                            self._log_error(f"  Snapshot failed: {pending_snapshot_name}")
                            if self.config.require_snapshot:
                                self._rollback()
                                return False
                            break
                        else:
                            # Still creating, show progress
                            self._log_debug(f"    Snapshot status: {status} ({elapsed:.0f}s)")
                    except Exception as e:
                        self._log_debug(f"    Checking snapshot... ({elapsed:.0f}s)")

                    time.sleep(5)

            # Step 9: Re-attach original disk as secondary
            self._log_info("  Attaching affected disk as secondary...")
            time.sleep(5)  # Brief wait for VM stability
            result = attach_original.execute(
                vm_name=self.vm_name,
                disk_name=self.original_disk_name,
                boot=False,
                tracking_label='rescue-disk-attach-orig'
            )
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
        """Get original boot disk information and detect OS type."""
        self.vm_info = self.compute.instances().get(
            project=self.project,
            zone=self.zone,
            instance=self.vm_name
        ).execute()

        # Detect OS type
        self.os_type = detect_os_type(self.vm_info)
        self._log_info(f"  Detected OS: {get_os_display_name(self.os_type)}")

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
