"""
GCE Rescue - Start VM Operation

Starts a VM instance.
Rollback: Stops the VM if it was stopped before.
"""

import time
from operations.base import BaseOperation, OperationResult


class StartVMOperation(BaseOperation):
    """
    Starts a VM instance.

    Rollback: Stops the VM if it was stopped before.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Start VM"

    def execute(self, vm_name: str, timeout: int = 300) -> OperationResult:
        """
        Start the VM instance.

        Args:
            vm_name: Name of the VM to start
            timeout: Maximum seconds to wait for VM to start

        Returns:
            OperationResult with success status and rollback data
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")

        try:
            # Get current state
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            original_status = vm['status']
            self._log_debug(f"Current VM status: {original_status}")

            if original_status == 'TERMINATED':
                self._log_debug("VM is TERMINATED, starting...")

                self.compute.instances().start(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()

                # Wait for VM to start
                start_time = time.time()

                def get_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if not self._wait_for_status(get_status, 'RUNNING', timeout):
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for VM to start (>{timeout}s)"
                    )

                duration = time.time() - start_time
                self._log_debug(f"VM started in {duration:.2f}s")

                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message=f"VM started ({duration:.0f}s)",
                    rollback_data={
                        'vm_name': vm_name,
                        'original_status': original_status
                    }
                )

            elif original_status == 'RUNNING':
                self._log_debug("VM already running")

                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message="VM already running",
                    rollback_data={
                        'vm_name': vm_name,
                        'original_status': original_status
                    }
                )

            else:
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"VM is in unexpected state: {original_status}",
                    error=f"Cannot start VM in state: {original_status}"
                )

        except Exception as e:
            self._log_error(f"Failed to start VM: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to start VM",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Stop the VM if it was stopped before.

        Args:
            rollback_data: Data from execute()

        Returns:
            True if rollback successful
        """

        try:
            vm_name = rollback_data['vm_name']
            original_status = rollback_data['original_status']

            self._log_debug(f"Rolling back {self.name} for {vm_name}")

            # Only stop if VM was stopped before
            if original_status == 'TERMINATED':
                self._log_info(f"  Stopping VM {vm_name}...")

                self.compute.instances().stop(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()

                def get_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if self._wait_for_status(get_status, 'TERMINATED'):
                    self._log_info(f"  [OK] VM stopped")
                    return True
                else:
                    self._log_error(f"  [X] Timeout stopping VM")
                    return False

            else:
                self._log_debug("VM was running, no rollback needed")
                return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
