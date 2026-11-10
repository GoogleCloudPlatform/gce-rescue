"""
Repair for the startup_script category (#203).

A startup script that exits non-zero runs from instance metadata, not from
the boot disk, so this repair needs neither rescue mode nor a disk swap:

  1. Save each failing startup-script key as gce-rescue-disabled-<key> and
     remove the key, in one setMetadata call guarded by the fingerprint of
     the metadata the change was computed from.
  2. Reset the VM.
  3. Read only the serial output written after the reset and check that the
     new boot completes without startup-script (or other) failures.

The saved copy has its own prefix rather than rescue-backup-*: restore turns
every rescue-backup-<key> back into <key>, so a later rescue/restore of the
VM would quietly re-enable the failing script.

Every key that failed in the latest boot is disabled in one run (diagnosis
names one per category), so a layered bootstrap (startup-script-url plus
startup-script) needs a single reset.

Re-running after an interruption is safe: a key that an earlier run already
disabled is not touched again; the VM is reset and verified.

Linux only: the keys handled are startup-script and startup-script-url.
"""

import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..core.diagnosis import (
    BOOT_ERROR_PATTERNS, analyze_serial_output, latest_boot_completed,
    latest_boot_start,
)
from ..operations import ResetVMOperation, SetMetadataOperation
from ..operations.base import extract_error_message

# Instance metadata keys that hold a Linux startup script.
STARTUP_SCRIPT_KEYS = ('startup-script', 'startup-script-url')

# A disabled script is saved under this prefix + its original key.
DISABLED_KEY_PREFIX = 'gce-rescue-disabled-'

# Post-reset boot check: the first look comes after one poll interval; the
# wait ends as soon as the new boot reaches userspace and the metadata script
# runner is done. Startup scripts run at the end of boot, so the budget
# covers a full boot plus slow units.
BOOT_WAIT_SECONDS = 240
POLL_SECONDS = 15

# Printed by google_metadata_script_runner once the startup event is handled.
# Boot-success markers such as "Started ssh.service" can come before the
# scripts run, so the check also waits for this line; otherwise a remaining
# failing script (another key, or one inherited from project metadata) could
# fail after the check had already passed.
_SCRIPTS_DONE_RE = re.compile(
    r'Finished running startup scripts|No startup scripts to run')

# The new boot stopped and will not finish on its own. Waiting stops here
# rather than on the first finding: a finding can show while the boot is
# still waiting (e.g. systemd's 90s device timeout for a bad fstab entry),
# and a re-run of repair in that window would see a boot in progress.
_BOOT_STUCK_RE = re.compile(r'You are in emergency mode|Kernel panic - not syncing')

# A key named in a failure line, e.g. "startup-script exit status 1" or
# 'Script "startup-script-url" failed'. The look-behind keeps
# windows-startup-script-ps1 and sysprep-specialize-script out.
_FAILED_KEY_RE = re.compile(r'(?<![\w-])(startup-script(?:-url)?)(?![\w-])')

# The startup_script failure regexes from diagnose_rules/startup_script.yaml,
# so the repair finds failure lines exactly as diagnose does.
_FAILURE_RES = [re.compile(regex, re.MULTILINE | re.IGNORECASE)
                for pattern in BOOT_ERROR_PATTERNS
                if pattern.category == 'startup_script'
                for regex in pattern.patterns]

# A line the script itself printed, as relayed by the script runner:
# 'Metadata key("startup-script"), command("/bin/bash"): <line>'.
_SCRIPT_OUTPUT_RE = re.compile(
    r'Metadata key\("(startup-script(?:-url)?)"\), command\("[^"]*"\): (.*?)\r*$',
    re.MULTILINE)

# Last lines of each failing script's own output shown in the plan.
SCRIPT_OUTPUT_LINES = 5


def disabled_key(key: str) -> str:
    """Metadata key that holds the saved copy of a disabled startup script."""
    return f'{DISABLED_KEY_PREFIX}{key}'


