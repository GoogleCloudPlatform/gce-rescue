"""
GCE Rescue - Operation State Tracking

Tracks the state of rescue/restore operations.
This allows us to know where we are in the process and rollback if needed.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any
from datetime import datetime


@dataclass
class OperationState:
    """
    State of a single operation.

    Tracks what was done and data needed for rollback.
    """
    operation_name: str
    success: bool
    message: str
    rollback_data: Dict[str, Any] = None
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class StateTracker:
    """
    Tracks the state of all operations in a workflow.

    This keeps track of:
    - Which operations completed successfully
    - Rollback data for each operation
    - Order of operations (for rollback in reverse)

    Example:
        tracker = StateTracker()

        # Record operation
        tracker.add_operation("Stop VM", success=True, rollback_data={...})
        tracker.add_operation("Create Disk", success=True, rollback_data={...})
        tracker.add_operation("Attach Disk", success=False)

        # Get operations for rollback (in reverse order)
        for op in tracker.get_rollback_operations():
            # Rollback this operation
            pass
    """

    def __init__(self):
        """Initialize empty state tracker."""
        self.operations: List[OperationState] = []
        self.workflow_start_time = datetime.now()

    def add_operation(self, operation_name: str, success: bool,
                     message: str, rollback_data: Dict[str, Any] = None):
        """
        Record an operation.

        Args:
            operation_name: Name of the operation
            success: Whether it succeeded
            message: Result message
            rollback_data: Data needed for rollback
        """
        state = OperationState(
            operation_name=operation_name,
            success=success,
            message=message,
            rollback_data=rollback_data
        )
        self.operations.append(state)

    def get_successful_operations(self) -> List[OperationState]:
        """Get only successful operations."""
        return [op for op in self.operations if op.success]

    def get_failed_operations(self) -> List[OperationState]:
        """Get only failed operations."""
        return [op for op in self.operations if not op.success]

    def get_rollback_operations(self) -> List[OperationState]:
        """
        Get operations that need rollback (in reverse order).

        Returns successful operations in reverse order, so we undo
        them from last to first.
        """
        successful = self.get_successful_operations()
        return list(reversed(successful))

    def all_succeeded(self) -> bool:
        """Check if all operations succeeded."""
        return all(op.success for op in self.operations)

    def get_summary(self) -> str:
        """Get summary of operations."""
        total = len(self.operations)
        successful = len(self.get_successful_operations())
        failed = len(self.get_failed_operations())

        duration = (datetime.now() - self.workflow_start_time).total_seconds()

        summary = f"Operations: {successful}/{total} succeeded"
        if failed > 0:
            summary += f", {failed} failed"
        summary += f" (took {duration:.1f}s)"

        return summary

    def print_summary(self):
        """Print detailed summary."""
        print()
        print("Operation Summary:")
        print(f"  Total: {len(self.operations)}")
        print(f"  Successful: {len(self.get_successful_operations())}")
        print(f"  Failed: {len(self.get_failed_operations())}")
        print()

        for i, op in enumerate(self.operations, 1):
            status = "[OK]" if op.success else "[X]"
            print(f"  {i}. {status} {op.operation_name}: {op.message}")
