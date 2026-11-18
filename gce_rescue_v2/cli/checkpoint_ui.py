"""Checkpoint recovery UI for interrupted operations."""

import sys
from typing import List, Optional, TextIO
from ..orchestration.checkpoint import CheckpointManager, CheckpointData
from ..orchestration.ownership import (
    ConfirmDisk, OriginalDiskProof, OwnershipChecker, attach_disk_command,
    auto_delete_command, delete_snapshot_command, describe_disk_command,
    detach_disk_command, zonal_disk_name, zonal_disk_source,
)
from ..utils.colors import error_prefix, warning_prefix, clear_lines
from ..utils.safe_text import safe_text
from .output import _Spinner
from .preflight import _create_tracked_client


def _warn_ignored_checkpoint(checkpoint_mgr: CheckpointManager, vm_name: str,
                             file: Optional[TextIO] = None) -> None:
    """Tell the user a checkpoint was found in metadata but not used.

    Args:
        checkpoint_mgr: Manager that just loaded the checkpoint.
        vm_name: VM name
        file: Stream to write to (stderr by default)
    """
    reason = getattr(checkpoint_mgr, "invalid_reason", None)
    if not reason:
        return
    file = file or sys.stderr
    print(f"{warning_prefix()} Ignoring the gce-rescue checkpoint on instance"
          f" [{vm_name}]: {safe_text(reason)}.",
          file=file)
    print("  It was changed after gce-rescue wrote it, or was not written by"
          " gce-rescue.", file=file)
    print("  If an earlier run was interrupted, check the instance's disks"
          " before you continue.", file=file)


def _prompt_incomplete_operation(checkpoint: CheckpointData, operation_type: str) -> str:
    """
    Prompt user about incomplete operation (interactive mode only).

    Args:
        checkpoint: Checkpoint data from incomplete operation
        operation_type: 'rescue' or 'restore'

    Returns:
        'continue', 'rollback', or 'abort'
    """
    # Get next step name for continue option
    next_step = checkpoint.current_step + 1
    step_names = {
        1: "Stop VM", 2: "Detach Boot Disk", 3: "Create Snapshot",
        4: "Create Rescue Disk", 5: "Attach Rescue Disk", 6: "Set Metadata",
        7: "Start VM", 8: "Attach Original Disk", 9: "Verify Startup"
    }
    next_step_name = step_names.get(next_step, f"Step {next_step}")
    # Checkpoint fields come from VM metadata: escape before printing.
    last_step_name = safe_text(
        checkpoint.get_last_completed_operation() or "None", max_len=64)
    started = safe_text(checkpoint.started_at[:19].replace('T', ' '))

    # Track lines for clearing after continue
    lines_printed = 0

    print(f"\n{warning_prefix()} An incomplete {operation_type} operation was detected"
          f" for this instance.")
    lines_printed += 2  # includes leading newline
    print("")
    lines_printed += 1
    print(f"  Started:    {started} ({checkpoint.get_age_display()})")
    lines_printed += 1
    print(f"  Progress:   {safe_text(checkpoint.current_step)} of"
          f" {safe_text(checkpoint.total_steps)} steps completed")
    lines_printed += 1
    print(f"  Last step:  {last_step_name}")
    lines_printed += 1
    print("")
    lines_printed += 1
    print("What would you like to do?")
    lines_printed += 1
    print(f"  [1] Continue  Resume from \"{next_step_name}\"")
    lines_printed += 1
    print("  [2] Rollback  Undo completed steps and restore original state")
    lines_printed += 1
    print("  [3] Abort     Do nothing and exit")
    lines_printed += 1
    print("")
    lines_printed += 1

    while True:
        try:
            response = input("Enter your choice (1/2/3): ").strip()
            lines_printed += 1  # input line
            if response == '1':
                # Clear the warning message when continuing
                clear_lines(lines_printed)
                return 'continue'
            elif response == '2':
                return 'rollback'
            elif response == '3':
                return 'abort'
            else:
                print("Please enter 1, 2, or 3.")
                lines_printed += 1
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            return 'abort'