def failing_startup_script_keys(boot_errors: List[Dict[str, Any]]) -> List[str]:
    """Linux startup-script keys named by startup_script findings, in order.

    Windows and sysprep keys are not returned: this repair is Linux-only.
    """
    keys: List[str] = []
    for err in boot_errors:
        if err.get('category') != 'startup_script':
            continue
        for key in _FAILED_KEY_RE.findall(err.get('detected_pattern', '')):
            if key not in keys:
                keys.append(key)
    return keys


def failing_keys_in_latest_boot(serial_output: str) -> List[str]:
    """Linux startup-script keys with a failure line in the latest boot.

    In the order they failed. Without a kernel banner the whole buffer counts.
    """
    tail = serial_output[latest_boot_start(serial_output):]
    found = sorted((match.start(), key)
                   for regex in _FAILURE_RES
                   for match in regex.finditer(tail)
                   for key in _FAILED_KEY_RE.findall(match.group(0)))
    keys: List[str] = []
    for _, key in found:
        if key not in keys:
            keys.append(key)
    return keys


def script_output_in_latest_boot(serial_output: str, keys: List[str],
                                 limit: int = SCRIPT_OUTPUT_LINES
                                 ) -> Dict[str, List[str]]:
    """The last `limit` lines each key's script printed in the latest boot."""
    tail = serial_output[latest_boot_start(serial_output):]
    output: Dict[str, List[str]] = {}
    for match in _SCRIPT_OUTPUT_RE.finditer(tail):
        key, line = match.group(1), match.group(2).strip()
        if key in keys and line:
            output.setdefault(key, []).append(line)
    return {key: lines[-limit:] for key, lines in output.items()}


# created-by value a MIG sets on its VMs:
# 'projects/<number>/(zones|regions)/<location>/instanceGroupManagers/<name>'.
_MIG_RE = re.compile(r'/(zones|regions)/([^/]+)/instanceGroupManagers/([^/]+)$')


@dataclass(frozen=True)
class ManagedGroup:
    """The managed instance group (zonal or regional) that created a VM."""
    name: str
    location_flag: str   # --zone=<zone> or --region=<region>


def managing_instance_group(items: List[Dict[str, str]]) -> Optional[ManagedGroup]:
    """The managed instance group that created the VM, if any."""
    for item in items:
        if item.get('key') != 'created-by':
            continue
        match = _MIG_RE.search(item.get('value', ''))
        if match:
            scope, location, name = match.groups()
            flag = '--zone' if scope == 'zones' else '--region'
            return ManagedGroup(name, f'{flag}={location}')
    return None


@dataclass
class DisablePlan:
    """What disabling the failing keys would do to the instance metadata."""
    disable: List[str] = field(default_factory=list)           # set now; save and remove
    already_disabled: List[str] = field(default_factory=list)  # removed, saved copy exists
    not_on_instance: List[str] = field(default_factory=list)   # no key, no saved copy
    conflicts: List[str] = field(default_factory=list)         # set now AND saved copy exists


def plan_disable(items: List[Dict[str, str]], keys: List[str]) -> DisablePlan:
    """Classify each failing key against the current metadata items."""
    present = {item['key'] for item in items}
    plan = DisablePlan()
    for key in keys:
        saved = disabled_key(key) in present
        if key in present:
            (plan.conflicts if saved else plan.disable).append(key)
        else:
            (plan.already_disabled if saved else plan.not_on_instance).append(key)
    return plan


def disable_keys(items: List[Dict[str, str]], keys: List[str]) -> List[Dict[str, str]]:
    """Metadata items with each key moved to its disabled_key(), order kept."""
    result: List[Dict[str, str]] = []
    saved: List[Dict[str, str]] = []
    for item in items:
        if item['key'] in keys:
            saved.append({'key': disabled_key(item['key']), 'value': item['value']})
        else:
            result.append(item)
    return result + saved


