"""
Start VM operation.

Starts a Compute Engine VM instance, waiting until it reaches RUNNING. Rollback
stops the VM only if it was previously stopped (TERMINATED) before execution.
"""

from dataclasses import dataclass
import time
from typing import Optional
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, VM_START_TIMEOUT

_MAX_START_RETRIES = 2
_TERMINATED_POLLS_BEFORE_RETRY = 3


@dataclass
class _StartRetryState:
    """Tracks VM status transitions while waiting for a start request."""

    saw_stopping: bool = False
    terminated_polls: int = 0
    retries: int = 0

    def should_retry(self, status: str) -> bool:
        """Update state for the latest status and return True if start() should be re-issued."""
        if status == 'STOPPING':
            self.saw_stopping = True
            self.terminated_polls = 0
            return False
        if status == 'TERMINATED':
            self.terminated_polls += 1
            if (
                self.saw_stopping
                or self.terminated_polls >= _TERMINATED_POLLS_BEFORE_RETRY
            ) and self.retries < _MAX_START_RETRIES:
                self.retries += 1
                self.saw_stopping = False
                self.terminated_polls = 0
                return True
            return False
        self.terminated_polls = 0
        return False


class StartVMOperation(BaseOperation):
    """Operation that starts a VM instance.

    Records the original VM status so rollback can stop the VM only when it
    was originally in TERMINATED state prior to execution.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Start VM"

    def execute(self, vm_name: str, timeout: int = 300, tracking_label: str = None) -> OperationResult:
        """
        Start the specified VM and wait until it is RUNNING.

        Args:
            vm_name (str): Name of the VM to start.
            timeout (int): Maximum seconds to wait for the VM to reach
                RUNNING. Default is 300.
            tracking_label (str): Tracking label for usage analytics.
                Format: '{operation_type}-{action_group}-{action_detail}'
                Example: 'rescue-vm-start'

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `original_status`.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            # Get current state
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            original_status = vm['status']
            self._log_debug(f"Current VM status: {original_status}")

            if original_status == 'STOPPING':
                # VM is stopping, wait for TERMINATED then start
                self._log_debug("VM is STOPPING, waiting for TERMINATED...")

                def get_stopping_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if not self._wait_for_status(get_stopping_status, 'TERMINATED', timeout // 2):
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message="Timeout waiting for VM to stop",
                        error="VM stuck in STOPPING state"
                    )

                original_status = 'TERMINATED'
                timeout /= 2

            if original_status == 'TERMINATED':
                compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute
                start_time = time.time()
                deadline = start_time + timeout
                retry_state = _StartRetryState()
                retry = False

                def get_start_progress():
                    nonlocal retry
                    status = self.compute.instances().get(
                        project=self.project, zone=self.zone, instance=vm_name
                    ).execute()['status']
                    retry = retry_state.should_retry(status)
                    return 'settled' if retry or status == 'RUNNING' else status

                while True:
                    reached = False
                    operation = compute.instances().start(
                        project=self.project, zone=self.zone, instance=vm_name
                    ).execute()
                    self._last_operation_error = None
                    if not self._wait_for_operation(operation, deadline - time.time()):
                        if self._last_operation_error:
                            raise RuntimeError(self._last_operation_error)
                        break
                    reached = self._wait_for_status(get_start_progress, 'settled', deadline - time.time())
                    if not reached or not retry:
                        break
                    self._log_debug(f"VM still TERMINATED; re-issuing start ({retry_state.retries}/{_MAX_START_RETRIES})")

                if not reached:
                    error_detail = VM_START_TIMEOUT.format(
                        vm_name=vm_name,
                        zone=self.zone,
                        project=self.project
                    )
                    self._log_debug(error_detail)
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for VM to start (>{timeout}s)",
                        error=error_detail
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

            elif original_status in ('STAGING', 'PROVISIONING'):
                # VM is already starting up, just wait for it
                self._log_debug(f"VM is {original_status}, waiting for RUNNING...")
                start_time = time.time()

                def get_status():
                    vm = self.compute.instances().get(
                        project=self.project,
                        zone=self.zone,
                        instance=vm_name
                    ).execute()
                    return vm['status']

                if not self._wait_for_status(get_status, 'RUNNING', timeout):
                    error_detail = VM_START_TIMEOUT.format(
                        vm_name=vm_name,
                        zone=self.zone,
                        project=self.project
                    )
                    self._log_debug(error_detail)
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Timeout waiting for VM to reach RUNNING (>{timeout}s)",
                        error=error_detail
                    )

                duration = time.time() - start_time
                self._log_debug(f"VM reached RUNNING in {duration:.2f}s")

                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message=f"VM started ({duration:.0f}s)",
                    rollback_data={
                        'vm_name': vm_name,
                        'original_status': 'TERMINATED'  # Treat as if it was terminated
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
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='start_vm')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project
                )
            else:
                error_detail = f"Failed to start VM: {error_msg}"
            self._log_debug(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to start VM: {error_msg}",
                error=error_detail
            )

    def rollback(self, rollback_data: dict,
                 tracking_label: Optional[str] = None) -> bool:
        """
        Stop the VM if it was originally TERMINATED before execution.

        Args:
            rollback_data (dict): Data from `execute()` including `vm_name`
                and `original_status`.
            tracking_label (str, optional): User-Agent for the rollback's
                API call (see BaseOperation.rollback).

        Returns:
            bool: True if rollback succeeded or was not needed; False on stop
            timeout or error.

        Raises:
            None
        """

        try:
            vm_name = rollback_data['vm_name']
            original_status = rollback_data['original_status']

            self._log_debug(f"Rolling back {self.name} for {vm_name}")

            # Only stop if VM was stopped before
            if original_status == 'TERMINATED':
                self._log_info(f"  Stopping VM {vm_name}...")

                compute = self._rollback_client(tracking_label)
                compute.instances().stop(
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
