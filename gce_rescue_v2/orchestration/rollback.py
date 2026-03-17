"""
GCE Rescue - Rollback Handler

Handles rollback of operations when something fails.
Undoes operations in reverse order with dependency checking.
"""

from typing import List, Set
from .state import StateTracker, OperationState
from ..operations.base import BaseOperation


# Define operation dependencies for safe rollback
# Key = operation that depends on successful rollback of Value
# If Value rollback fails, Key should be skipped
ROLLBACK_DEPENDENCIES = {
    # Don't start VM if boot disk re-attach failed
    "Stop VM": ["Detach Boot Disk"],
    # Don't attach original disk if VM is in broken state
    "Attach Original Disk": ["Stop VM"],
}

# Critical operations - if these fail during rollback, abort remaining rollbacks
CRITICAL_ROLLBACK_OPS = [
    "Detach Boot Disk",  # Without boot disk reattached, VM will be broken
]


class RollbackHandler:
    """
    Handles rollback of operations with dependency checking.

    When an operation fails, this undoes all previous successful operations
    in reverse order, returning the system to its original state.

    Dependency checking ensures we don't leave the VM in a broken state.
    For example: If re-attaching the boot disk fails, we skip starting the VM
    (because starting without a boot disk would fail anyway).

    Example:
        handler = RollbackHandler(logger)

        # Operation sequence:
        # 1. Stop VM [OK]
        # 2. Create Disk [OK]
        # 3. Attach Disk [X] FAILED

        # Rollback:
        handler.rollback(state_tracker, operations_map)
        # → Undo operation 2 (delete disk)
        # → Undo operation 1 (restart VM)
    """

    def __init__(self, logger=None):
        """
        Initialize rollback handler.

        Args:
            logger: Optional logger for output
        """
        self.logger = logger
        self.failed_rollbacks: Set[str] = set()  # Track which rollbacks failed

    def _log_info(self, message: str):
        """Log info message."""
        if self.logger:
            self.logger.info(message)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def _log_debug(self, message: str):
        """Log debug message with component prefix."""
        if self.logger:
            self.logger.debug(f"[Rollback] {message}", stacklevel=2)

    def _should_skip_rollback(self, op_name: str) -> bool:
        """
        Check if rollback should be skipped due to failed dependencies.

        Args:
            op_name: Name of the operation to check

        Returns:
            True if rollback should be skipped, False otherwise
        """
        dependencies = ROLLBACK_DEPENDENCIES.get(op_name, [])

        for dep in dependencies:
            if dep in self.failed_rollbacks:
                return True

        return False

    def rollback(self, state_tracker: StateTracker,
                operations_map: dict) -> bool:
        """
        Rollback all successful operations in reverse order with dependency checking.

        Args:
            state_tracker: Tracker with operation states
            operations_map: Map of operation names to operation instances
                           e.g., {"Stop VM": stop_vm_operation}

        Returns:
            True if all rollbacks succeeded, False if any failed

        Dependency checking:
            - If a critical rollback fails (e.g., re-attach boot disk),
              dependent rollbacks are skipped to avoid making things worse
            - Example: If boot disk re-attach fails, VM start is skipped
        """

        # Reset failed rollbacks tracker
        self.failed_rollbacks = set()

        rollback_ops = state_tracker.get_rollback_operations()

        if not rollback_ops:
            self._log_debug("No operations to rollback")
            return True

        self._log_info("")
        self._log_info("Rolling back operations...")
        self._log_debug(f"Rolling back {len(rollback_ops)} operations in reverse order")

        all_succeeded = True
        critical_failure = False

        for op_state in rollback_ops:
            op_name = op_state.operation_name
            self._log_debug(f"Rolling back: {op_name}")

            # Check if we should skip due to failed dependencies
            if self._should_skip_rollback(op_name):
                self._log_error(f"  [SKIP] {op_name}: skipped due to failed dependency")
                self._log_error(f"         (Cannot {op_name.lower()} safely after previous rollback failure)")
                all_succeeded = False
                continue

            # Get the operation instance
            operation = operations_map.get(op_name)

            if not operation:
                self._log_error(f"  [X] Cannot rollback {op_name}: operation not found")
                self.failed_rollbacks.add(op_name)
                all_succeeded = False
                continue

            if not op_state.rollback_data:
                self._log_debug(f"  → {op_name}: no rollback needed")
                continue

            # Perform rollback
            try:
                success = operation.rollback(op_state.rollback_data)

                if success:
                    self._log_debug(f"  [OK] Rolled back: {op_name}")
                else:
                    self._log_error(f"  [X] Failed to rollback: {op_name}")
                    self.failed_rollbacks.add(op_name)
                    all_succeeded = False

                    # Check if this was a critical operation
                    if op_name in CRITICAL_ROLLBACK_OPS:
                        critical_failure = True
                        self._log_error(f"  [!] CRITICAL: {op_name} rollback failed!")
                        self._log_error(f"      VM may be in an inconsistent state.")

            except Exception as e:
                self._log_error(f"  [X] Error rolling back {op_name}: {str(e)}")
                self.failed_rollbacks.add(op_name)
                all_succeeded = False

                if op_name in CRITICAL_ROLLBACK_OPS:
                    critical_failure = True

        # Final status
        if all_succeeded:
            self._log_info("[OK] Rollback completed successfully")
        elif critical_failure:
            self._log_error("")
            self._log_error("=" * 60)
            self._log_error("[CRITICAL] Rollback failed - VM may be in broken state!")
            self._log_error("=" * 60)
            self._log_error("")
            self._log_error("Manual intervention required:")
            self._log_error("  1. Check VM status: gcloud compute instances describe <vm> --zone=<zone>")
            self._log_error("  2. Check attached disks")
            self._log_error("  3. Manually reattach boot disk if needed")
            self._log_error("  4. Start VM manually once disk is attached")
        else:
            self._log_error("[X] Rollback completed with errors")
            self._log_error("Manual intervention may be required")

        return all_succeeded
