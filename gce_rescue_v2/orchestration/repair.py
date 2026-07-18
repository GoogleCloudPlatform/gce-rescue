"""
GCE Rescue - Repair Orchestrator

Automates the full repair flow: diagnose -> rescue (with embedded fix) -> restore.
Fix scripts are embedded directly in the startup script - no SSH, no external tools.

Supported fix categories (filesystem/initramfs/grub scripts land with this
change):
    - fstab: Comments out invalid UUID/device/label entries
    - filesystem: Repairs the filesystem (fsck) before the mount attempt
    - initramfs: Rebuilds the initramfs inside the target chroot
    - grub: Reinstalls/regenerates the GRUB configuration

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

from ..core.config import RescueConfig, RestoreConfig, VERSION, build_user_agent
from ..core.fix_catalog import SUPPORTED_FIX_CATEGORIES
from ..operations import DiagnoseOperation
from ..utils.colors import green, red
# Composition helpers shared with the rescue orchestrator (re-exported here
# for backward compatibility; RESCUE_COMPLETE_MARKER is the marker used by
# the rescue startup script).
from .compose import (
    RESCUE_COMPLETE_MARKER, compose_startup_script, strip_shebang,
)
from .rescue import RescueOrchestrator
from .restore import RestoreOrchestrator

# Marker prefixes emitted by fix scripts to serial console
REPAIR_LINE_MARKER = 'GCE-REPAIR-LINE:'
REPAIR_RESULT_MARKER = 'GCE-REPAIR-RESULT:'

# Fix scripts must run in dependency order, regardless of the order categories
# appear in the diagnosis:
#   1. filesystem — fsck first, so later fixes edit files on a clean
#      (mountable) filesystem; its pre-mount block must also run before the
#      base script's mount attempt.
#   2. fstab — mount-config edits on the now-clean filesystem.
#   3. initramfs — rebuild the initrd after config changes.
#   4. grub — last, because GRUB config regeneration must see the rebuilt
#      initrd to reference the correct images.
# Categories not listed here keep their diagnosis order, after the known ones.
FIX_EXECUTION_ORDER = ['filesystem', 'fstab', 'initramfs', 'grub']

# Minimum verification timeouts (seconds) per fix category. The OS default
# (300s Linux) only covers the disk mount; a forced fsck, a tool install, or
# an initramfs rebuild inside the startup script routinely runs longer, and a
# verification timeout mid-fix misreports the repair as mount_failed while
# the fix is still writing to the disk. An explicit --verification-timeout
# always wins over these floors.
REPAIR_VERIFICATION_FLOOR = {
    'filesystem': 1800,  # e2fsck -f / xfs_repair scale with disk size
    'initramfs': 900,    # rebuild + possible tool install
    'grub': 900,         # grub-install + config regeneration in chroot
}

# Line the base mount script logs at startup-script start. Serial output can
# span several boots of the same VM within one rescue session; only markers
# after the LAST occurrence of this banner belong to the current repair run
# (earlier ones would double-count a previous attempt's fixes).
BOOT_BANNER = '=== GCE Rescue Auto-Mount Started ==='

# Per-session completion token the rescue orchestrator substitutes into the
# startup script (SESSION_ID_PLACEHOLDER). After an interrupted repair the
# token is still embedded in the VM's startup-script metadata, so resume()
# can recover it and check the guest attribute the fix session would have
# set on completion.
_SESSION_TOKEN_RE = re.compile(r'COMPLETE-[0-9a-f]{12}')

# Device-name heuristics for filesystem findings: on the ORIGINAL (booting)
# VM the boot disk is the first device (sda / nvme0n1); LVM roots surface as
# dm-* / mapper names. Anything clearly second-disk (sdb+, nvme1+, vdb+) is
# not repairable by rescuing the boot disk.
_BOOT_DEVICE_RE = re.compile(
    r'\b(sda\d*|nvme0n1(?:p\d+)?|dm-\d+|mapper/[\w-]+)\b', re.IGNORECASE
)
_NONBOOT_DEVICE_RE = re.compile(
    r'\b(sd[b-z]\d*|nvme[1-9]\d*n\d+(?:p\d+)?|vd[b-z]\d*)\b', re.IGNORECASE
)

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
                 config: RescueConfig = None, logger=None, log_file: str = None,
                 session_id: str = None, mode: str = None):
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.config = config or RescueConfig()
        self.logger = logger
        self.log_file = log_file
        self.session_id = session_id
        self.mode = mode

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

        # When True, _init_progress() skips the "Repairing instance" header
        # (caller prints its own concise header before execute())
        self._suppress_header = False

    def _log_info(self, message: str):
        if self.logger:
            self.logger.info(message)

    def _log_debug(self, message: str):
        if self.logger:
            self.logger.debug(f"[Repair] {message}", stacklevel=2)

    def _log_error(self, message: str):
        if self.logger:
            self.logger.error(message)

    def _ua(self, step: str) -> str:
        """Build User-Agent string for the given step."""
        return build_user_agent(
            session_id=self.session_id, command='repair',
            mode=self.mode, step=step
        )

    def _create_tracked_client(self, user_agent: str):
        """Create a compute client with custom User-Agent for analytics."""
        credentials = self.compute._http.credentials

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
            tracking_label=self._ua('val-iam')
        ))
        runner.add(VMStateValidator(
            self.compute, self.project, self.zone, self.vm_name,
            tracking_label=self._ua('val-vm-state')
        ))

        results = runner.run_all(self.logger)
        if not results.all_passed():
            results.print_failures()
            return False

        # Diagnosis-driven repair is Linux-only; Windows is supported only
        # with a custom fix script (--fix-script)
        from ..utils.os_detection import detect_os_type
        compute = self._create_tracked_client(self._ua('val-os'))
        vm_info = compute.instances().get(
            project=self.project, zone=self.zone, instance=self.vm_name
        ).execute()
        os_type = detect_os_type(vm_info)
        if os_type == 'windows' and not self.config.fix_script:
            self._log_error("Automated repair is only supported for Linux VMs.")
            print("", file=sys.stderr)
            print("For Windows VMs, supply a custom fix script:", file=sys.stderr)
            print(
                f"  $ gce-rescue repair {self.vm_name} "
                f"--zone={self.zone} --project={self.project} "
                f"--fix-script=FIX.ps1",
                file=sys.stderr
            )
            print("", file=sys.stderr)
            print("Or use rescue mode for manual repair:", file=sys.stderr)
            print(
                f"  $ gce-rescue rescue {self.vm_name} "
                f"--zone={self.zone} --project={self.project}",
                file=sys.stderr
            )
            return False

        # Validate a custom fix script's pre-mount markers BEFORE any VM
        # mutation (composition otherwise fails at rescue step 6, after the
        # VM was stopped and disks swapped).
        if self.config.fix_script:
            from .compose import extract_premount_blocks
            try:
                extract_premount_blocks(strip_shebang(self.config.fix_script))
            except ValueError as e:
                self._log_error(f"Invalid --fix-script: {e}")
                return False

        # Verify fix scripts exist for all supported categories
        fixes_dir = Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
        for cat in SUPPORTED_FIX_CATEGORIES:
            script_path = fixes_dir / f'{cat}_fix.sh'
            if not script_path.exists():
                self._log_error(
                    f"Fix script missing for category '{cat}': {script_path}\n"
                    f"The repair tool may not be installed correctly. "
                    f"Try reinstalling: pip install --force-reinstall ."
                )
                return False

        self._log_debug("Validation passed")
        return True

    def diagnose(self) -> Optional[Dict[str, Any]]:
        """Run diagnosis and return the diagnosis dict (or None on failure)."""
        diagnose_op = DiagnoseOperation(
            self.compute, self.project, self.zone, self.logger
        )
        result = diagnose_op.execute(
            self.vm_name, tracking_label=self._ua('diagnose'), stabilize=True
        )

        if not result.success:
            self._log_error(f"Diagnosis failed: {result.message}")
            return None

        return result.rollback_data

    def _filesystem_errors_on_boot_disk(self, diagnosis: Dict[str, Any]) -> bool:
        """Whether the filesystem findings could concern the boot disk.

        filesystem patterns survive boot success, so a healthy-booting VM
        with a corrupt SECONDARY disk still diagnoses 'filesystem' - but a
        rescue cycle only ever fscks the boot disk, so repairing it would
        stop the VM for nothing and report NO_ISSUES. Returns False only
        when EVERY filesystem finding names a clearly non-boot device
        (sdb+/nvme1+/vdb+); ambiguous findings keep the category fixable.
        """
        saw_any = False
        for err in diagnosis.get('boot_errors', []):
            if err.get('category') != 'filesystem':
                continue
            saw_any = True
            text = ' '.join(
                str(err.get(key, ''))
                for key in ('detected_pattern', 'description', 'evidence')
            )
            if _BOOT_DEVICE_RE.search(text) or not _NONBOOT_DEVICE_RE.search(text):
                return True
        return not saw_any

    def get_fixable_categories(self, diagnosis: Dict[str, Any]) -> List[str]:
        """Return list of categories from diagnosis that have fix scripts.

        The list is sorted into FIX_EXECUTION_ORDER (filesystem, fstab,
        initramfs, grub) so fix scripts always compose in dependency order;
        unknown categories keep their diagnosis order after the known ones.
        filesystem is excluded when its findings only name non-boot devices
        (see _filesystem_errors_on_boot_disk).
        """
        categories = []
        seen = set()
        for err in diagnosis.get('boot_errors', []):
            cat = err.get('category', '')
            if cat in SUPPORTED_FIX_CATEGORIES and cat not in seen:
                seen.add(cat)
                categories.append(cat)
        if ('filesystem' in categories
                and not self._filesystem_errors_on_boot_disk(diagnosis)):
            categories.remove('filesystem')
            self._log_debug(
                "filesystem findings reference only non-boot devices; "
                "excluded from rescue-based repair"
            )
        # Stable sort: known categories in execution order, unknowns after
        # them in their original (diagnosis) order.
        categories.sort(
            key=lambda c: FIX_EXECUTION_ORDER.index(c)
            if c in FIX_EXECUTION_ORDER else len(FIX_EXECUTION_ORDER)
        )
        return categories

    def get_unfixable_categories(self, diagnosis: Dict[str, Any]) -> List[str]:
        """Return list of categories from diagnosis that lack fix scripts.

        Includes filesystem when its findings only name non-boot devices:
        the category has a fix script, but rescuing the boot disk cannot
        repair a secondary disk, so the user gets manual guidance instead.
        """
        categories = []
        seen = set()
        for err in diagnosis.get('boot_errors', []):
            cat = err.get('category', '')
            if cat not in SUPPORTED_FIX_CATEGORIES and cat not in seen:
                seen.add(cat)
                categories.append(cat)
        if ('filesystem' not in categories
                and any(e.get('category') == 'filesystem'
                        for e in diagnosis.get('boot_errors', []))
                and not self._filesystem_errors_on_boot_disk(diagnosis)):
            categories.append('filesystem')
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

        # Destructive-fix guard: filesystem repair runs fsck/xfs_repair on
        # the user's original disk - the pre-rescue snapshot is the ONLY
        # rollback for whatever fsck rewrites, so it is non-negotiable here.
        if 'filesystem' in fixable and not self.config.create_snapshot:
            self._log_error(
                "Filesystem repair runs a destructive fsck on the original "
                "disk and requires the pre-rescue snapshot as its rollback. "
                "Re-run without --no-snapshot."
            )
            return {'status': 'error', 'fixed_count': 0, 'fix_lines': [],
                    'error': 'filesystem repair requires a snapshot '
                             '(--no-snapshot was given)',
                    'snapshot_name': None, 'duration_seconds': 0}

        # Generate repair startup script
        repair_script = self._generate_repair_script(diagnosis)
        self._log_debug(f"Generated repair script ({len(repair_script)} bytes)")

        return self._run_repair_flow(repair_script, fixable_categories=fixable)

    def execute_custom(self) -> Dict[str, Any]:
        """Execute repair with the custom fix script from config (--fix-script).

        Skips diagnosis entirely: the engineer supplied the fix, so the flow is
        rescue (mount + custom script) -> parse results -> restore -> verify.

        The fix script is propagated onto the inner rescue config (instead of
        a full startup-script override) so the rescue orchestrator composes it
        per-OS — bash for Linux, PowerShell for Windows — with all placeholders
        (disk name, Windows rescue password) resolved as usual.

        Returns:
            Dict with keys: status, fixed_count, fix_lines, error,
            snapshot_name, duration_seconds
        """
        return self._run_repair_flow(fix_script=self.config.fix_script)

    def _run_repair_flow(self, repair_script: Optional[str] = None,
                         fix_script: Optional[str] = None,
                         fixable_categories: Optional[List[str]] = None
                         ) -> Dict[str, Any]:
        """Run the repair flow with the given fix payload.

        Shared by diagnosis-driven repair (execute), which passes a fully
        composed startup script as repair_script plus its fixable_categories
        (used to scale the verification timeout and to make the snapshot
        mandatory for destructive categories), and custom fix scripts
        (execute_custom), which pass fix_script for per-OS composition inside
        the rescue orchestrator. Flow: rescue with the fix embedded -> parse
        repair results from serial console -> restore -> post-restore boot
        check.
        """
        # Initialize progress display
        self._init_progress()
        start_time = time.time()

        try:
            # Phase 1: Rescue with embedded fix script
            self._update_progress("Rescue")
            rescue_config = RescueConfig()
            rescue_config.create_snapshot = self.config.create_snapshot
            rescue_config.force = self.config.force
            # Propagate custom rescue image settings (issue #102)
            rescue_config.custom_rescue_image = self.config.custom_rescue_image
            rescue_config.custom_rescue_image_size_gb = self.config.custom_rescue_image_size_gb
            # Custom fix script (--fix-script): composed per-OS by the rescue
            # orchestrator's startup-script generation
            rescue_config.fix_script = fix_script
            # Propagate explicit --verification-timeout (issue #123); OS-aware
            # defaults come from RescueConfig itself.
            rescue_config.verification_timeout_override = (
                self.config.verification_timeout_override
            )

            categories = fixable_categories or []
            # fsck rewrites the original disk in place; if the snapshot step
            # fails there is no rollback, so its failure must abort the
            # rescue instead of being logged and skipped.
            if 'filesystem' in categories:
                rescue_config.require_snapshot = True
            # Raise the verification budget for long-running fix categories
            # (fsck, initramfs rebuild) unless the user set an explicit
            # timeout - see REPAIR_VERIFICATION_FLOOR.
            if rescue_config.verification_timeout_override is None and categories:
                floor = max(
                    (REPAIR_VERIFICATION_FLOOR.get(c, 0) for c in categories),
                    default=0,
                )
                if floor > rescue_config.effective_verification_timeout('linux'):
                    rescue_config.verification_timeout_override = floor
                    self._log_debug(
                        f"Verification timeout raised to {floor}s for "
                        f"long-running fix categories {categories}"
                    )

            rescue = RescueOrchestrator(
                compute=self.compute, project=self.project, zone=self.zone,
                vm_name=self.vm_name, config=rescue_config, logger=self.logger,
                log_file=self.log_file, startup_script_override=repair_script,
                suppress_progress=True,
                progress_callback=self._make_progress_callback(
                    "Rescue", RESCUE_SUBSTEP_LABELS
                ),
                session_id=self.session_id, mode=self.mode
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

            # Zip per-marker outcomes back onto the requested categories.
            # Scripts compose in exactly the get_fixable_categories() order
            # and each emits ONE result marker, so serial marker order ==
            # category order. A length mismatch means serial dropped a
            # marker - attribution would be a guess, so the key is omitted
            # entirely (correctness over completeness). Custom --fix-script
            # flows have no categories and never get the key.
            if fixable_categories:
                marker_results = repair_results.get('marker_results', [])
                if len(marker_results) == len(fixable_categories):
                    repair_results['category_outcomes'] = [
                        {'category': category, 'kind': marker['kind'],
                         'count': marker['count'], 'reason': marker['reason']}
                        for category, marker
                        in zip(fixable_categories, marker_results)
                    ]
                else:
                    self._log_debug(
                        f"Marker/category count mismatch "
                        f"({len(marker_results)} markers for "
                        f"{len(fixable_categories)} categories); omitting "
                        f"per-category outcomes"
                    )

            # If mount failed (no completion marker found by verify), don't restore
            if not rescue.verification_succeeded:
                self._finish_progress(False)
                self._log_error(
                    "Startup script did not complete. The disk may not have mounted."
                )
                # Fix scripts emit LINE/RESULT markers before the completion
                # marker, so anything already parsed explains WHY the mount
                # failed (e.g. an unrepairable filesystem).
                if repair_results.get('error'):
                    self._log_error(
                        f"Reported by the fix script: {repair_results['error']}"
                    )
                for fix_line in repair_results.get('fix_lines', []):
                    self._log_error(f"  {fix_line}")
                self._log_error(
                    "A long-running fix (fsck, initramfs rebuild) may still "
                    "be executing - check the serial console before "
                    "restoring, or the restore can interrupt it mid-write."
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
                    f"  $ gce-rescue restore {self.vm_name} "
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
                ),
                session_id=self.session_id, mode=self.mode
            )
            if not restore.execute():
                self._finish_progress(False)
                self._log_error("Restore phase failed.")
                self._log_error(
                    "VM may be in rescue mode. Try restoring manually:"
                )
                self._log_error(
                    f"  $ gce-rescue restore {self.vm_name} "
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
            compute = self._create_tracked_client(self._ua('snapshot-lookup'))
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

    def _rescue_fixes_completed(self) -> bool:
        """Whether the interrupted session's fix scripts COMPLETED.

        resume() restores immediately, and restore stops the VM - if a fix
        is still running (fsck mid-repair, initramfs mid-rebuild) that stop
        interrupts it mid-write on the very disk being repaired. Completion
        is confirmed by ANY of:
          1. Session token: the rescue startup script (still in the VM's
             metadata after an interruption) embeds a per-session
             COMPLETE-<12hex> token; the script sets the gce-rescue/status
             guest attribute to that exact token as its final step. A match
             is the deterministic signal rescue verification itself uses.
          2. Serial fallback: the current boot's serial output contains the
             completion marker (covers older sessions and guest attributes
             being unavailable).
        Returns False when neither confirms - the caller must NOT restore.
        """
        try:
            compute = self._create_tracked_client(self._ua('resume-check'))
        except Exception as e:
            self._log_debug(f"Could not create tracked client: {e}")
            compute = self.compute

        # Signal 1: session token from metadata matches the guest attribute
        token = None
        try:
            vm_info = compute.instances().get(
                project=self.project, zone=self.zone, instance=self.vm_name
            ).execute()
            for item in vm_info.get('metadata', {}).get('items', []):
                if item.get('key') in ('startup-script',
                                       'windows-startup-script-ps1'):
                    match = _SESSION_TOKEN_RE.search(item.get('value') or '')
                    if match:
                        token = match.group(0)
                        break
        except Exception as e:
            self._log_debug(f"Could not read startup-script metadata: {e}")

        if token:
            try:
                resp = compute.instances().getGuestAttributes(
                    project=self.project, zone=self.zone,
                    instance=self.vm_name, queryPath='gce-rescue/status'
                ).execute()
                values = [str(resp.get('variableValue', ''))]
                values += [
                    str(item.get('value', ''))
                    for item in resp.get('queryValue', {}).get('items', [])
                ]
                if any(v.strip().upper() == token.upper() for v in values):
                    self._log_debug(
                        "Fix completion confirmed via session guest attribute"
                    )
                    return True
            except Exception as e:
                self._log_debug(
                    f"Could not read completion guest attribute: {e}"
                )

        # Signal 2 (fallback): completion marker on the current boot's serial
        try:
            serial_response = compute.instances().getSerialPortOutput(
                project=self.project, zone=self.zone, instance=self.vm_name
            ).execute()
            windowed = self._window_to_last_boot(
                serial_response.get('contents', '')
            )
            if RESCUE_COMPLETE_MARKER in windowed:
                self._log_debug("Fix completion confirmed via serial marker")
                return True
        except Exception as e:
            self._log_debug(f"Could not check serial for completion: {e}")

        return False

    def resume(self) -> Dict[str, Any]:
        """Resume an interrupted repair: parse results from serial console + restore.

        Called when the VM is already in rescue mode from a previous repair attempt.
        Skips the rescue phase entirely.

        Refuses to restore ('fix_in_progress') when the previous session's
        fix scripts cannot be confirmed complete - restoring stops the VM,
        which would interrupt a still-running fsck/rebuild mid-write.

        Returns:
            Dict with keys: status, fixed_count, fix_lines, error,
            snapshot_name, duration_seconds
        """
        self._total_steps = 2  # Repair, Restore (rescue already done)

        # Look up snapshot from the original rescue phase
        snapshot_name = self._find_rescue_snapshot()

        # Safety guard: only restore once the fix session is confirmed done.
        if not self._rescue_fixes_completed():
            self._log_error(
                "Cannot confirm that the previous repair's fix scripts have "
                "finished - the fix may still be running."
            )
            self._log_error(
                "Restoring now would stop the VM and could interrupt a "
                "long-running fix (fsck, initramfs rebuild) mid-write."
            )
            self._log_error("")
            self._log_error("Check the serial console for progress:")
            self._log_error(
                f"  $ gcloud compute instances get-serial-port-output "
                f"{self.vm_name} --zone={self.zone} --project={self.project}"
            )
            self._log_error("")
            self._log_error("Re-run repair once the fix has finished:")
            self._log_error(
                f"  $ gce-rescue repair {self.vm_name} "
                f"--zone={self.zone} --project={self.project}"
            )
            self._log_error("")
            self._log_error(
                "To restore anyway (deliberate manual override):"
            )
            self._log_error(
                f"  $ gce-rescue restore {self.vm_name} "
                f"--zone={self.zone} --project={self.project}"
            )
            return {
                'status': 'fix_in_progress', 'fixed_count': 0,
                'fix_lines': [],
                'error': 'Fix completion could not be confirmed; '
                         'the fix may still be running',
                'snapshot_name': snapshot_name,
                'duration_seconds': 0
            }

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
                ),
                session_id=self.session_id, mode=self.mode
            )
            if not restore.execute():
                self._finish_progress(False)
                self._log_error("Restore phase failed.")
                self._log_error(
                    "VM may be in rescue mode. Try restoring manually:"
                )
                self._log_error(
                    f"  $ gce-rescue restore {self.vm_name} "
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

    def _extract_fstab_targets(self, diagnosis: Dict[str, Any]) -> List[str]:
        """Extract specific identifiers from fstab error patterns for targeted fixing.

        Parses detected_pattern strings from diagnosis boot_errors to extract
        UUIDs, device paths, and mount points that the fix script should target.
        This prevents false-positive commenting of legitimate secondary disk entries.

        Returns:
            Deduplicated list of identifier strings (UUIDs, device names, mount paths).
        """
        targets = []
        seen = set()

        extraction_patterns = [
            # UUID from "UUID=xxx" or "can't find UUID=xxx"
            (r'UUID=([\w-]+)', 1),
            # UUID from /dev/disk/by-uuid/xxx paths
            (r'/by-uuid/([\w-]+)', 1),
            # Systemd escaped device path: dev-disk-by\x2duuid-UUID.device
            # systemd replaces / with - and escapes special chars
            (r'dev-disk-by\\x2duuid-([\w-]+)\.device', 1),
            # Systemd escaped device path (already unescaped): dev-disk-by-uuid-UUID.device
            (r'dev-disk-by-uuid-([\w-]+)\.device', 1),
            # PARTUUID from "PARTUUID=xxx"
            (r'PARTUUID=([\w-]+)', 1),
            # Systemd escaped PARTUUID: dev-disk-by\x2dpartuuid-UUID.device
            (r'dev-disk-by\\x2dpartuuid-([\w-]+)\.device', 1),
            (r'dev-disk-by-partuuid-([\w-]+)\.device', 1),
            # Raw device like /dev/sdb1
            (r'/dev/(sd[a-z]+\d*)', 1),
            # Raw NVMe devices (e.g. /dev/nvme0n1, /dev/nvme1n1p2)
            (r'/dev/(nvme\d+n\d+(?:p\d+)?)', 1),
            # Systemd unit name -> mount point (e.g., "mnt-data.mount" -> /mnt/data)
            (r'for ([\w.-]+)\.mount', 1),
            # Disk label from /dev/disk/by-label/xxx
            (r'/by-label/([\w-]+)', 1),
            # LABEL= entries
            (r'LABEL=([\w-]+)', 1),
        ]

        for err in diagnosis.get('boot_errors', []):
            if err.get('category') != 'fstab':
                continue
            pattern_text = err.get('detected_pattern', '')
            if not pattern_text:
                continue

            # Decode systemd hex escapes (e.g. \x2d -> '-') before matching
            pattern_text = re.sub(
                r'\\x([0-9a-fA-F]{2})',
                lambda m: chr(int(m.group(1), 16)),
                pattern_text
            )

            for regex, group in extraction_patterns:
                match = re.search(regex, pattern_text, re.IGNORECASE)
                if match:
                    value = match.group(group)
                    # Convert systemd unit name to mount path: mnt-data -> /mnt/data
                    if regex.startswith(r'for '):
                        value = '/' + value.replace('-', '/')
                    if value not in seen:
                        seen.add(value)
                        targets.append(value)

        if not targets:
            self._log_debug(
                "No fstab repair targets extracted from diagnosis patterns"
            )
        else:
            self._log_debug(f"Extracted fstab repair targets: {targets}")

        return targets

    def _load_base_script(self) -> str:
        """Load rescue_mount.sh with the disk placeholder resolved.

        Looks up the VM's boot disk name via the API, validates it, and
        substitutes it into the base mount script.

        Raises:
            ValueError: If the boot disk cannot be determined or has an
                invalid name.
        """
        # Load base rescue mount script
        script_dir = Path(__file__).parent.parent / 'startup_scripts'
        base_script_path = script_dir / 'rescue_mount.sh'

        with open(base_script_path, 'r', encoding='utf-8') as f:
            base_script = f.read()

        # Get original disk name for placeholder replacement
        compute = self._create_tracked_client(self._ua('script-vm-info'))
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
        return base_script.replace('DISK_NAME_PLACEHOLDER', original_disk)

    def _generate_repair_script(self, diagnosis: Dict[str, Any]) -> str:
        """Generate combined startup script: rescue_mount.sh + fix script(s).

        Loads rescue_mount.sh, appends the fix script(s) selected from
        diagnosis, and relocates the completion marker to the very end.
        """
        base_script = self._load_base_script()

        # Get fix scripts for fixable categories
        fixable = self.get_fixable_categories(diagnosis)
        fix_scripts = []
        for category in fixable:
            fix_script = self._get_fix_script(category)
            fix_scripts.append(fix_script)

        if not fix_scripts:
            raise ValueError(
                f"No fix scripts were loaded for categories {fixable}. "
                f"Cannot proceed with repair."
            )

        # Extract repair targets from diagnosis for targeted fixing
        targets = self._extract_fstab_targets(diagnosis)

        return compose_startup_script(base_script, fix_scripts, targets)

    def _get_fix_script(self, category: str) -> str:
        """Load fix script template for a category.

        Raises:
            FileNotFoundError: If the fix script file does not exist.
            ValueError: If the fix script file is empty.
        """
        fixes_dir = Path(__file__).parent.parent / 'startup_scripts' / 'fixes'
        script_path = fixes_dir / f'{category}_fix.sh'

        if not script_path.exists():
            raise FileNotFoundError(
                f"Fix script missing for category '{category}': {script_path}. "
                f"Cannot proceed with repair — the fix would not be applied."
            )

        with open(script_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.strip():
            raise ValueError(
                f"Fix script for category '{category}' is empty: {script_path}. "
                f"Cannot proceed with repair — the fix would not be applied."
            )

        # Remove shebang line if present (already in base script)
        if content.startswith('#!/bin/bash'):
            content = content.split('\n', 1)[1]

        return content

    def _parse_repair_results(self) -> Dict[str, Any]:
        """Parse repair results from serial console output.

        Looks for GCE-REPAIR-LINE: and GCE-REPAIR-RESULT: markers.
        Checks both default port and port 2 as fallback.

        Each fix script emits exactly one GCE-REPAIR-RESULT marker, so a
        multi-category repair produces several. They are aggregated into the
        single status contract the CLI consumes (cli/repair.py):
          - fixed_count is the SUM of all SUCCESS counts.
          - 'failed' if ANY script reported FAILED; error joins all failure
            reasons with '; '. Partial success is represented within this
            status: fixed_count and fix_lines still carry the fixes that DID
            apply, which the CLI's 'failed' branch already prints.
          - else 'success' if any script reported SUCCESS.
          - else 'no_issues' if any script reported NO_ISSUES.
          - else 'unknown' (no markers found).

        Alongside the aggregate, marker_results preserves the PER-SCRIPT
        outcomes in serial order (one entry per GCE-REPAIR-RESULT marker):
        {'kind': 'success'|'no_issues'|'failed', 'count': int,
        'reason': Optional[str]}. Fix scripts compose in category order, so
        the caller can zip this back onto the categories it requested.

        Returns:
            Dict with: status, fixed_count, fix_lines, error, marker_results
        """
        serial_output = ''
        try:
            compute = self._create_tracked_client(self._ua('serial-parse'))
            # Try default port first (matches verify_startup behavior)
            serial_response = compute.instances().getSerialPortOutput(
                project=self.project, zone=self.zone,
                instance=self.vm_name
            ).execute()
            serial_output = self._window_to_last_boot(
                serial_response.get('contents', '')
            )

            # If no repair markers found, try port 2 as fallback
            if REPAIR_RESULT_MARKER not in serial_output:
                self._log_debug("No repair markers on default port, trying port 2")
                serial_response = compute.instances().getSerialPortOutput(
                    project=self.project, zone=self.zone,
                    instance=self.vm_name, port=2
                ).execute()
                port2_output = self._window_to_last_boot(
                    serial_response.get('contents', '')
                )
                if REPAIR_RESULT_MARKER in port2_output:
                    serial_output = port2_output
        except Exception as e:
            self._log_debug(f"Could not fetch serial console: {e}")
            return {
                'status': 'unknown', 'fixed_count': 0,
                'fix_lines': [], 'error': f'Could not read serial console: {e}',
                'marker_results': []
            }

        # Single pass: collect fix lines and aggregate result markers. The
        # per-segment counter tracks [FIXED] lines since the previous result
        # marker, so a SUCCESS marker with an unparseable count falls back to
        # the count of ITS OWN script's fixes (not every script's lines).
        fix_lines = []
        saw_success = False
        saw_no_issues = False
        fixed_count = 0
        segment_fixed_lines = 0
        failures: List[str] = []
        marker_results: List[Dict[str, Any]] = []

        for line in serial_output.split('\n'):
            if REPAIR_LINE_MARKER in line:
                idx = line.index(REPAIR_LINE_MARKER)
                content = line[idx + len(REPAIR_LINE_MARKER):].strip()
                fix_lines.append(content)
                if content.startswith('[FIXED]'):
                    segment_fixed_lines += 1
            elif REPAIR_RESULT_MARKER in line:
                idx = line.index(REPAIR_RESULT_MARKER)
                result_str = line[idx + len(REPAIR_RESULT_MARKER):].strip()

                if result_str.startswith('SUCCESS:'):
                    saw_success = True
                    try:
                        count = int(result_str.split(':')[1])
                    except (ValueError, IndexError):
                        count = segment_fixed_lines
                    fixed_count += count
                    marker_results.append(
                        {'kind': 'success', 'count': count, 'reason': None}
                    )
                elif result_str.startswith('NO_ISSUES:'):
                    saw_no_issues = True
                    marker_results.append(
                        {'kind': 'no_issues', 'count': 0, 'reason': None}
                    )
                elif result_str.startswith('FAILED:'):
                    reason = result_str.split(':', 1)[1] if ':' in result_str else 'Unknown'
                    failures.append(reason)
                    marker_results.append(
                        {'kind': 'failed', 'count': 0, 'reason': reason}
                    )
                segment_fixed_lines = 0

        error = None
        if failures:
            # Any failure makes the whole repair 'failed'; fixed_count and
            # fix_lines still reflect the fixes that DID apply (partial
            # success), which the CLI's failed branch prints.
            status = 'failed'
            error = '; '.join(failures)
        elif saw_success:
            status = 'success'
        elif saw_no_issues:
            status = 'no_issues'
            fixed_count = 0
        else:
            status = 'unknown'
            fixed_count = 0

        if status == 'unknown':
            self._log_debug(
                f"No repair markers found in serial output "
                f"({len(serial_output)} bytes)"
            )

        return {
            'status': status, 'fixed_count': fixed_count,
            'fix_lines': fix_lines, 'error': error,
            'marker_results': marker_results
        }

    @staticmethod
    def _window_to_last_boot(serial_output: str) -> str:
        """Slice serial output to the current boot's startup-script run.

        Serial output accumulates across VM restarts within a session, so
        markers from a PREVIOUS repair attempt would be aggregated (and
        double-counted) into this one. Only content after the last mount
        banner belongs to the current run; output without the banner is
        returned whole (pre-banner failures, custom scripts).
        """
        serial_output = serial_output.replace('\r', '')
        idx = serial_output.rfind(BOOT_BANNER)
        if idx != -1:
            return serial_output[idx:]
        return serial_output

    def _verify_boot_after_repair(self) -> Dict[str, Any]:
        """Check if the VM boots successfully after repair.

        Waits for the VM to generate new serial console output after restore,
        then analyzes it for boot errors.

        Returns:
            Dict with: verified (bool/None), errors (list of error descriptions)
        """
        from ..core.diagnosis import analyze_serial_output

        BOOT_WAIT_SECONDS = 45
        self._log_debug(
            f"Waiting {BOOT_WAIT_SECONDS}s for VM to boot before verification"
        )
        for remaining in range(BOOT_WAIT_SECONDS, 0, -1):
            sys.stdout.write(
                f"\rVerifying boot (waiting {remaining}s for serial output)..."
            )
            sys.stdout.flush()
            time.sleep(1)

        try:
            compute = self._create_tracked_client(self._ua('boot-verify'))
            serial_response = compute.instances().getSerialPortOutput(
                project=self.project, zone=self.zone,
                instance=self.vm_name
            ).execute()
            serial_output = serial_response.get('contents', '')

            if not serial_output or len(serial_output.strip()) < 50:
                sys.stdout.write("\r" + " " * 60 + "\r")
                sys.stdout.flush()
                self._log_debug("Serial output too short for boot verification")
                return {'verified': None, 'errors': []}

            diagnosis = analyze_serial_output(
                serial_output=serial_output,
                vm_name=self.vm_name,
                zone=self.zone,
                vm_status='RUNNING'
            )

            sys.stdout.write("\r" + " " * 60 + "\r")
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
            sys.stdout.write("\r" + " " * 60 + "\r")
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
            if not self._suppress_header:
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
            prefix = f"  ({phase_num}/{self._total_steps}) {phase + ':':<9}"
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
            prefix = f"  ({prev_num}/{self._total_steps}) {prev_phase + ':':<9}"
            done = green("done.")
            if trail:
                final = f"\r{prefix} {trail}  {done}"
            else:
                final = f"\r{prefix} {done}"
            sys.stdout.write(f"{final:<120}\n")
            sys.stdout.flush()

        self._log_debug(f"Phase: {phase}")

    def _finish_progress(self, success: bool = True, error: str = None):
        """Finalize the last active phase line with done./FAILED.

        When `error` (a pre-formatted, user-facing block) is supplied for a
        failure, it is printed AFTER the FAILED line so it never interleaves
        with the live spinner. Operations record the same detail to the log
        file; in debug mode the spinner is off and the detail is already in the
        console logs, so it is not reprinted here.
        """
        if self._progress_started:
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
                prefix = f"  ({phase_num}/{self._total_steps}) {phase + ':':<9}" if phase else "  "
                status_label = green("done.") if success else red("FAILED.")

                if trail:
                    final = f"\r{prefix} {trail}  {status_label}"
                else:
                    final = f"\r{prefix} {status_label}"

                sys.stdout.write(f"{final:<120}\n")
                sys.stdout.flush()

        if error and not getattr(self, '_is_debug_mode', False):
            print(f"\n{error}", file=sys.stderr)
