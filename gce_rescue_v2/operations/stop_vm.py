"""
Stop VM operation.

Stops a Compute Engine VM instance, waiting until it reaches TERMINATED.
Rollback restarts the VM only if it was originally running prior to execution.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, VM_STOP_TIMEOUT, VM_NOT_FOUND


class StopVMOperation(BaseOperation):
    """Operation that stops a VM instance.

    Captures the original VM status so rollback can restart the VM only when it
    was RUNNING before stopping.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Stop VM"

    def execute(self, vm_name: str, timeout: int = 300, discard_local_ssd: bool = False) -> OperationResult:
        """
        Stop the specified VM and wait until it is TERMINATED.

        Args:
            vm_name (str): Name of the VM to stop.
            timeout (int): Maximum seconds to wait for the VM to reach
                TERMINATED. Default is 300.
            discard_local_ssd (bool): If True, allows stopping VMs with Local SSDs
                attached (data on Local SSDs will be lost). Default is False.

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `original_status`.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")

        try:
            # Step 1: Get current VM state (for rollback)
            self._log_debug(f"Getting current VM state...")
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            original_status = vm['status']
            self._log_debug(f"Current VM status: {original_status}")

            # Step 2: Stop the VM (only if running)
            if original_status == 'RUNNING':
                self._log_debug(f"VM is RUNNING, stopping...")

                # Build stop request parameters
                stop_params = {
                    'project': self.project,
                    'zone': self.zone,
                    'instance': vm_name
                }

                # Add discardLocalSsd if needed (for VMs with Local SSDs)
                if discard_local_ssd:
                    stop_params['discardLocalSsd'] = True
                    self._log_debug("Using discardLocalSsd=True for Local SSD VM")

                self.compute.instances().stop(**stop_params).execute()

                # Step 3: Wait for VM to stop
                self._log_debug("Waiting for VM to stop...")
                start_time = time.time()

                def get_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if not self._wait_for_status(get_status, 'TERMINATED', timeout):
                    # Enhanced error message with suggestions
                    error_detail = VM_STOP_TIMEOUT.format(
                        vm_name=vm_name,
                        zone=self.zone,
                        project=self.project
                    )
                    self._log_error(error_detail)
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for VM to stop (>{timeout}s)",
                        error=error_detail
                    )

                duration = time.time() - start_time
                self._log_debug(f"VM stopped in {duration:.2f}s")

                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message=f"VM stopped ({duration:.0f}s)",
                    rollback_data={
                        'vm_name': vm_name,
                        'original_status': original_status
                    }
                )

            elif original_status == 'TERMINATED':
                self._log_debug(f"VM already stopped")

                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message="VM already stopped",
                    rollback_data={
                        'vm_name': vm_name,
                        'original_status': original_status
                    }
                )

            else:
                # VM is in some other state (STOPPING, SUSPENDING, etc.)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"VM is in unexpected state: {original_status}",
                    error=f"Cannot stop VM in state: {original_status}"
                )

        except Exception as e:
            error_msg = extract_error_message(e)

            # Get enhanced error suggestion based on error type
            suggestion = get_error_suggestion(error_msg, operation='stop_vm')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project
                )
            else:
                error_detail = f"Failed to stop VM: {error_msg}"

            self._log_error(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to stop VM: {error_msg}",
                error=error_detail
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Restart the VM if it was RUNNING before execution.

        Args:
            rollback_data (dict): Data from `execute()` with `vm_name` and
                `original_status`.

        Returns:
            bool: True if rollback succeeded or not needed; False otherwise.

        Raises:
            None
        """

        try:
            vm_name = rollback_data['vm_name']
            original_status = rollback_data['original_status']

            self._log_debug(f"Rolling back {self.name} for {vm_name}")
            self._log_debug(f"Original status was: {original_status}")

            # Only restart if VM was running before
            if original_status == 'RUNNING':
                self._log_info(f"  Restarting VM {vm_name}...")

                self.compute.instances().start(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()

                # Wait for VM to start
                def get_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if self._wait_for_status(get_status, 'RUNNING'):
                    self._log_info(f"  [OK] VM restarted")
                    return True
                else:
                    self._log_error(f"  [X] Timeout restarting VM")
                    return False

            else:
                # VM was already stopped, nothing to rollback
                self._log_debug(f"VM was not running, no rollback needed")
                return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