def _print_quiet_checkpoint_block(checkpoint: CheckpointData, command: str,
                                  vm_name: str, zone: str, project: str,
                                  file: Optional[TextIO] = None) -> None:
    """
    Explain an incomplete operation found in --quiet mode.

    Unattended runs must never resume or roll back on their own, because
    either choice changes disks and metadata the user has not reviewed.
    Instead this prints what was found and the commands that let the user
    choose interactively.

    Args:
        checkpoint: Checkpoint data from the incomplete operation
        command: The command that found it ('repair', 'rescue', ...)
        vm_name: VM name
        zone: GCP zone
        project: GCP project ID
        file: Stream to write to (stderr by default)
    """
    file = file or sys.stderr
    last_step = safe_text(
        checkpoint.get_last_completed_operation() or "None", max_len=64)
    started = safe_text(checkpoint.started_at[:19].replace('T', ' '))
    flags = f"--zone={zone} --project={project}"

    print(f"{error_prefix()} An incomplete {checkpoint.operation} operation was "
          f"detected for instance [{vm_name}].", file=file)
    print("", file=file)
    print(f"  Started:    {started} ({checkpoint.get_age_display()})", file=file)
    print(f"  Progress:   {safe_text(checkpoint.current_step)} of"
          f" {safe_text(checkpoint.total_steps)} steps completed", file=file)
    print(f"  Last step:  {last_step}", file=file)
    print("", file=file)
    print(f"{command} --quiet does not resume or roll back an interrupted "
          f"operation automatically. Nothing was changed.", file=file)
    print("", file=file)
    print("To roll back the completed steps and retry, run without --quiet "
          "and choose Rollback:", file=file)
    print(f"  $ gce-rescue {command} {vm_name} {flags}", file=file)
    print("", file=file)
    # Resuming is offered by the command that owns the checkpoint (rescue
    # for repair's own rescue phase).
    print(f"To resume the interrupted {checkpoint.operation} instead, run "
          f"without --quiet and choose Continue:", file=file)
    print(f"  $ gce-rescue {checkpoint.operation} {vm_name} {flags}", file=file)


def _confirm_disk(question: str, details: List[str], name: str) -> bool:
    """Ask the operator about a disk gce-rescue could not confirm.

    The operator has to type the disk name: a quick "y" is too easy to
    give to a prompt that a forged checkpoint can bring up.
    """
    print(f"\n{warning_prefix()} {question}", file=sys.stderr)
    for line in details:
        print(f"  {line}", file=sys.stderr)
    try:
        answer = input("Type the disk name to use it, or press Enter to skip: ").strip()
    except EOFError:
        answer = ''
    return answer == name


# Rollback steps whose undo detaches a disk from the VM or deletes the
# rescue disk. They are dropped when the boot disk cannot be put back, so
# the VM is never left without a boot disk.
_UNDO_REMOVES_DISK = ("Attach Rescue Disk", "Attach Original Disk",
                      "Create Rescue Disk")


