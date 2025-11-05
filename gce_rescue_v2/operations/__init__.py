"""
GCE Rescue - Operations Module

This module provides individual operations with rollback support.
Each operation does ONE thing and can undo itself.

Usage:
    from operations import (
        StopVMOperation,
        StartVMOperation,
        CreateDiskOperation,
        AttachDiskOperation,
        DetachDiskOperation,
        SetMetadataOperation,
        DeleteDiskOperation
    )

    # Execute an operation
    operation = StopVMOperation(compute, project, zone, logger)
    result = operation.execute(vm_name='my-instance')

    if result.success:
        print(f"[OK] {result.message}")
        # Save rollback data for later
        rollback_data = result.rollback_data
    else:
        print(f"[X] {result.message}")

    # Later, if we need to rollback
    if need_rollback:
        operation.rollback(rollback_data)
"""

from operations.base import BaseOperation, OperationResult
from operations.stop_vm import StopVMOperation
from operations.start_vm import StartVMOperation
from operations.create_disk import CreateDiskOperation
from operations.attach_disk import AttachDiskOperation
from operations.detach_disk import DetachDiskOperation
from operations.set_metadata import SetMetadataOperation
from operations.delete_disk import DeleteDiskOperation
from operations.create_snapshot import CreateSnapshotOperation

__all__ = [
    # Base classes
    'BaseOperation',
    'OperationResult',

    # Operations
    'StopVMOperation',
    'StartVMOperation',
    'CreateDiskOperation',
    'AttachDiskOperation',
    'DetachDiskOperation',
    'SetMetadataOperation',
    'DeleteDiskOperation',
    'CreateSnapshotOperation',
]
