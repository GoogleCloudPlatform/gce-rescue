"""
GCE Rescue - Stop VM Operation

Stops a VM instance.
Rollback: Restarts the VM if it was running before.
"""

import time
from operations.base import BaseOperation, OperationResult


class StopVMOperation(BaseOperation):
    """
    Stops a VM instance.

    Rollback: Restarts the VM if it was running before.

    Example:
        operation = StopVMOperation(compute, project, zone, logger)
        result = operation.execute(vm_name='my-instance')

        if result.success:
            print("VM stopped!")
            # Later, if we need to rollback:
            operation.rollback(result.rollback_data)
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Stop VM"

    def execute(self, vm_name: str, timeout: int = 300) -> OperationResult:
        """
        Stop the VM instance.

        Args:
            vm_name: Name of the VM to stop
            timeout: Maximum seconds to wait for VM to stop (default: 5 minutes)

        Returns:
            OperationResult with success status and rollback data
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

                self.compute.instances().stop(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()

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
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for VM to stop (>{timeout}s)"
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
            self._log_error(f"Failed to stop VM: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to stop VM",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Restart the VM if it was running before.

        Args:
            rollback_data: Data from execute() containing original VM state

        Returns:
            True if rollback successful, False otherwise
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