def restore_instructions(keys: List[str], vm_name: str, zone: str,
                         project: str) -> List[str]:
    """Commands that put each disabled script back once it is fixed."""
    target = f"{vm_name} --zone={zone} --project={project}"
    lines: List[str] = []
    for key in keys:
        saved = disabled_key(key)
        show = (f"gcloud compute instances describe {target} "
                f"--format=\"value(metadata.items.extract({saved}))\"")
        lines.append(f"To put {key} back after fixing it:")
        if key == 'startup-script-url':
            lines.append(f"  $ {show}   # shows the URL")
            lines.append(f"  $ gcloud compute instances add-metadata {target} "
                         f"--metadata={key}=FIXED_URL")
        else:
            lines.append(f"  $ {show} > {key}.sh")
            lines.append(f"  (fix {key}.sh, then)")
            lines.append(f"  $ gcloud compute instances add-metadata {target} "
                         f"--metadata-from-file={key}={key}.sh")
        lines.append(f"  $ gcloud compute instances remove-metadata {target} "
                     f"--keys={saved}")
    lines.append("If Terraform or another tool manages this VM's metadata, fix "
                 "the script there too, or its next apply restores the failing "
                 "script.")
    return lines


def mig_guidance(mig: 'ManagedGroup', keys: List[str], project: str) -> List[str]:
    """Why a repair on a MIG member does not last, and what to do instead."""
    return [
        f"This VM belongs to managed instance group {mig.name}. Its instance "
        f"template still has {', '.join(keys)}: when the group recreates the "
        "VM (autohealing, a rolling update, a resize), the failing script "
        "comes back. Fix the script in a new instance template and roll it "
        "out:",
        f"  $ gcloud compute instance-groups managed rolling-action "
        f"start-update {mig.name} --version=template=FIXED_TEMPLATE "
        f"{mig.location_flag} --project={project}",
    ]


@dataclass
class RepairPlan:
    """What a run will change, read from the instance and its serial output.

    Built by StartupScriptRepair.plan(); the CLI shows it before asking, and
    run() builds a fresh one so it acts on current metadata.
    """
    keys: List[str]                       # failing keys, in the order they failed
    disable: DisablePlan
    items: List[Dict[str, str]]           # current instance metadata items
    fingerprint: Optional[str]            # of those items
    serial_offset: int                    # where output written from now on starts
    script_output: Dict[str, List[str]]   # last lines each failing script printed
    mig: Optional[ManagedGroup] = None    # managing instance group, if any
    has_local_ssd: bool = False
    has_shutdown_script: bool = False

    @property
    def disabled(self) -> List[str]:
        """Keys the VM boots without after the run."""
        return self.disable.disable + self.disable.already_disabled


