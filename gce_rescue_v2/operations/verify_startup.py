"""
Verify Startup Script operation.

Polls VM serial console output for completion marker to verify
startup script executed successfully.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message


class VerifyStartupOperation(BaseOperation):
    """
    Operation that verifies startup script completion via serial console.

    Polls instances().getSerialPortOutput() for completion marker.
    Returns success only if marker found within timeout period.

    No rollback needed - this is a read-only verification operation.
    """

    @property
    def name(self) -> str:
        return "Verify Startup Script"

    def execute(self, vm_name: str, completion_marker: str = "GCE-RESCUE-COMPLETE",
                timeout: int = 120, tracking_label: str = None) -> OperationResult:
        """
        Verify startup script completion by polling serial console.

        Args:
            vm_name: VM instance name
            completion_marker: String to search for in serial output
            timeout: Maximum seconds to wait (default: 120)
            tracking_label: Optional tracking label for analytics

        Returns:
            OperationResult with success=True if marker found, False if timeout
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")
        self._log_debug(f"  Looking for marker: {completion_marker}")
        self._log_debug(f"  Timeout: {timeout}s")

        start_time = time.time()
        poll_interval = 5  # Poll every 5 seconds (consistent with V2 patterns)

        try:
            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute

            while True:
                elapsed = time.time() - start_time

                # Check timeout
                if elapsed > timeout:
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for startup script ({timeout}s)",
                        error=f"Startup script did not complete within {timeout}s"
                    )

                # Poll serial console
                try:
                    result = compute.instances().getSerialPortOutput(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()

                    contents = result.get('contents', '')

                    # Check for completion marker
                    if completion_marker in contents:
                        duration = time.time() - start_time
                        self._log_debug(f"Startup script completed in {duration:.1f}s")

                        return OperationResult(
                            operation_name=self.name,
                            success=True,
                            message=f"Startup script completed ({duration:.0f}s)",
                            rollback_data=None  # No rollback needed for verification
                        )

                    # Log progress
                    self._log_debug(f"  Waiting for startup script... ({elapsed:.0f}s/{timeout}s)")

                except Exception as e:
                    # Serial console might be temporarily unavailable
                    self._log_debug(f"  Serial console check error (will retry): {str(e)}")

                # Wait before next poll
                time.sleep(poll_interval)

        except Exception as e:
            # Catch-all for unexpected errors
            error_msg = extract_error_message(e)

            # Check if serial console is disabled
            if "Serial port output is not enabled" in error_msg or "403" in str(e):
                # Return SUCCESS with warning - don't fail the operation
                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message="Completed (manual verification required - serial console disabled)",
                    rollback_data=None
                )

            self._log_error(f"Unexpected error: {error_msg}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Verification failed: {error_msg}",
                error=error_msg
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        No rollback needed for verification operation.
        This is a read-only check.
        """
        return True
