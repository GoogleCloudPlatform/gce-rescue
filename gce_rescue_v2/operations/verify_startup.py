"""
Verify Startup Script operation.

Polls VM serial console output for completion marker to verify
startup script executed successfully.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message

# Failure lines the base mount script prints right before exiting 1. Once one
# of these is on the serial console the completion marker can never arrive,
# so verification aborts immediately instead of polling out the full timeout
# (observed live: 36 minutes spent waiting on a mount that had already
# failed). The serial buffer is wiped on VM stop and every rescue starts with
# a stop, so these lines can only come from the CURRENT rescue boot.
STARTUP_FAILURE_MARKERS = (
    'ERROR: All mount attempts failed',
    'ERROR: Disk not found after 5 minutes',
    'ERROR: No supported filesystem found!',
)


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
                timeout: int = 120, tracking_label: str = None,
                expected_completion_value: str = 'COMPLETE') -> OperationResult:
        """
        Verify startup script completion by polling serial console.

        Args:
            vm_name: VM instance name
            completion_marker: String to search for in serial output
            timeout: Maximum seconds to wait (default: 120)
            tracking_label: Optional tracking label for analytics
            expected_completion_value: Exact guest-attribute value that counts
                as completion. Guest attributes persist across stop/start/
                restore and cannot be cleared from outside the VM, so the
                orchestrator passes a per-session token here - a stale value
                from a PREVIOUS rescue of the same VM must not short-circuit
                this session's verification.

        Returns:
            OperationResult with success=True if marker found, False if timeout
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")
        self._log_debug(f"  Looking for marker: {completion_marker}")
        self._log_debug(f"  Timeout: {timeout}s")

        start_time = time.time()
        poll_interval = 5  # Poll every 5 seconds (consistent with V2 patterns)
        # Most recent serial output seen; dumped to the log on timeout so
        # failures are diagnosable from the log alone (no separate serial pull).
        last_serial = ''
        # How much trailing serial output to keep for diagnostics.
        serial_tail_chars = 4000

        try:
            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute

            while True:
                elapsed = time.time() - start_time

                # Check timeout
                if elapsed > timeout:
                    serial_tail = last_serial[-serial_tail_chars:] if last_serial else ''
                    self._log_info(
                        f"Startup verification timed out after {timeout}s "
                        f"(marker '{completion_marker}' not seen)"
                    )
                    if serial_tail:
                        # DEBUG so it always lands in the log file (file handler
                        # is DEBUG) without spamming the console.
                        self._log_debug(
                            f"Last serial console output (tail) at timeout:\n"
                            f"{'-' * 60}\n{serial_tail}\n{'-' * 60}"
                        )
                    else:
                        self._log_debug("No serial console output captured before timeout")
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for startup script ({timeout}s)",
                        error=f"Startup script did not complete within {timeout}s",
                        details={
                            'timed_out': True,
                            'timeout_seconds': timeout,
                            'serial_tail': serial_tail,
                        }
                    )

                # Reliable completion signal: a guest attribute set by the
                # startup script. Checked before serial because the serial
                # console can drop the script's final output burst before the
                # process exits (the marker may never reach serial).
                if self._completion_guest_attribute_set(
                        compute, vm_name, expected_completion_value):
                    duration = time.time() - start_time
                    self._log_debug(
                        f"Completion guest attribute set in {duration:.1f}s"
                    )
                    return OperationResult(
                        operation_name=self.name,
                        success=True,
                        message=f"Startup script completed ({duration:.0f}s)",
                        rollback_data=None
                    )

                # Poll serial console (fallback)
                try:
                    result = compute.instances().getSerialPortOutput(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()

                    contents = result.get('contents', '')
                    if contents:
                        last_serial = contents

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

                    # A terminal failure line means the marker can never
                    # arrive - abort now instead of polling out the timeout.
                    for failure_marker in STARTUP_FAILURE_MARKERS:
                        if failure_marker in contents:
                            self._log_debug(
                                f"Startup script failed: {failure_marker!r}"
                            )
                            return OperationResult(
                                operation_name=self.name,
                                success=False,
                                message=f"Startup script failed: {failure_marker}",
                                error=(
                                    f"Startup script reported a terminal "
                                    f"failure: {failure_marker}"
                                )
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

    def _completion_guest_attribute_set(self, compute, vm_name: str,
                                        expected_value: str = 'COMPLETE') -> bool:
        """Check whether the guest set gce-rescue/status to THIS session's value.

        Reliable, deterministic completion signal (unlike serial scraping).
        The value comparison is exact (case-insensitive): guest attributes
        survive stop/start/restore, so a stale value from a previous rescue
        session must not count. Returns False on any error (attribute not set
        yet, guest attributes disabled, 404) so the caller keeps polling /
        falls back to serial.
        """
        expected = expected_value.strip().upper()
        try:
            resp = compute.instances().getGuestAttributes(
                project=self.project, zone=self.zone, instance=vm_name,
                queryPath='gce-rescue/status'
            ).execute()
            # Querying a specific key returns variableValue; a path returns items.
            if str(resp.get('variableValue', '')).strip().upper() == expected:
                return True
            for item in resp.get('queryValue', {}).get('items', []):
                if str(item.get('value', '')).strip().upper() == expected:
                    return True
        except Exception:
            return False
        return False

    def rollback(self, rollback_data: dict) -> bool:
        """
        No rollback needed for verification operation.
        This is a read-only check.
        """
        return True