class StartupScriptRepair:
    """Disable a failing startup script, reset the VM and verify the boot.

    Args:
        compute: Compute API client.
        project, zone, vm_name: The instance to repair.
        logger: Optional logger.
        client_for: Returns the client for a read call, given a step name
            (lets the caller tag calls for usage tracking). Defaults to
            `compute`.
        ua: Returns the tracking label for a mutating operation, given a step
            name. Defaults to no label.
        sleep: Injected for tests.
    """

    def __init__(self, compute, project: str, zone: str, vm_name: str,
                 logger=None,
                 client_for: Optional[Callable[[str], Any]] = None,
                 ua: Optional[Callable[[str], Optional[str]]] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name
        self.logger = logger
        self._client_for = client_for or (lambda step: compute)
        self._ua = ua or (lambda step: None)
        self._sleep = sleep

    def _log_debug(self, message: str) -> None:
        if self.logger:
            self.logger.debug(message)

    def plan(self, boot_errors: List[Dict[str, Any]]
             ) -> Tuple[Optional[RepairPlan], Optional[Dict[str, Any]]]:
        """Work out what a run would change, without changing anything.

        Returns:
            (plan, None) when the repair can proceed, or (None, result) with a
            'not_applied' / 'error' result (see run()) when it cannot.
        """
        start = time.time()
        try:
            vm = self._client_for('startup-script-vm-info').instances().get(
                project=self.project, zone=self.zone, instance=self.vm_name
            ).execute()
            serial = self._client_for('startup-script-serial').instances() \
                .getSerialPortOutput(project=self.project, zone=self.zone,
                                     instance=self.vm_name, start=0).execute()
        except Exception as e:
            return None, self._result(
                'error', start,
                error=(f"Could not read instance [{self.vm_name}] or its serial "
                       f"console: {extract_error_message(e)}. Nothing was changed."))

        contents = serial.get('contents', '')
        keys = failing_keys_in_latest_boot(contents)
        keys += [k for k in failing_startup_script_keys(boot_errors) if k not in keys]
        if not keys:
            return None, self._result(
                'not_applied', start,
                error=("The startup-script failure does not name startup-script "
                       "or startup-script-url, so there is nothing to disable."))

        metadata = vm.get('metadata', {})
        items = metadata.get('items', [])
        disable = plan_disable(items, keys)
        self._log_debug(f"startup_script repair: keys={keys} plan={disable}")
        if disable.conflicts:
            return None, self._result(
                'not_applied', start,
                error=(f"{', '.join(disable.conflicts)} is failing, but an earlier "
                       f"repair already saved a disabled copy as "
                       f"{', '.join(disabled_key(k) for k in disable.conflicts)}. "
                       "Nothing was changed, so that copy is not overwritten."),
                guidance=self._conflict_guidance(disable.conflicts))
        if not disable.disable and not disable.already_disabled:
            return None, self._result(
                'not_applied', start,
                error=(f"{', '.join(disable.not_on_instance)} is not set on the "
                       "instance; it is probably inherited from project "
                       "metadata, which repair does not change (it would affect "
                       "every VM in the project). Nothing was changed."),
                guidance=[
                    "Check the project-wide value:",
                    f"  $ gcloud compute project-info describe --project={self.project} "
                    f"--flatten='commonInstanceMetadata.items[]' "
                    f"--format='table(commonInstanceMetadata.items.key)'",
                ])

        present = {item['key'] for item in items}
        return RepairPlan(
            keys=keys, disable=disable, items=items,
            fingerprint=metadata.get('fingerprint'),
            serial_offset=int(serial.get('next', 0)),
            script_output=script_output_in_latest_boot(contents, keys),
            mig=managing_instance_group(items),
            has_local_ssd=any(d.get('type') == 'SCRATCH'
                              for d in vm.get('disks', [])),
            has_shutdown_script=bool(
                present & {'shutdown-script', 'shutdown-script-url'}),
        ), None

    def run(self, boot_errors: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Run the repair for the given diagnosis findings.

        Returns:
            Dict with: status ('success', 'not_applied' or 'error'),
            fixed_count, fix_lines, error, guidance (lines to show when
            nothing was changed), next_steps (restore commands, and how to
            fix a managing instance group's template), boot_verified,
            boot_errors_after, snapshot_name (always None), duration_seconds.
        """
        start = time.time()
        plan, refusal = self.plan(boot_errors)
        if refusal:
            return refusal

        if plan.disable.disable:
            result = SetMetadataOperation(
                self.compute, self.project, self.zone, self.logger
            ).execute(
                self.vm_name, disable_keys(plan.items, plan.disable.disable),
                preserve_existing=False,
                tracking_label=self._ua('meta-disable-startup-script'),
                expected_fingerprint=plan.fingerprint,
            )
            if not result.success:
                return self._result(
                    'error', start,
                    error=(f"Could not update the instance metadata: {result.error}. "
                           "Nothing was changed. If the metadata was modified at "
                           "the same time, run repair again."))

        fix_lines = [
            f"[FIXED] startup_script: disabled {key} (saved as {disabled_key(key)})"
            if key in plan.disable.disable else
            f"[FIXED] startup_script: {key} was already disabled by an earlier "
            f"repair (saved as {disabled_key(key)})"
            for key in plan.disabled
        ]
        next_steps = restore_instructions(plan.disabled, self.vm_name, self.zone,
                                          self.project)
        if plan.mig:
            next_steps = (mig_guidance(plan.mig, plan.disabled, self.project)
                          + [""] + next_steps)

        reset = ResetVMOperation(
            self.compute, self.project, self.zone, self.logger
        ).execute(self.vm_name, tracking_label=self._ua('vm-reset'))
        if not reset.success:
            return self._result(
                'error', start, fix_lines=fix_lines, next_steps=next_steps,
                error=(f"{reset.error}. The startup script is already disabled; "
                       "reset or restart the VM to boot without it."))

        verified, errors_after = self._verify_boot(plan.serial_offset)
        return self._result(
            'success', start, fix_lines=fix_lines, next_steps=next_steps,
            fixed_count=len(plan.disabled), boot_verified=verified,
            boot_errors_after=errors_after)

    def _conflict_guidance(self, keys: List[str]) -> List[str]:
        target = f"{self.vm_name} --zone={self.zone} --project={self.project}"
        lines = ["Compare the saved copy with the failing script, keep what you",
                 "need, then remove the saved copy and run repair again:"]
        for key in keys:
            lines.append(f"  $ gcloud compute instances remove-metadata {target} "
                         f"--keys={disabled_key(key)}")
        return lines

    def _serial_since(self, offset: int) -> str:
        response = self._client_for('boot-verify').instances() \
            .getSerialPortOutput(project=self.project, zone=self.zone,
                                 instance=self.vm_name, start=offset).execute()
        return response.get('contents', '')

    def _verify_boot(self, offset: int) -> Tuple[Optional[bool], List[str]]:
        """Check the boot that followed the reset.

        Only serial output written after `offset` is read, so the failure line
        from the boot before the reset cannot be mistaken for a new one. The
        wait ends once the boot completed and the script runner finished, or
        once the new boot is stuck (emergency mode, kernel panic), e.g. on an
        fstab entry the disabled script had written. If the runner never
        reports, the completed boot is analyzed as it is.

        Returns:
            (True, []) when the new boot completed with no findings;
            (False, findings) when it shows findings, completed or not;
            (None, [reason]) when it could not be confirmed in time.
        """
        output = ''
        waited = 0
        while waited < BOOT_WAIT_SECONDS:
            sys.stdout.write(f"\rVerifying boot (up to {BOOT_WAIT_SECONDS - waited}s)...")
            sys.stdout.flush()
            self._sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            try:
                output = self._serial_since(offset)
            except Exception as e:  # serial reads are best-effort
                self._log_debug(f"Serial read failed during boot check: {e}")
                continue
            if latest_boot_completed(output) and _SCRIPTS_DONE_RE.search(output):
                break
            if _BOOT_STUCK_RE.search(output):
                break
        sys.stdout.write("\r" + " " * 60 + "\r")
        sys.stdout.flush()

        diagnosis = self._analyze(output)
        if diagnosis.diagnosis_status == 'boot_errors_detected':
            return False, [f"{e.category}: {e.description}"
                           for e in diagnosis.boot_errors]
        if not latest_boot_completed(output):
            return None, [f"the boot after the reset did not finish within "
                          f"{BOOT_WAIT_SECONDS}s"]
        if diagnosis.diagnosis_status == 'healthy':
            return True, []
        return None, ["the serial output after the reset could not be analyzed"]

    def _analyze(self, output: str):
        return analyze_serial_output(output, self.vm_name, self.zone,
                                     'RUNNING', 'linux')

    @staticmethod
    def _result(status: str, start: float, *, error: Optional[str] = None,
                fix_lines: Optional[List[str]] = None,
                next_steps: Optional[List[str]] = None,
                guidance: Optional[List[str]] = None, fixed_count: int = 0,
                boot_verified: Optional[bool] = None,
                boot_errors_after: Optional[List[str]] = None) -> Dict[str, Any]:
        return {
            'status': status,
            'fixed_count': fixed_count,
            'fix_lines': fix_lines or [],
            'error': error,
            'guidance': guidance or [],
            'next_steps': next_steps or [],
            'boot_verified': boot_verified,
            'boot_errors_after': boot_errors_after or [],
            'snapshot_name': None,
            'duration_seconds': time.time() - start,
        }
