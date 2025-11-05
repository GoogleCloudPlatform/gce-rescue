"""
GCE Rescue - Base Operation

This module provides the base class for all operations.
Each operation does ONE thing and knows how to undo itself (rollback).

Key concept: Every operation saves "rollback_data" during execute().
If something fails later, we use this data to undo the operation.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any
import time


@dataclass
class OperationResult:
    """
    Result from an operation.

    Attributes:
        operation_name: Name of the operation (for display)
        success: True if operation succeeded, False if failed
        message: Human-readable message about the result
        rollback_data: Data needed to rollback this operation (if it fails later)
        error: Optional error details
    """
    operation_name: str
    success: bool
    message: str
    rollback_data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None

    def __str__(self):
        """String representation."""
        status = "[OK]" if self.success else "[X]"
        return f"{status} {self.operation_name}: {self.message}"


class BaseOperation(ABC):
    """
    Base class for all operations.

    Every operation must:
    1. Inherit from this class
    2. Implement the execute() method (do the operation)
    3. Implement the rollback() method (undo the operation)
    4. Implement the name property

    Key concept: ROLLBACK
    Each operation saves "rollback_data" when executed.
    If something fails later, we can use this data to undo the operation.

    Example:
        Operation: Stop VM
        Rollback data: {"vm_name": "my-vm", "original_state": "RUNNING"}
        Rollback action: Start the VM (because it was running before)

    Example usage:
        operation = StopVMOperation(compute, project, zone)
        result = operation.execute(vm_name='my-instance')

        if result.success:
            print("VM stopped!")
            # Later, if we need to rollback:
            operation.rollback(result.rollback_data)
        else:
            print(f"Failed: {result.message}")
    """

    def __init__(self, compute, project: str, zone: str, logger=None):
        """
        Initialize operation.

        Args:
            compute: GCP compute client
            project: GCP project ID
            zone: GCP zone
            logger: Optional logger for debug output
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.logger = logger
        self.result: Optional[OperationResult] = None

    @abstractmethod
    def execute(self, **kwargs) -> OperationResult:
        """
        Execute the operation.

        This method must:
        1. Perform the operation
        2. Save rollback_data (for rollback later)
        3. Return OperationResult with success/failure

        Args:
            **kwargs: Operation-specific parameters

        Returns:
            OperationResult with success status and rollback data
        """
        pass

    @abstractmethod
    def rollback(self, rollback_data: Dict[str, Any]) -> bool:
        """
        Rollback (undo) this operation.

        This uses the rollback_data saved during execute()
        to undo the operation and return to the original state.

        Args:
            rollback_data: Data saved during execute()

        Returns:
            True if rollback successful, False otherwise
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Human-readable name of this operation.

        Used for display and logging.
        """
        pass

    def _log_debug(self, message: str):
        """Log debug message if logger available."""
        if self.logger:
            self.logger.debug(message)

    def _log_info(self, message: str):
        """Log info message if logger available."""
        if self.logger:
            self.logger.info(message)

    def _log_error(self, message: str):
        """Log error message if logger available."""
        if self.logger:
            self.logger.error(message)

    def _wait_for_status(self, check_func, target_status: str, timeout: int = 300) -> bool:
        """
        Wait for a resource to reach a target status.

        Args:
            check_func: Function that returns current status
            target_status: Status to wait for
            timeout: Maximum seconds to wait

        Returns:
            True if reached target status, False if timeout
        """
        start_time = time.time()

        while True:
            # Check timeout
            if time.time() - start_time > timeout:
                self._log_error(f"Timeout waiting for status: {target_status}")
                return False

            # Check current status
            current_status = check_func()
            self._log_debug(f"Current status: {current_status}, Target: {target_status}")

            if current_status == target_status:
                return True

            # Wait before checking again
            time.sleep(5)