def _vet_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                             checkpoint: CheckpointData, state_tracker,
                             logger=None,
                             confirm: Optional[ConfirmDisk] = None,
                             notes: Optional[List[str]] = None) -> List[str]:
    """
    Drop rollback steps that would touch a resource not shown to be this VM's.

    Rollback replays steps recorded in the checkpoint, which is VM metadata
    that people other than the operator can write. Every step that deletes,
    attaches or detaches a disk is checked against what Compute Engine
    reports (see orchestration.ownership), and the attach settings are
    rebuilt from trusted values:

    - A rescue disk is deleted only if gce-rescue made it for this VM and
      it is attached now or was never attached.
    - Safety snapshots are never deleted by a rollback.
    - A disk is detached only if it is this VM's rescue disk, or shown to
      be its original boot disk.
    - A disk is re-attached only if it is this VM's rescue disk, or shown
      to be its original boot disk. When that cannot be shown (a rescue
      with --no-snapshot, or one made by an older version), `confirm`
      asks the operator.
    - Re-attached disks are zonal disks of this project and zone, in
      read-write mode; auto-delete is turned on only when the ownership
      marker recorded it.
    - If the boot disk cannot be put back, nothing is detached or deleted,
      so the VM keeps the boot disk it has now.

    Steps that fail these checks are removed from `state_tracker`, and the
    command to do the step by hand is returned so the user can run it
    after checking.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        state_tracker: StateTracker built from the checkpoint (changed in place)
        logger: Optional logger
        confirm: Asks the operator about a disk that could not be
            confirmed. None skips such steps.
        notes: If given, other messages for the user are appended (kept
            snapshots and disks, auto-delete to turn back on).

    Returns:
        Commands for the steps that were skipped, in rollback order.
    """
    checker = OwnershipChecker(compute, project, zone, vm_name, logger)
    notes = notes if notes is not None else []
    context = checkpoint.context or {}
    rescue_disk = context.get('rescue_disk_name')
    snapshot = context.get('snapshot_name')
    for op in state_tracker.operations:
        if op.operation_name == "Create Snapshot" and op.rollback_data:
            snapshot = snapshot or op.rollback_data.get('snapshot_name')

    def _original(disk: str, action: str):
        """Proof (or operator confirmation) that `disk` is the original."""
        proof = checker.original_disk_proof(
            disk, rescue_disk=rescue_disk, snapshot=snapshot,
            search_snapshots=True)
        if proof:
            return proof
        if checker.confirm_original_disk(disk, confirm, action,
                                         rescue_disk=rescue_disk):
            return OriginalDiskProof()
        return None

    skipped = []
    kept = []
    boot_not_restored = False
    for op in reversed(state_tracker.operations):
        data = dict(op.rollback_data or {})
        name = op.operation_name
        command = None
        if name == "Create Rescue Disk":
            disk = data.get('disk_name')
            if disk and not checker.is_disposable_rescue_disk(disk):
                notes.append(
                    f"Disk [{disk}] named in the checkpoint was not deleted:"
                    f" gce-rescue could not confirm it is a rescue disk made"
                    f" for instance [{vm_name}] in this run. If it is a"
                    f" leftover rescue disk, check it and delete it yourself:")
                notes.append("  $ " + describe_disk_command(disk, zone, project))
                continue
        elif name == "Create Snapshot":
            snap = data.get('snapshot_name')
            if data.get('created_by_operation') and snap:
                if checker.is_own_snapshot(snap):
                    notes.append(
                        f"Safety snapshot [{snap}] was kept. Delete it when"
                        f" you no longer need it:")
                    notes.append(f"  $ {delete_snapshot_command(snap, project)}")
                else:
                    notes.append(
                        f"Snapshot [{snap}] named in the checkpoint was not"
                        f" taken by gce-rescue for instance [{vm_name}]; it"
                        f" was left as it is.")
            continue
        elif name in ("Attach Rescue Disk", "Attach Original Disk"):
            device = data.get('device_name')
            disk = checker.device_disk(device)
            if not disk:
                # Nothing attached under that name: nothing to undo.
                continue
            if name == "Attach Rescue Disk":
                ok = checker.is_rescue_disk(disk)
            else:
                ok = _original(disk, f"Detach it from [{vm_name}]") is not None
            if not ok:
                command = detach_disk_command(vm_name, zone, project, device)
        elif name in ("Detach Boot Disk", "Detach Original Disk",
                      "Detach Rescue Disk"):
            info = data.get('disk_info')
            if not isinstance(info, dict):
                kept.append(op)
                continue
            disk = zonal_disk_name(info.get('source'), project, zone)
            # The disk this step puts back as the VM's boot disk.
            boot = name != "Detach Original Disk"
            auto_delete = False
            ok = False
            if disk and disk in checker.attached_disks() and not any(
                    k.operation_name in ("Attach Rescue Disk",
                                         "Attach Original Disk")
                    and checker.device_disk(
                        (k.rollback_data or {}).get('device_name')) == disk
                    for k in kept):
                # Still attached and nothing in this rollback detaches it
                # first: re-attaching would fail.
                notes.append(
                    f"Disk [{disk}] is still attached to instance [{vm_name}],"
                    f" so it was not re-attached"
                    f"{' as the boot disk' if boot else ''}.")
                boot_not_restored = boot_not_restored or boot
                continue
            if not disk:
                ok = False
            elif name == "Detach Rescue Disk":
                ok = checker.is_rescue_disk(disk)
            else:
                proof = _original(disk, f"Attach it to [{vm_name}]"
                                  f"{' as the boot disk' if boot else ''}")
                ok = proof is not None
                if proof and proof.auto_delete:
                    auto_delete = True
                elif ok and boot and info.get('autoDelete') is True:
                    notes.append(
                        f"Auto-delete was not turned back on for disk"
                        f" [{disk}]. If it was on before the rescue, run:")
                    notes.append("  $ " + auto_delete_command(
                        vm_name, zone, project, disk))
            if ok:
                clean = {
                    'source': zonal_disk_source(project, zone, disk),
                    'boot': boot,
                    'autoDelete': auto_delete,
                    'deviceName': info.get('deviceName') or disk,
                    'mode': 'READ_WRITE',
                }
                if info.get('interface') in ('SCSI', 'NVME'):
                    clean['interface'] = info['interface']
                data['disk_info'] = clean
                op.rollback_data = data
            else:
                command = attach_disk_command(
                    vm_name, zone, project, disk or '<disk>', boot=boot)
                boot_not_restored = boot_not_restored or boot
        if command:
            skipped.append(command)
        else:
            kept.append(op)

    if boot_not_restored:
        # Detaching the current boot disk, or deleting the rescue disk,
        # would leave the VM with no disk to boot from.
        dropped = [op for op in kept if op.operation_name in _UNDO_REMOVES_DISK]
        if dropped:
            kept = [op for op in kept if op not in dropped]
            notes.append(
                f"The boot disk was not put back, so no disk was detached"
                f" from or deleted for instance [{vm_name}]. After you attach"
                f" the right boot disk, detach and delete the rescue disk"
                f" yourself.")
    state_tracker.operations = list(reversed(kept))
    return skipped


