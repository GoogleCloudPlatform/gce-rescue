"""
Set Machine Type operation.

Changes the machine type of a stopped Compute Engine VM. The VM must be
TERMINATED: stopping and starting it are separate operations.
Rollback sets the original machine type back while the VM is stopped.
"""

import time
from typing import Optional
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion
from ..core.machine_types import machine_type_name


class SetMachineTypeOperation(BaseOperation):
    """Operation that changes the machine type of a stopped VM."""

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Set Machine Type"

    def execute(self, vm_name: str, machine_type: str, timeout: int = 300,
                tracking_label: str = None) -> OperationResult:
        """
        Change the machine type of a stopped VM.

        Args:
            vm_name (str): Name of the VM.
            machine_type (str): Target machine type name (e.g. e2-medium).
            timeout (int): Maximum seconds to wait for the operation.
            tracking_label (str, optional): Tracking User-Agent string.

        Returns:
            OperationResult: Result containing `vm_name`,
            `previous_machine_type` and `new_machine_type` in
            `rollback_data`. Succeeds without an API call when the VM
            already has the target type (e.g. on a resumed run).
        """
        self._log_debug(f"Executing {self.name}: {vm_name} -> {machine_type}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            compute = (
                self._create_tracked_client(tracking_label)
                if tracking_label else self.compute
            )
            vm = compute.instances().get(
                project=self.project, zone=self.zone, instance=vm_name
            ).execute()
            status = vm.get('status')
            current_type = machine_type_name(vm.get('machineType', ''))
            rollback_data = {
                'vm_name': vm_name,
                'previous_machine_type': current_type,
                'new_machine_type': machine_type,
            }

            if current_type == machine_type:
                msg = f"VM already uses machine type {machine_type}"
                self._log_debug(f"  {msg}")
                return OperationResult(
                    operation_name=self.name, success=True, message=msg,
                    rollback_data=rollback_data
                )

            if status != 'TERMINATED':
                msg = (
                    f"VM must be stopped to change its machine type "
                    f"(current state: {status})"
                )
                self._log_debug(msg)
                return OperationResult(
                    operation_name=self.name, success=False, message=msg,
                    error=msg
                )

            self._log_debug(f"  Changing machine type: {current_type} -> {machine_type}")
            start_time = time.time()
            if not self._set_machine_type(compute, vm_name, machine_type, timeout):
                op_error = self._last_operation_error or (
                    f"Timeout waiting for machine type change (>{timeout}s)"
                )
                return self._failure(op_error, vm_name)

            duration = time.time() - start_time
            return OperationResult(
                operation_name=self.name,
                success=True,
                message=(
                    f"Machine type changed {current_type} -> {machine_type} "
                    f"({duration:.0f}s)"
                ),
                rollback_data=rollback_data
            )

        except Exception as e:
            return self._failure(extract_error_message(e), vm_name)

    def rollback(self, rollback_data: dict,
                 tracking_label: Optional[str] = None) -> bool:
        """
        Set the original machine type back.

        The VM must be stopped; a running VM is left unchanged and the
        rollback reports failure.

        Args:
            rollback_data (dict): Data from `execute()`.
            tracking_label (str, optional): User-Agent for the rollback's
                API calls (see BaseOperation.rollback).

        Returns:
            bool: True if the VM has its original machine type, False
            otherwise.
        """
        vm_name = rollback_data.get('vm_name')
        previous_type = rollback_data.get('previous_machine_type')
        if not vm_name or not previous_type:
            self._log_debug("No machine type to restore")
            return True

        try:
            compute = self._rollback_client(tracking_label)
            vm = compute.instances().get(
                project=self.project, zone=self.zone, instance=vm_name
            ).execute()
            if machine_type_name(vm.get('machineType', '')) == previous_type:
                self._log_debug(f"VM already uses machine type {previous_type}")
                return True
            if vm.get('status') != 'TERMINATED':
                self._log_error(
                    f"Cannot restore machine type {previous_type} on {vm_name}: "
                    f"VM is {vm.get('status')}, it must be stopped"
                )
                return False
            if not self._set_machine_type(compute, vm_name, previous_type):
                self._log_error(
                    f"Failed to restore machine type {previous_type} on "
                    f"{vm_name}: {self._last_operation_error or 'timeout'}"
                )
                return False
            self._log_debug(f"Restored machine type {previous_type}")
            return True
        except Exception as e:
            self._log_error(
                f"Failed to restore machine type {previous_type} on {vm_name}: "
                f"{extract_error_message(e)}"
            )
            return False

    def _set_machine_type(self, compute, vm_name: str, machine_type: str,
                          timeout: int = 300) -> bool:
        operation = compute.instances().setMachineType(
            project=self.project,
            zone=self.zone,
            instance=vm_name,
            body={'machineType': f'zones/{self.zone}/machineTypes/{machine_type}'}
        ).execute()
        return self._wait_for_operation(operation, timeout)

    def _failure(self, error_msg: str, vm_name: str) -> OperationResult:
        suggestion = get_error_suggestion(error_msg, operation='set_machine_type')
        if suggestion:
            error_detail = suggestion.format(
                vm_name=vm_name, zone=self.zone, project=self.project
            )
        else:
            error_detail = f"Failed to change machine type: {error_msg}"
        self._log_debug(error_detail)
        return OperationResult(
            operation_name=self.name,
            success=False,
            message=f"Failed to change machine type: {error_msg}",
            error=error_detail
        )
