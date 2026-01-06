"""
Delete Disk operation.

Permanently deletes a Compute Engine persistent disk. This operation is
irreversible and should only be used after completing the rescue workflow.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, DISK_DELETE_FAILED


class DeleteDiskOperation(BaseOperation):
    """Operation that permanently deletes a disk.

    This operation cannot be rolled back. Use only when it is safe to remove
    the resource and there is no need to restore it.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Delete Disk"

    def execute(self, disk_name: str) -> OperationResult:
        """
        Permanently delete a persistent disk.

        Args:
            disk_name (str): Name of the disk to delete.

        Returns:
            OperationResult: Result with success status. `rollback_data` is
            always None because deletion is irreversible.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name}: {disk_name}")
        self._log_debug("WARNING: This operation cannot be rolled back!")

        try:
            # Delete the disk
            operation = self.compute.disks().delete(
                project=self.project,
                zone=self.zone,
                disk=disk_name
            ).execute()

            # Wait for operation to complete
            if not self._wait_for_operation(operation):
                error_detail = DISK_DELETE_FAILED.format(
                    vm_name=None,
                    zone=self.zone,
                    project=self.project,
                    disk_name=disk_name
                )
                self._log_error(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message="Timeout waiting for disk delete operation",
                    error=error_detail
                )

            self._log_debug(f"Disk {disk_name} deleted")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk deleted",
                rollback_data=None  # Cannot rollback deletion!
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='delete_disk')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=None,
                    zone=self.zone,
                    project=self.project,
                    disk_name=disk_name
                )
            else:
                error_detail = f"Failed to delete disk: {error_msg}"
            self._log_error(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to delete disk: {error_msg}",
                error=error_detail
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback is not possible for a deleted disk.

        Args:
            rollback_data (dict): Unused.

        Returns:
            bool: Always False to indicate deletion cannot be undone.

        Raises:
            None
        """

        self._log_error("Cannot rollback disk deletion - disk is gone!")
        self._log_error("This is a permanent operation")
        return False