def _print_skipped_rollback(skipped: List[str], vm_name: str,
                            notes: Optional[List[str]] = None) -> None:
    """List the rollback steps that were skipped, with their commands."""
    if skipped:
        print(f"{warning_prefix()} Skipped {len(skipped)} rollback step"
              f"{'s' if len(skipped) != 1 else ''}: gce-rescue could not"
              f" confirm the disk belongs to instance [{vm_name}].",
              file=sys.stderr)
        print("  Check it, and if it does, run:", file=sys.stderr)
        for command in skipped:
            print(f"  $ {command}", file=sys.stderr)
    for line in notes or []:
        print(line, file=sys.stderr)


def _handle_restore_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                                        checkpoint: CheckpointData, logger=None) -> bool:
    """
    Rollback restore operation to return to rescue mode.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        logger: Optional logger

    Returns:
        True if rollback succeeded
    """
    from ..orchestration.rollback import RollbackHandler
    from ..orchestration.state import StateTracker
    from ..operations import (
        StopVMOperation, DetachDiskOperation, AttachDiskOperation,
        SetMetadataOperation, StartVMOperation
    )

    # Create operations map for restore operations
    operations_map = {
        "Stop VM": StopVMOperation(compute, project, zone, logger),
        "Detach Rescue Disk": DetachDiskOperation(compute, project, zone, logger),
        "Detach Original Disk": DetachDiskOperation(compute, project, zone, logger),
        "Attach Original Disk": AttachDiskOperation(compute, project, zone, logger),
        "Set Metadata": SetMetadataOperation(compute, project, zone, logger),
        "Start VM": StartVMOperation(compute, project, zone, logger),
    }

    # Build state tracker from checkpoint
    state_tracker = StateTracker()
    for op in checkpoint.completed_operations:
        state_tracker.add_operation(
            operation_name=op.name,
            success=True,
            message="Completed in previous session",
            rollback_data=op.rollback_data,
            step_number=op.step
        )

    notes: List[str] = []
    skipped = _vet_checkpoint_rollback(compute, project, zone, vm_name,
                                       checkpoint, state_tracker, logger,
                                       confirm=_confirm_disk, notes=notes)

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        if skipped or notes:
            _print_skipped_rollback(skipped, vm_name, notes)
            print(f"Checkpoint cleared for instance [{vm_name}].")
        else:
            print(f"\nNo changes were made. Checkpoint cleared for instance [{vm_name}].")
        return not skipped

    # Perform rollback with progress indicator
    print(f"\nRolling back to rescue mode for instance [{vm_name}]:")
    spinner = _Spinner("  Undoing changes")
    spinner.start()
    handler = RollbackHandler(logger)
    success = handler.rollback(state_tracker, operations_map)
    spinner.stop()

    # Clear checkpoint after rollback
    checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
    checkpoint_mgr.clear_checkpoint()

    _print_skipped_rollback(skipped, vm_name, notes)
    if success and skipped:
        print(f"Rollback of instance [{vm_name}] finished except for"
              f" the skipped steps above.")
        success = False
    elif success:
        print(f"Instance [{vm_name}] is back in rescue mode.")
    else:
        print(f"{error_prefix()} Rollback completed with errors."
              f" Manual intervention may be required.", file=sys.stderr)

    return success


