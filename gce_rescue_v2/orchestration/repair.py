"""
GCE Rescue - Repair Orchestrator

Automates the full repair flow: diagnose -> rescue (with embedded fix) -> restore.
Fix scripts are embedded directly in the startup script - no SSH, no external tools.

Supported fix categories:
    - fstab: Comments out invalid UUID/device/label entries

Usage:
    orchestrator = RepairOrchestrator(compute, project, zone, vm_name, config, logger)
    orchestrator.validate()
    diagnosis = orchestrator.diagnose()
    orchestrator.execute(diagnosis)
"""

import sys
import threading
import time
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional, List, Set

from googleapiclient import discovery
import googleapiclient.http
import google_auth_httplib2
import httplib2

from ..core.config import RescueConfig, RestoreConfig, VERSION
from ..operations import DiagnoseOperation
from .rescue import RescueOrchestrator
from .restore import RestoreOrchestrator

# Categories that have automated fix scripts
SUPPORTED_FIX_CATEGORIES: Set[str] = {'fstab'}

# Marker prefixes emitted by fix scripts to serial console
REPAIR_LINE_MARKER = 'GCE-REPAIR-LINE:'
REPAIR_RESULT_MARKER = 'GCE-REPAIR-RESULT:'

# Completion marker used by rescue startup script
RESCUE_COMPLETE_MARKER = 'GCE-RESCUE-COMPLETE'

# Maps raw rescue/restore step labels to user-friendly display names
RESCUE_SUBSTEP_LABELS = {
    'Stopping': 'Stopping VM',
    'Snapshotting': 'Creating snapshot',
    'Creating rescue disk': 'Creating rescue disk',
    'Starting': 'Starting rescue VM',
    'Attaching affected disk': 'Mounting disk',
}
RESTORE_SUBSTEP_LABELS = {
    'Stopping': 'Stopping VM',
    'Restoring affected disk': 'Restoring boot disk',
    'Starting': 'Starting VM',
}


