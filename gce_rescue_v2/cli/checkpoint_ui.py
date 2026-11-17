"""Checkpoint recovery UI for interrupted operations."""

import sys
from typing import List, Optional, TextIO
from ..orchestration.checkpoint import CheckpointManager, CheckpointData
from ..orchestration.ownership import (
    OwnershipChecker, attach_disk_command, delete_disk_command,
    delete_snapshot_command,
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


def _vet_checkpoint_rollback(compute, project: str, zone: str, vm_name: str,
                             checkpoint: CheckpointData, state_tracker,
                             logger=None) -> List[str]:
    """
    Drop rollback steps that would touch a resource not shown to be this VM's.

    Rollback replays steps recorded in the checkpoint, which is VM metadata
    that people other than the operator can write. Steps that delete a disk
    or snapshot, or attach a disk to the VM, only run when the resource is
    shown to belong to the rescue of this VM (see orchestration.ownership).
    The others are removed from `state_tracker`, and the command to do the
    step by hand is returned so the user can run it after checking.

    Args:
        compute: GCP compute client
        project: GCP project ID
        zone: GCP zone
        vm_name: VM name
        checkpoint: Checkpoint data
        state_tracker: StateTracker built from the checkpoint (changed in place)
        logger: Optional logger

    Returns:
        Commands for the steps that were skipped, in rollback order.
    """
    checker = OwnershipChecker(compute, project, zone, vm_name, logger)
    context = checkpoint.context or {}
    rescue_disk = context.get('rescue_disk_name')
    snapshot = context.get('snapshot_name')
    for op in state_tracker.operations:
        if op.operation_name == "Create Snapshot" and op.rollback_data:
            snapshot = snapshot or op.rollback_data.get('snapshot_name')

    def _disk_from(data):
        info = data.get('disk_info') or {}
        return (info.get('source') or '').split('/')[-1] or data.get('disk_name')

    skipped = []
    kept = []
    for op in reversed(state_tracker.operations):
        data = op.rollback_data or {}
        name = op.operation_name
        command = None
        if name == "Create Rescue Disk":
            disk = data.get('disk_name')
            if disk and not checker.is_rescue_disk(disk):
                command = delete_disk_command(disk, zone, project)
        elif name == "Create Snapshot":
            snap = data.get('snapshot_name')
            if (data.get('created_by_operation') and snap
                    and not checker.is_own_snapshot(snap)):
                command = delete_snapshot_command(snap, project)
        elif name in ("Detach Boot Disk", "Detach Original Disk"):
            disk = _disk_from(data)
            if disk and not checker.is_original_disk(
                    disk, rescue_disk=rescue_disk, snapshot=snapshot,
                    search_snapshots=True):
                command = attach_disk_command(
                    vm_name, zone, project, disk,
                    boot=(name == "Detach Boot Disk"))
        elif name == "Detach Rescue Disk":
            disk = _disk_from(data)
            if disk and not checker.is_rescue_disk(disk):
                command = attach_disk_command(vm_name, zone, project, disk,
                                              boot=True)
        if command:
            skipped.append(command)
        else:
            kept.append(op)
    state_tracker.operations = list(reversed(kept))
    return skipped


def _print_skipped_rollback(skipped: List[str], vm_name: str) -> None:
    """List the rollback steps that were skipped, with their commands."""
    if not skipped:
        return
    print(f"{warning_prefix()} Skipped {len(skipped)} rollback step"
          f"{'s' if len(skipped) != 1 else ''}: gce-rescue could not confirm"
          f" the disk or snapshot belongs to instance [{vm_name}].",
          file=sys.stderr)
    print("  Check it, and if it does, run:", file=sys.stderr)
    for command in skipped:
        print(f"  $ {command}", file=sys.stderr)


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

    skipped = _vet_checkpoint_rollback(compute, project, zone, vm_name,
                                       checkpoint, state_tracker, logger)

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        if skipped:
            _print_skipped_rollback(skipped, vm_name)
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

    _print_skipped_rollback(skipped, vm_name)
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

    skipped = _vet_checkpoint_rollback(compute, project, zone, vm_name,
                                       checkpoint, state_tracker, logger)

    # Check if there's anything to rollback
    rollback_ops = state_tracker.get_rollback_operations()
    if not rollback_ops:
        # Nothing was changed -- just clear the stale checkpoint
        checkpoint_mgr = CheckpointManager(compute, project, zone, vm_name, logger)
        checkpoint_mgr.clear_checkpoint()
        if skipped:
            _print_skipped_rollback(skipped, vm_name)
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

    _print_skipped_rollback(skipped, vm_name)
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