def _reconcile_rescue_state(compute, project: str, zone: str, vm_name: str,
                            checkpoint: CheckpointData, state_tracker,
                            logger=None) -> None:
    """
    Reconcile checkpoint state with actual VM disk state.

    Inspects the real VM via GCP API and adds any operations that completed
    server-side but weren't checkpointed (e.g., Ctrl+C during API call).

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data from metadata
        state_tracker: StateTracker to add missing operations to
        logger: Optional logger
    """
    def _log(msg):
        if logger:
            logger.debug(f"[Reconcile] {msg}")

    context = checkpoint.context or {}
    original_disk_name = context.get('original_disk_name')
    rescue_disk_name = context.get('rescue_disk_name')

    if not original_disk_name:
        _log("No original_disk_name in checkpoint context, skipping reconciliation")
        return

    # Get actual VM state
    try:
        tracked = _create_tracked_client(compute, 'rescue-reconcile')
        vm = tracked.instances().get(
            project=project, zone=zone, instance=vm_name
        ).execute()
    except Exception as e:
        _log(f"Could not fetch VM state: {e}")
        return

    attached_disks = vm.get('disks', [])
    attached_disk_names = []
    for d in attached_disks:
        source = d.get('source', '')
        if source:
            attached_disk_names.append(source.split('/')[-1])

    checkpointed_op_names = {op.name for op in checkpoint.completed_operations}

    # Check 1: Original boot disk detached but not in checkpoint
    if original_disk_name not in attached_disk_names:
        if "Detach Boot Disk" not in checkpointed_op_names:
            _log(f"Original boot disk '{original_disk_name}' is detached but not in"
                 f" checkpoint - adding to rollback")
            disk_source = f"projects/{project}/zones/{zone}/disks/{original_disk_name}"
            # Re-attach exactly as the disk was before rescue when the
            # checkpoint recorded it (#170); older checkpoints fall back.
            attachment = context.get('original_boot_attachment') or {}
            disk_info = {
                'source': disk_source,
                'boot': True,
                'autoDelete': attachment.get('autoDelete', True),
                'deviceName': (attachment.get('deviceName')
                               or context.get('original_device_name')
                               or original_disk_name),
                'mode': attachment.get('mode', 'READ_WRITE')
            }
            if attachment.get('interface'):
                disk_info['interface'] = attachment['interface']
            state_tracker.add_operation(
                operation_name="Detach Boot Disk",
                success=True,
                message="Detected via reconciliation (completed but not checkpointed)",
                rollback_data={
                    'vm_name': vm_name,
                    'disk_info': disk_info
                },
                step_number=2
            )

    # Check 2: Rescue disk exists and is attached but not in checkpoint
    if rescue_disk_name and rescue_disk_name in attached_disk_names:
        if "Attach Rescue Disk" not in checkpointed_op_names:
            _log(f"Rescue disk '{rescue_disk_name}' is attached but not in"
                 f" checkpoint - adding to rollback")
            # Need to add Create Rescue Disk first (so rollback deletes it)
            if "Create Rescue Disk" not in checkpointed_op_names:
                state_tracker.add_operation(
                    operation_name="Create Rescue Disk",
                    success=True,
                    message="Detected via reconciliation",
                    rollback_data={'disk_name': rescue_disk_name},
                    step_number=4
                )
            state_tracker.add_operation(
                operation_name="Attach Rescue Disk",
                success=True,
                message="Detected via reconciliation",
                rollback_data={
                    'vm_name': vm_name,
                    'device_name': rescue_disk_name
                },
                step_number=5
            )

    # Check 3: Rescue disk exists (not attached) but creation not in checkpoint
    if rescue_disk_name and "Create Rescue Disk" not in checkpointed_op_names:
        if rescue_disk_name not in attached_disk_names:
            try:
                tracked_disk = _create_tracked_client(compute, 'rescue-reconcile')
                tracked_disk.disks().get(
                    project=project, zone=zone, disk=rescue_disk_name
                ).execute()
                _log(f"Rescue disk '{rescue_disk_name}' exists but not in"
                     f" checkpoint - adding to rollback")
                state_tracker.add_operation(
                    operation_name="Create Rescue Disk",
                    success=True,
                    message="Detected via reconciliation",
                    rollback_data={'disk_name': rescue_disk_name},
                    step_number=4
                )
            except Exception:
                pass  # Disk doesn't exist, nothing to reconcile

    # Check 4: Rescue metadata set but not in checkpoint
    metadata_obj = vm.get('metadata', {})
    metadata_items = metadata_obj.get('items', [])
    has_rescue_metadata = any(item.get('key') == 'rescue-mode' for item in metadata_items)
    if has_rescue_metadata and "Set Metadata" not in checkpointed_op_names:
        _log("Rescue metadata found but not in checkpoint - adding to rollback")
        # Reconstruct pre-rescue metadata by removing rescue keys and restoring backups
        from ..operations.set_metadata import restore_pre_rescue_items
        restored_items = restore_pre_rescue_items(metadata_items)
        original_metadata = {
            'fingerprint': metadata_obj.get('fingerprint', ''),
            'items': restored_items
        }
        state_tracker.add_operation(
            operation_name="Set Metadata",
            success=True,
            message="Detected via reconciliation",
            rollback_data={
                'vm_name': vm_name,
                'original_metadata': original_metadata
            },
            step_number=6
        )


