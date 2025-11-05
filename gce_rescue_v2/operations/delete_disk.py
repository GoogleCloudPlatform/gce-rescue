"""
GCE Rescue - Delete Disk Operation

Deletes a disk.
Rollback: Cannot undo deletion (disk is gone).
"""

import time
from operations.base import BaseOperation, OperationResult


class DeleteDiskOperation(BaseOperation):
    """
    Deletes a disk.

    WARNING: Rollback is NOT possible - disk is permanently deleted!
    Only use this operation at the very end, after confirming success.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Delete Disk"

    def execute(self, disk_name: str) -> OperationResult:
        """
        Delete a disk.

        WARNING: This operation cannot be rolled back!

        Args:
            disk_name: Name of the disk to delete

        Returns:
            OperationResult with success status (no rollback data - can't undo!)
        """

        self._log_debug(f"Executing {self.name}: {disk_name}")
        self._log_debug("WARNING: This operation cannot be rolled back!")

        try:
            # Delete the disk
            self.compute.disks().delete(
                project=self.project,
                zone=self.zone,
                disk=disk_name
            ).execute()

            # Wait a bit for deletion
            time.sleep(2)

            self._log_debug(f"Disk {disk_name} deleted")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk deleted",
                rollback_data=None  # Cannot rollback deletion!
            )

        except Exception as e:
            self._log_error(f"Failed to delete disk: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to delete disk",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: NOT POSSIBLE - disk is already deleted.

        This is a permanent operation. Do not use DeleteDiskOperation
        in a sequence that might need rollback. Only use it after
        confirming all operations succeeded.

        Args:
            rollback_data: Unused (None)

        Returns:
            False (cannot rollback deletion)
        """

        self._log_error("Cannot rollback disk deletion - disk is gone!")
        self._log_error("This is a permanent operation")
        return False
