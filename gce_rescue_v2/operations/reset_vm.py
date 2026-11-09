"""
Reset VM operation.

Resets a running Compute Engine VM (the equivalent of pressing its reset
button) and waits for the reset operation to finish. The VM stays on the
same host and keeps its disks, including Local SSD data. A reset cannot be
undone, so rollback is a no-op.
"""

from .base import BaseOperation, OperationResult, extract_error_message


class ResetVMOperation(BaseOperation):
    """Operation that resets a running VM instance."""

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Reset VM"

    def execute(self, vm_name: str, timeout: int = 300,
                tracking_label: str = None) -> OperationResult:
        """
        Reset the specified VM and wait for the reset operation to finish.

        Args:
            vm_name (str): Name of the VM to reset. It must be RUNNING.
            timeout (int): Maximum seconds to wait for the operation.
            tracking_label (str): Tracking label for usage analytics.

        Returns:
            OperationResult: success when the reset operation completed.
        """
        self._log_debug(f"Executing {self.name} for {vm_name}")
        try:
            compute = (self._create_tracked_client(tracking_label)
                       if tracking_label else self.compute)
            operation = compute.instances().reset(
                project=self.project, zone=self.zone, instance=vm_name
            ).execute()
            if not self._wait_for_operation(operation, timeout=timeout):
                return OperationResult(
                    operation_name=self.name, success=False,
                    message="Reset did not complete",
                    error=f"Timed out after {timeout}s waiting for the reset "
                          f"of instance [{vm_name}]",
                )
            return OperationResult(
                operation_name=self.name, success=True, message="VM reset",
            )
        except Exception as e:
            error_msg = extract_error_message(e)
            self._log_debug(f"Reset failed: {error_msg}")
            return OperationResult(
                operation_name=self.name, success=False,
                message=f"Failed to reset VM: {error_msg}",
                error=f"Failed to reset instance [{vm_name}]: {error_msg}",
            )

    def rollback(self, rollback_data: dict, tracking_label: str = None) -> bool:
        """A reset cannot be undone; nothing to roll back."""
        return True