def _handle_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                                checkpoint: CheckpointData, logger=None) -> bool:
    """
    Rollback from checkpoint.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        logger: Optional logger

    Returns:
        True if rollback succeeded
    """
    from ..orchestration.rollback import RollbackHandler
    from ..orchestration.state import StateTracker, OperationState
    from ..operations import (
        StopVMOperation, DetachDiskOperation, AttachDiskOperation,
        SetMetadataOperation, StartVMOperation, DeleteDiskOperation,
        CreateSnapshotOperation, CreateDiskOperation
    )

    # Create operations map
    operations_map = {
        "Stop VM": StopVMOperation(compute, project, zone, logger),
        "Detach Boot Disk": DetachDiskOperation(compute, project, zone, logger),
        "Create Snapshot": CreateSnapshotOperation(compute, project, zone, logger),
        "Create Rescue Disk": CreateDiskOperation(compute, project, zone, logger),
        "Attach Rescue Disk": AttachDiskOperation(compute, project, zone, logger),
        "Set Metadata": SetMetadataOperation(compute, project, zone, logger),
        "Start VM": StartVMOperation(compute, project, zone, logger),
        "Attach Original Disk": AttachDiskOperation(compute, project, zone, logger),
    }

    # Build state tracker from checkpoint
    state_tracker = StateTracker()
    for op in checkpoint.completed_operations:
        state_tracker.add_operation(
            operation_name=op.name,
            success=True,
            message="Completed in previous session",
            rollback_data=op.rollback_data,
            step_number=op.step
        )

    # Reconcile with actual VM state to catch operations that completed
    # server-side but weren't checkpointed (e.g., Ctrl+C during API call)
    if checkpoint.operation == 'rescue':
        _reconcile_rescue_state(
            compute, project, zone, vm_name, checkpoint, state_tracker, logger
        )

    notes: List[str] = []
    skipped = _vet_checkpoint_rollback(compute, project, zone, vm_name,
                                       checkpoint, state_tracker, logger,
                                       confirm=_confirm_disk, notes=notes)

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        # Nothing was changed -- just clear the stale checkpoint
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        if skipped or notes:
            _print_skipped_rollback(skipped, vm_name, notes)
            print(f"Checkpoint cleared for instance [{vm_name}].")
        else:
            print(f"\nNo changes were made. Checkpoint cleared for instance [{vm_name}].")
        return not skipped

    # Perform rollback with progress indicator
    print(f"\nRolling back instance [{vm_name}]:")
    spinner = _Spinner("  Undoing changes")
    spinner.start()
    handler = RollbackHandler(logger)
    success = handler.rollback(state_tracker, operations_map)
    spinner.stop()

    # Clear checkpoint after rollback
    checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
    checkpoint_mgr.clear_checkpoint()

    _print_skipped_rollback(skipped, vm_name, notes)
    if success and skipped:
        print(f"Rollback of instance [{vm_name}] finished except for"
              f" the skipped steps above.")
        success = False
    elif success:
        print(f"Instance [{vm_name}] has been restored to its original state.")
    else:
        print(f"{error_prefix()} Rollback completed with errors."
              f" Manual intervention may be required.", file=sys.stderr)

    return success
