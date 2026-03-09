"""
Attach Disk operation.

Attaches a Compute Engine persistent disk to a VM instance. Rollback detaches
the disk using the provided rollback metadata.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, DISK_ATTACH_FAILED


class AttachDiskOperation(BaseOperation):
    """Operation that attaches a disk to a VM instance.

    On success, returns rollback metadata sufficient to detach the disk during
    rollback. Rollback only detaches the disk that was attached by this
    operation (identified by device name).
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Attach Disk"

    def execute(self, vm_name: str, disk_name: str, boot: bool = False,
                auto_delete: bool = False, read_only: bool = False, tracking_label: str = None) -> OperationResult:
        """
        Attach a persistent disk to a VM instance.

        Args:
            vm_name (str): Target VM instance name.
            disk_name (str): Name of the disk to attach.
            boot (bool): Whether to mark the disk as a boot device.
            auto_delete (bool): Whether to auto-delete the disk with the VM.
            read_only (bool): Attach in read-only mode when True; read-write otherwise.
            tracking_label (str): Tracking label for usage analytics.
                Format: '{operation_type}-{action_group}-{action_detail}'
                Example: 'rescue-disk-attach-rescue'

        Returns:
            OperationResult: Result containing success status, message, and
            `rollback_data` with `vm_name` and `device_name` for detaching.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name}: {disk_name} to {vm_name}")
        self._log_debug(f"  Boot: {boot}, AutoDelete: {auto_delete}, ReadOnly: {read_only}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            # Prepare attachment configuration
            attach_body = {
                'source': f'projects/{self.project}/zones/{self.zone}/disks/{disk_name}',
                'boot': boot,
                'autoDelete': auto_delete,
                'deviceName': disk_name,  # Use disk name as device name
                'mode': 'READ_ONLY' if read_only else 'READ_WRITE'
            }

            self._log_debug(f"Attaching with config: {attach_body}")

            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute
            # Attach the disk
            operation = compute.instances().attachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=attach_body
            ).execute()

            # Wait for operation to complete
            if not self._wait_for_operation(operation):
                error_detail = DISK_ATTACH_FAILED.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project,
                    disk_name=disk_name
                )
                self._log_error(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message="Timeout waiting for disk attach operation",
                    error=error_detail
                )

            self._log_debug("Disk attached")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk attached",
                rollback_data={
                    'vm_name': vm_name,
                    'device_name': disk_name
                }
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='attach_disk')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project,
                    disk_name=disk_name
                )
            else:
                error_detail = f"Failed to attach disk: {error_msg}"
            self._log_error(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to attach disk: {error_msg}",
                error=error_detail
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Detach the disk that was attached by this operation.

        Args:
            rollback_data (dict): Data from `execute()` including `vm_name` and
                `device_name` to identify the attached disk.

        Returns:
            bool: True if rollback succeeded; False if detachment failed.

        Raises:
            None
        """

        try:
            vm_name = rollback_data['vm_name']
            device_name = rollback_data['device_name']

            self._log_debug(f"Rolling back {self.name}: detaching disk")
            self._log_info(f"  Detaching disk...")

            operation = self.compute.instances().detachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                deviceName=device_name
            ).execute()

            # Wait for operation to complete
            self._wait_for_operation(operation)

            self._log_info(f"  [OK] Disk detached")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
