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
import json

from googleapiclient import discovery
import googleapiclient.http
import google_auth_httplib2
import httplib2


def extract_error_message(exception: Exception) -> str:
    """
    Extract a clean, user-friendly error message from GCP API exceptions.

    GCP HttpError exceptions contain verbose JSON with nested error details.
    This function extracts just the meaningful message for display.

    Args:
        exception: Any exception, but optimized for googleapiclient.errors.HttpError

    Returns:
        Clean error message string

    Example:
        Input (raw HttpError):
            <HttpError 404 ... {"error": {"message": "The resource 'disk-1' was not found"}}>
        Output:
            "The resource 'disk-1' was not found"
    """
    error_str = str(exception)

    # Try to extract message from GCP HttpError JSON response
    try:
        # HttpError format: <HttpError XXX ... {json}>
        # Find the JSON part (starts with '{')
        json_start = error_str.find('{')
        if json_start != -1:
            json_str = error_str[json_start:]
            # Find matching closing brace
            brace_count = 0
            json_end = 0
            for i, char in enumerate(json_str):
                if char == '{':
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break

            if json_end > 0:
                error_data = json.loads(json_str[:json_end])
                # GCP error format: {"error": {"message": "...", "errors": [...]}}
                if 'error' in error_data:
                    error_info = error_data['error']
                    if 'message' in error_info:
                        return error_info['message']
                    if 'errors' in error_info and error_info['errors']:
                        return error_info['errors'][0].get('message', error_str)
    except (json.JSONDecodeError, KeyError, IndexError):
        pass

    # Fallback: return original string, but truncate if too long
    if len(error_str) > 200:
        return error_str[:200] + "..."
    return error_str


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
        # Error message from the most recent _wait_for_operation failure, so
        # callers can surface the real cause (e.g. an org-policy violation that
        # only appears in the async operation result).
        self._last_operation_error: Optional[str] = None

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
        """Log debug message with component prefix."""
        if self.logger:
            self.logger.debug(f"[{self.name}] {message}", stacklevel=2)

    def _log_info(self, message: str):
        """Log info message if logger available."""
        if self.logger:
            self.logger.info(message)

    def _log_error(self, message: str):
        """Log error message if logger available."""
        if self.logger:
            self.logger.error(message)

    def _create_tracked_client(self, user_agent: str):
        """Create a compute client with a custom User-Agent for analytics.

        Args:
            user_agent: Full User-Agent string (from build_user_agent()).

        Returns:
            Compute API client with the custom User-Agent header.
        """
        credentials = self.compute._http.credentials

        def _request_builder(http, *args, **kwargs):
            headers = kwargs.setdefault('headers', {})
            headers['user-agent'] = user_agent
            auth_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http()
            )
            return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

        return discovery.build(
            'compute', 'v1', credentials=credentials,
            cache_discovery=False, requestBuilder=_request_builder
        )

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

    def _wait_for_operation(self, operation: dict, timeout: int = 300) -> bool:
        """
        Wait for a GCP Zone Operation to complete.

        GCP API calls return an operation object that runs asynchronously.
        This method polls the operation status until it reaches 'DONE'.

        Args:
            operation: The operation response from a GCP API call
            timeout: Maximum seconds to wait (default: 300)

        Returns:
            True if operation completed successfully, False if timeout or error

        Example:
            operation = compute.instances().attachDisk(...).execute()
            if self._wait_for_operation(operation):
                print("Disk attached!")
        """
        operation_name = operation.get('name')
        if not operation_name:
            self._log_debug("No operation name found, assuming synchronous completion")
            return True

        self._log_debug(f"Waiting for operation: {operation_name}")
        start_time = time.time()

        while True:
            # Check timeout
            elapsed = time.time() - start_time
            if elapsed > timeout:
                self._log_error(f"Operation timeout after {timeout}s: {operation_name}")
                return False

            # Poll operation status
            try:
                result = self.compute.zoneOperations().get(
                    project=self.project,
                    zone=self.zone,
                    operation=operation_name
                ).execute()

                status = result.get('status', 'UNKNOWN')
                self._log_debug(f"Operation {operation_name}: {status} ({elapsed:.0f}s)")

                if status == 'DONE':
                    # Check for errors
                    if 'error' in result:
                        errors = result['error'].get('errors', [])
                        error_msg = '; '.join([e.get('message', 'Unknown error') for e in errors])
                        self._last_operation_error = error_msg
                        self._log_error(f"Operation failed: {error_msg}")
                        return False
                    self._last_operation_error = None
                    return True

            except Exception as e:
                self._log_debug(f"Error polling operation: {e}")

            # Wait before polling again
            time.sleep(2)
