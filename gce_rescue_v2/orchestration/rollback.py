"""
GCE Rescue - Rollback Handler

Handles rollback of operations when something fails.
Undoes operations in reverse order.
"""

from typing import List
from orchestration.state import StateTracker, OperationState
from operations.base import BaseOperation


class RollbackHandler:
    """
    Handles rollback of operations.

    When an operation fails, this undoes all previous successful operations
    in reverse order, returning the system to its original state.

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

    def _log_info(self, message: str):
        """Log info message."""
        if self.logger:
            self.logger.info(message)

    def _log_error(self, message: str):
        """Log error message."""
        if self.logger:
            self.logger.error(message)

    def _log_debug(self, message: str):
        """Log debug message."""
        if self.logger:
            self.logger.debug(message)

    def rollback(self, state_tracker: StateTracker,
                operations_map: dict) -> bool:
        """
        Rollback all successful operations in reverse order.

        Args:
            state_tracker: Tracker with operation states
            operations_map: Map of operation names to operation instances
                           e.g., {"Stop VM": stop_vm_operation}

        Returns:
            True if all rollbacks succeeded, False if any failed
        """

        rollback_ops = state_tracker.get_rollback_operations()

        if not rollback_ops:
            self._log_debug("No operations to rollback")
            return True

        self._log_info("")
        self._log_info("Rolling back operations...")
        self._log_debug(f"Rolling back {len(rollback_ops)} operations in reverse order")

        all_succeeded = True

        for op_state in rollback_ops:
            self._log_debug(f"Rolling back: {op_state.operation_name}")

            # Get the operation instance
            operation = operations_map.get(op_state.operation_name)

            if not operation:
                self._log_error(f"  [X] Cannot rollback {op_state.operation_name}: operation not found")
                all_succeeded = False
                continue

            if not op_state.rollback_data:
                self._log_debug(f"  → {op_state.operation_name}: no rollback needed")
                continue

            # Perform rollback
            try:
                success = operation.rollback(op_state.rollback_data)

                if success:
                    self._log_debug(f"  [OK] Rolled back: {op_state.operation_name}")
                else:
                    self._log_error(f"  [X] Failed to rollback: {op_state.operation_name}")
                    all_succeeded = False

            except Exception as e:
                self._log_error(f"  [X] Error rolling back {op_state.operation_name}: {str(e)}")
                all_succeeded = False

        if all_succeeded:
            self._log_info("[OK] Rollback completed successfully")
        else:
            self._log_error("[X] Rollback completed with errors")
            self._log_error("Manual intervention may be required")

        return all_succeeded