class RepairOrchestrator:
    """Orchestrates diagnose -> rescue (with fix) -> restore."""

    def __init__(self, compute, project: str, zone: str, vm_name: str,
                 config: RescueConfig = None, logger=None, log_file: str = None):
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RescueConfig()
        self.logger = logger
        self.log_file = log_file

        # Progress tracking
        self._spinner_thread = None
        self._spinner_stop = False
        self._is_debug_mode = False
        self._progress_started = False
        self._progress_phases = []
        self._progress_lock = threading.Lock()
        self._total_steps = 3  # Rescue, Repair, Restore

        # Substep tracking (updated by progress callbacks from sub-orchestrators)
        self._current_phase = ''
        self._current_substep = ''
        self._current_line_substeps: List[str] = []

    def _log_info(self, message: str):
        if self.logger:
            self.logger.info(message)

    def _log_debug(self, message: str):
        if self.logger:
            self.logger.debug(f"[Repair] {message}", stacklevel=2)

    def _log_error(self, message: str):
        if self.logger:
            self.logger.error(message)

    def _create_tracked_client(self, tracking_label: str):
        """Create a compute client with unique User-Agent for usage tracking."""
        credentials = self.compute._http.credentials
        user_agent = f'gce-rescue-{VERSION}-{tracking_label}'

        def _request_builder(http, *args, **kwargs):
            headers = kwargs.setdefault('headers', {})
            headers['user-agent'] = user_agent
            auth_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http()
            )
            return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

        return discovery.build(
            'compute', 'v1', credentials=credentials,
            cache_discovery=False, requestBuilder=_request_builder
        )

    def validate(self) -> bool:
        """Run pre-flight validation (credentials, IAM, VM state) + Linux-only check."""
        from ..validators import (
            ValidationRunner, CredentialsValidator,
            IAMPermissionsValidator, VMStateValidator,
        )

        runner = ValidationRunner()
        runner.add(CredentialsValidator(self.compute, self.project, self.zone))
        runner.add(IAMPermissionsValidator(
            self.compute, self.project, self.zone, self.vm_name,
            tracking_label='repair-val-iam'
        ))
        runner.add(VMStateValidator(
            self.compute, self.project, self.zone, self.vm_name,
            tracking_label='repair-val-vm-state'
        ))

        results = runner.run_all(self.logger)
        if not results.all_passed():
            results.print_failures()
            return False

        # Check Linux-only
        from ..utils.os_detection import detect_os_type
        compute = self._create_tracked_client('repair-val-os')
        vm_info = compute.instances().get(
            project=self.project, zone=self.zone, instance=self.vm_name
        ).execute()
        os_type = detect_os_type(vm_info)
        if os_type == 'windows':
            self._log_error("Repair is only supported for Linux VMs.")
            print("", file=sys.stderr)
            print("For Windows VMs, use rescue mode for manual repair:", file=sys.stderr)
            print(
                f"  $ gce-rescue-v2 rescue {self.vm_name} "
                f"--zone={self.zone} --project={self.project}",
                file=sys.stderr
            )
            return False

        self._log_debug("Validation passed")
        return True

    def diagnose(self) -> Optional[Dict[str, Any]]:
        """Run diagnosis and return the diagnosis dict (or None on failure)."""
        diagnose_op = DiagnoseOperation(
            self.compute, self.project, self.zone, self.logger
        )
        result = diagnose_op.execute(self.vm_name, tracking_label='repair-diagnose')

        if not result.success:
            self._log_error(f"Diagnosis failed: {result.message}")
            return None

        return result.rollback_data

    def get_fixable_categories(self, diagnosis: Dict[str, Any]) -> List[str]:
        """Return list of categories from diagnosis that have fix scripts."""
        categories = []
        seen = set()
        for err in diagnosis.get('boot_errors', []):
            cat = err.get('category', '')
            if cat in SUPPORTED_FIX_CATEGORIES and cat not in seen:
                seen.add(cat)
                categories.append(cat)
        return categories

    def get_unfixable_categories(self, diagnosis: Dict[str, Any]) -> List[str]:
        """Return list of categories from diagnosis that lack fix scripts."""
        categories = []
        seen = set()
        for err in diagnosis.get('boot_errors', []):
            cat = err.get('category', '')
            if cat not in SUPPORTED_FIX_CATEGORIES and cat not in seen:
                seen.add(cat)
                categories.append(cat)
        return categories

    def execute(self, diagnosis: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the full repair: rescue (with fix script) -> parse results -> restore.

        Args:
            diagnosis: Diagnosis dict from diagnose()

        Returns:
            Dict with keys: status, fixed_count, fix_lines, error,
            snapshot_name, duration_seconds
        """
        fixable = self.get_fixable_categories(diagnosis)
        if not fixable:
            return {'status': 'no_fix', 'fixed_count': 0, 'fix_lines': [],
                    'error': None, 'snapshot_name': None, 'duration_seconds': 0}

        # Generate repair startup script
        repair_script = self._generate_repair_script(diagnosis)
        self._log_debug(f"Generated repair script ({len(repair_script)} bytes)")

        # Initialize progress display
        self._init_progress()
        start_time = time.time()

        try:
            # Phase 1: Rescue with embedded fix script
            self._update_progress("Rescue")
            rescue_config = RescueConfig()
            rescue_config.create_snapshot = self.config.create_snapshot
            rescue_config.force = self.config.force

            rescue = RescueOrchestrator(
                compute=self.compute, project=self.project, zone=self.zone,
                vm_name=self.vm_name, config=rescue_config, logger=self.logger,
                log_file=self.log_file, startup_script_override=repair_script,
                suppress_progress=True,
                progress_callback=self._make_progress_callback(
                    "Rescue", RESCUE_SUBSTEP_LABELS
                )
            )
            # Skip validation (already done)
            if not rescue.execute():
                self._finish_progress(False)
                self._log_error("Rescue phase failed. Check logs for details.")
                return {
                    'status': 'rescue_failed', 'fixed_count': 0,
                    'fix_lines': [], 'error': 'Rescue phase failed',
                    'snapshot_name': rescue.snapshot_name,
                    'duration_seconds': time.time() - start_time
                }

            snapshot_name = rescue.snapshot_name

            # Phase 2: Parse repair results from serial console
            self._update_progress("Repair")
            with self._progress_lock:
                self._current_substep = 'Applying fix'
            repair_results = self._parse_repair_results()
            with self._progress_lock:
                if (self._current_line_substeps
                        and self._current_line_substeps[-1]
                        != 'Applying fix'):
                    self._current_line_substeps.append('Applying fix')
                elif not self._current_line_substeps:
                    self._current_line_substeps.append('Applying fix')
                self._current_substep = 'Verifying fix'
            self._log_debug(f"Repair results: {repair_results}")

            # If mount failed (no completion marker found by verify), don't restore
            if not rescue.verification_succeeded:
                self._finish_progress(False)
                self._log_error(
                    "Startup script did not complete. The disk may not have mounted."
                )
                self._log_error(
                    "VM is in rescue mode for manual investigation."
                )
                self._log_error("")
                self._log_error("Connect to investigate:")
                self._log_error(
                    f"  $ gcloud compute ssh {self.vm_name} "
                    f"--zone={self.zone} --project={self.project}"
                )
                self._log_error("")
                self._log_error("When done, restore with:")
                self._log_error(
                    f"  $ gce-rescue-v2 restore {self.vm_name} "
                    f"--zone={self.zone} --project={self.project}"
                )
                return {
                    'status': 'mount_failed', 'fixed_count': 0,
                    'fix_lines': [], 'error': 'Disk mount did not complete',
                    'snapshot_name': snapshot_name,
                    'duration_seconds': time.time() - start_time
                }

            # Phase 3: Restore
            self._update_progress("Restore")
            restore_config = RestoreConfig()
            restore_config.delete_rescue_disk = True

            restore = RestoreOrchestrator(
                compute=self.compute, project=self.project, zone=self.zone,
                vm_name=self.vm_name, config=restore_config, logger=self.logger,
                log_file=self.log_file, suppress_progress=True,
                progress_callback=self._make_progress_callback(
                    "Restore", RESTORE_SUBSTEP_LABELS
                )
            )
            if not restore.execute():
                self._finish_progress(False)
                self._log_error("Restore phase failed.")
                self._log_error(
                    "VM may be in rescue mode. Try restoring manually:"
                )
                self._log_error(
                    f"  $ gce-rescue-v2 restore {self.vm_name} "
                    f"--zone={self.zone} --project={self.project}"
                )
                return {
                    'status': 'restore_failed',
                    'fixed_count': repair_results.get('fixed_count', 0),
                    'fix_lines': repair_results.get('fix_lines', []),
                    'error': 'Restore phase failed',
                    'snapshot_name': snapshot_name,
                    'duration_seconds': time.time() - start_time
                }

            self._finish_progress(True)

            # Post-restore boot verification
            boot_check = self._verify_boot_after_repair()
            repair_results['snapshot_name'] = snapshot_name
            repair_results['duration_seconds'] = time.time() - start_time
            repair_results['boot_verified'] = boot_check.get('verified')
            repair_results['boot_errors_after'] = boot_check.get('errors', [])
            return repair_results

        except Exception as e:
            self._finish_progress(False)
            self._log_error(f"Unexpected error during repair: {e}")
            return {
                'status': 'error', 'fixed_count': 0,
                'fix_lines': [], 'error': str(e),
                'snapshot_name': None,
                'duration_seconds': time.time() - start_time
            }

    def _find_rescue_snapshot(self) -> Optional[str]:
        """Find the pre-rescue snapshot name from VM metadata.

        During rescue, the original disk name is stored in metadata as
        'rescue-original-disk'. Snapshots follow the naming pattern
        'pre-rescue-{disk_name}-{timestamp}'.

        Returns:
            Snapshot name if found, None otherwise.
        """
        try:
            compute = self._create_tracked_client('repair-snapshot-lookup')
            vm_info = compute.instances().get(
                project=self.project, zone=self.zone, instance=self.vm_name
            ).execute()

            # Get original disk name from rescue metadata
            original_disk = None
            for item in vm_info.get('metadata', {}).get('items', []):
                if item.get('key') == 'rescue-original-disk':
                    original_disk = item.get('value')
                    break

            if not original_disk:
                self._log_debug("No rescue-original-disk in metadata")
                return None

            # Find matching snapshot (most recent first)
            snapshots = compute.snapshots().list(
                project=self.project,
                filter=f'name:pre-rescue-{original_disk}-*'
            ).execute()

            items = snapshots.get('items', [])
            if not items:
                self._log_debug(f"No snapshots found matching pre-rescue-{original_disk}-*")
                return None

            # Return the most recent one (highest timestamp suffix)
            items.sort(key=lambda s: s.get('creationTimestamp', ''), reverse=True)
            name = items[0].get('name')
            self._log_debug(f"Found rescue snapshot: {name}")
            return name

        except Exception as e:
            self._log_debug(f"Could not find rescue snapshot: {e}")
            return None

    def resume(self) -> Dict[str, Any]:
        """Resume an interrupted repair: parse results from serial console + restore.

        Called when the VM is already in rescue mode from a previous repair attempt.
        Skips the rescue phase entirely.

        Returns:
            Dict with keys: status, fixed_count, fix_lines, error,
            snapshot_name, duration_seconds
        """
        self._total_steps = 2  # Repair, Restore (rescue already done)

        # Look up snapshot from the original rescue phase
        snapshot_name = self._find_rescue_snapshot()

        self._init_progress()
        start_time = time.time()

        try:
            # Phase 1: Parse repair results from serial console
            self._update_progress("Repair")
            with self._progress_lock:
                self._current_substep = 'Verifying fix'
            repair_results = self._parse_repair_results()
            self._log_debug(f"Repair results: {repair_results}")

            # Phase 2: Restore
            self._update_progress("Restore")
            restore_config = RestoreConfig()
            restore_config.delete_rescue_disk = True

            restore = RestoreOrchestrator(
                compute=self.compute, project=self.project, zone=self.zone,
                vm_name=self.vm_name, config=restore_config, logger=self.logger,
                log_file=self.log_file, suppress_progress=True,
                progress_callback=self._make_progress_callback(
                    "Restore", RESTORE_SUBSTEP_LABELS
                )
            )
            if not restore.execute():
                self._finish_progress(False)
                self._log_error("Restore phase failed.")
                self._log_error(
                    "VM may be in rescue mode. Try restoring manually:"
                )
                self._log_error(
                    f"  $ gce-rescue-v2 restore {self.vm_name} "
                    f"--zone={self.zone} --project={self.project}"
                )
                return {
                    'status': 'restore_failed',
                    'fixed_count': repair_results.get('fixed_count', 0),
                    'fix_lines': repair_results.get('fix_lines', []),
                    'error': 'Restore phase failed',
                    'snapshot_name': snapshot_name,
                    'duration_seconds': time.time() - start_time
                }

            self._finish_progress(True)

            # Post-restore boot verification
            boot_check = self._verify_boot_after_repair()
            repair_results['snapshot_name'] = snapshot_name
            repair_results['duration_seconds'] = time.time() - start_time
            repair_results['boot_verified'] = boot_check.get('verified')
            repair_results['boot_errors_after'] = boot_check.get('errors', [])
            return repair_results

        except Exception as e:
            self._finish_progress(False)
            self._log_error(f"Unexpected error during resume: {e}")
            return {
                'status': 'error', 'fixed_count': 0,
                'fix_lines': [], 'error': str(e),
                'snapshot_name': snapshot_name,
                'duration_seconds': time.time() - start_time
            }

    def _generate_repair_script(self, diagnosis: Dict[str, Any]) -> str:
        """Generate combined startup script: rescue_mount.sh + fix script(s).

        Loads rescue_mount.sh, replaces the completion marker with a comment,
        appends fix script(s), then appends the completion marker at the end.
        """
        # Load base rescue mount script
        script_dir = Path(__file__).parent.parent / 'startup_scripts'
        base_script_path = script_dir / 'rescue_mount.sh'

        with open(base_script_path, 'r') as f:
            base_script = f.read()

        # Get original disk name for placeholder replacement
        compute = self._create_tracked_client('repair-script-vm-info')
        vm_info = compute.instances().get(
            project=self.project, zone=self.zone, instance=self.vm_name
        ).execute()
        original_disk = None
        for disk in vm_info.get('disks', []):
            if disk.get('boot'):
                original_disk = disk['source'].split('/')[-1]
                break

        if not original_disk:
            raise ValueError("Could not determine original boot disk name")

        # Validate disk name
        if not re.match(r'^[a-z]([-a-z0-9]*[a-z0-9])?$', original_disk):
            raise ValueError(f"Disk name contains invalid characters: {original_disk}")

        # Replace disk placeholder
        base_script = base_script.replace('DISK_NAME_PLACEHOLDER', original_disk)

        # Remove the completion marker line (we'll add it at the very end)
        # The marker is: echo "GCE-RESCUE-COMPLETE" >&2
        base_script = base_script.replace(
            f'echo "{RESCUE_COMPLETE_MARKER}" >&2',
            f'# GCE-RESCUE-COMPLETE marker moved to end (repair mode)'
        )

        # Get fix scripts for fixable categories
        fixable = self.get_fixable_categories(diagnosis)
        fix_scripts = []
        for category in fixable:
            fix_script = self._get_fix_script(category)
            if fix_script:
                fix_scripts.append(fix_script)

        # Combine: base script + fix scripts + completion marker
        combined = base_script + '\n'
        combined += '\n# === GCE Repair Fix Scripts ===\n'
        combined += 'log "=== Starting repair fixes ==="\n\n'

        for fix_script in fix_scripts:
            combined += fix_script + '\n\n'

        combined += 'log "=== Repair fixes completed ==="\n'
        combined += f'echo "{RESCUE_COMPLETE_MARKER}" >&2\n'
        combined += 'log "=== Startup script completed successfully ==="\n'

        return combined

    def _get_fix_script(self, category: str) -> Optional[str]:
        """Load fix script template for a category."""
        fixes_dir = Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
        script_path = fixes_dir / f'{category}_fix.sh'

        if not script_path.exists():
            self._log_debug(f"No fix script found for category: {category}")
            return None

        with open(script_path, 'r') as f:
            content = f.read()

        # Remove shebang line if present (already in base script)
        if content.startswith('#!/bin/bash'):
            content = content.split('\n', 1)[1]

        return content

    def _parse_repair_results(self) -> Dict[str, Any]:
        """Parse repair results from serial console output.

        Looks for GCE-REPAIR-LINE: and GCE-REPAIR-RESULT: markers.
        Checks both default port and port 2 as fallback.

        Returns:
            Dict with: status, fixed_count, fix_lines, error
        """
        serial_output = ''
        try:
            compute = self._create_tracked_client('repair-serial-parse')
            # Try default port first (matches verify_startup behavior)
            serial_response = compute.instances().getSerialPortOutput(
                project=self.project, zone=self.zone,
                instance=self.vm_name
            ).execute()
            serial_output = serial_response.get('contents', '')

            # If no repair markers found, try port 2 as fallback
            if REPAIR_RESULT_MARKER not in serial_output:
                self._log_debug("No repair markers on default port, trying port 2")
                serial_response = compute.instances().getSerialPortOutput(
                    project=self.project, zone=self.zone,
                    instance=self.vm_name, port=2
                ).execute()
                port2_output = serial_response.get('contents', '')
                if REPAIR_RESULT_MARKER in port2_output:
                    serial_output = port2_output
        except Exception as e:
            self._log_debug(f"Could not fetch serial console: {e}")
            return {
                'status': 'unknown', 'fixed_count': 0,
                'fix_lines': [], 'error': f'Could not read serial console: {e}'
            }

        # Strip control characters that may interfere with marker detection
        serial_output = serial_output.replace('\r', '')

        # Extract repair lines
        fix_lines = []
        for line in serial_output.split('\n'):
            if REPAIR_LINE_MARKER in line:
                idx = line.index(REPAIR_LINE_MARKER)
                fix_lines.append(line[idx + len(REPAIR_LINE_MARKER):].strip())

        # Extract repair result
        status = 'unknown'
        fixed_count = 0
        error = None

        for line in serial_output.split('\n'):
            if REPAIR_RESULT_MARKER in line:
                idx = line.index(REPAIR_RESULT_MARKER)
                result_str = line[idx + len(REPAIR_RESULT_MARKER):].strip()

                if result_str.startswith('SUCCESS:'):
                    status = 'success'
                    try:
                        fixed_count = int(result_str.split(':')[1])
                    except (ValueError, IndexError):
                        fixed_count = len(fix_lines)
                elif result_str.startswith('NO_ISSUES:'):
                    status = 'no_issues'
                    fixed_count = 0
                elif result_str.startswith('FAILED:'):
                    status = 'failed'
                    error = result_str.split(':', 1)[1] if ':' in result_str else 'Unknown'

        if status == 'unknown':
            self._log_debug(
                f"No repair markers found in serial output "
                f"({len(serial_output)} bytes)"
            )

        return {
            'status': status, 'fixed_count': fixed_count,
            'fix_lines': fix_lines, 'error': error
        }

    def _verify_boot_after_repair(self) -> Dict[str, Any]:
        """Check if the VM boots successfully after repair.

        Waits for the VM to generate new serial console output after restore,
        then analyzes it for boot errors.

        Returns:
            Dict with: verified (bool/None), errors (list of error descriptions)
        """
        from ..core.boot_patterns import analyze_serial_output

        BOOT_WAIT_SECONDS = 45
        self._log_debug(
            f"Waiting {BOOT_WAIT_SECONDS}s for VM to boot before verification"
        )
        sys.stdout.write("Verifying boot...")
        sys.stdout.flush()
        time.sleep(BOOT_WAIT_SECONDS)

        try:
            compute = self._create_tracked_client('repair-boot-verify')
            serial_response = compute.instances().getSerialPortOutput(
                project=self.project, zone=self.zone,
                instance=self.vm_name
            ).execute()
            serial_output = serial_response.get('contents', '')

            if not serial_output or len(serial_output.strip()) < 50:
                sys.stdout.write("\r" + " " * 40 + "\r")
                sys.stdout.flush()
                self._log_debug("Serial output too short for boot verification")
                return {'verified': None, 'errors': []}

            diagnosis = analyze_serial_output(
                serial_output=serial_output,
                vm_name=self.vm_name,
                zone=self.zone,
                vm_status='RUNNING'
            )

            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()

            if diagnosis.diagnosis_status == 'healthy':
                self._log_debug("Boot verification: VM is booting normally")
                return {'verified': True, 'errors': []}
            elif diagnosis.diagnosis_status == 'boot_errors_detected':
                error_descs = [
                    f"{e.category}: {e.description}"
                    for e in diagnosis.boot_errors
                ]
                self._log_debug(
                    f"Boot verification: {len(error_descs)} error(s) still detected"
                )
                return {'verified': False, 'errors': error_descs}
            else:
                return {'verified': None, 'errors': []}

        except Exception as e:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()
            self._log_debug(f"Boot verification failed: {e}")
            return {'verified': None, 'errors': []}

    # --- Progress display ---

    def _make_progress_callback(self, phase: str, label_map: Dict[str, str]):
        """Return a callback that updates the current substep display.

        Args:
            phase: Phase name shown to user (e.g. "Rescue", "Restore")
            label_map: Maps raw step labels to user-friendly names
        """
        def callback(raw_label: str):
            display_label = label_map.get(raw_label, raw_label)
            with self._progress_lock:
                self._current_phase = phase
                # Record previous substep in trail before moving to next
                if (self._current_substep
                        and self._current_substep != display_label
                        and (not self._current_line_substeps
                             or self._current_line_substeps[-1]
                             != self._current_substep)):
                    self._current_line_substeps.append(
                        self._current_substep
                    )
                self._current_substep = display_label
        return callback

    def _init_progress(self):
        """Initialize multi-line progress display."""
        console_level = getattr(
            self.logger, 'console_level', self.logger.level
        ) if self.logger else logging.INFO
        self._is_debug_mode = console_level <= logging.DEBUG
        self._progress_phases = []
        self._progress_lock = threading.Lock()
        self._current_phase = ''
        self._current_substep = ''
        self._current_line_substeps: List[str] = []

        if not self._is_debug_mode:
            sys.stdout.write(f"Repairing instance [{self.vm_name}]:\n")
            sys.stdout.flush()
            self._spinner_stop = False
            self._spinner_thread = threading.Thread(
                target=self._run_spinner, daemon=True
            )
            self._spinner_thread.start()

        self._progress_started = True

    def _run_spinner(self):
        """Run spinner showing current phase line with substep trail.

        Output format per phase:
          Rescue:  Stopping VM -> Creating snapshot -> Creating rescue disk..\\
        When phase completes:
          Rescue:  Stopping VM -> Creating snapshot -> Creating rescue disk  done.
        """
        dots = ['.  ', '.. ', '...']
        idx = 0

        while not self._spinner_stop:
            with self._progress_lock:
                phase = self._current_phase
                substep = self._current_substep
                substeps = list(self._current_line_substeps)

            if not phase:
                time.sleep(0.4)
                continue

            # Build the substep trail for the current phase line
            trail = " -> ".join(substeps) if substeps else ""
            if substep and (not substeps or substeps[-1] != substep):
                if trail:
                    trail += f" -> {substep}"
                else:
                    trail = substep

            phase_num = len(self._progress_phases)
            prefix = f"  ({phase_num}/3) {phase + ':':<9}"
            if trail:
                line = f"\r{prefix} {trail}{dots[idx]}"
            else:
                line = f"\r{prefix} {dots[idx]}"

            sys.stdout.write(f"{line:<120}")
            sys.stdout.flush()
            idx = (idx + 1) % len(dots)
            time.sleep(0.4)

    def _update_progress(self, phase: str):
        """Start a new phase, finalizing the previous phase line."""
        with self._progress_lock:
            prev_phase = self._current_phase
            prev_substeps = list(self._current_line_substeps)
            prev_substep = self._current_substep

            self._progress_phases.append(phase)
            self._current_phase = phase
            self._current_substep = ''
            self._current_line_substeps = []

        # Include last active substep in the trail
        if prev_substep and (not prev_substeps
                             or prev_substeps[-1] != prev_substep):
            prev_substeps.append(prev_substep)

        # Finalize previous phase line (if any) as "done."
        if prev_phase and not self._is_debug_mode:
            trail = " -> ".join(prev_substeps) if prev_substeps else ""
            prev_num = len(self._progress_phases) - 1
            prefix = f"  ({prev_num}/3) {prev_phase + ':':<9}"
            if trail:
                final = f"\r{prefix} {trail}  done."
            else:
                final = f"\r{prefix} done."
            sys.stdout.write(f"{final:<120}\n")
            sys.stdout.flush()

        self._log_debug(f"Phase: {phase}")

    def _finish_progress(self, success: bool = True):
        """Finalize the last active phase line with done./FAILED."""
        if not self._progress_started:
            return

        self._spinner_stop = True
        if self._spinner_thread:
            self._spinner_thread.join(timeout=0.5)

        if not self._is_debug_mode:
            with self._progress_lock:
                phase = self._current_phase
                substeps = list(self._current_line_substeps)
                substep = self._current_substep

            # Include current substep if not already recorded
            if substep and (not substeps or substeps[-1] != substep):
                substeps.append(substep)

            trail = " -> ".join(substeps) if substeps else ""
            phase_num = len(self._progress_phases)
            prefix = f"  ({phase_num}/3) {phase + ':':<9}" if phase else "  "
            status_label = "done." if success else "FAILED."

            if trail:
                final = f"\r{prefix} {trail}  {status_label}"
            else:
                final = f"\r{prefix} {status_label}"

            sys.stdout.write(f"{final:<120}\n")
            sys.stdout.flush()
