"""
Detach Disk operation.

Detaches a Compute Engine persistent disk from a VM instance. Rollback re-
attaches the disk using the captured original configuration.
"""

import time
from operations.base import BaseOperation, OperationResult, extract_error_message


class DetachDiskOperation(BaseOperation):
    """Operation that detaches a disk from a VM.

    The operation captures the current attachment configuration to enable
    accurate re-attachment during rollback.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Detach Disk"

    def execute(self, vm_name: str, device_name: str) -> OperationResult:
        """
        Detach a disk from the specified VM instance.

        Args:
            vm_name (str): Name of the target VM instance.
            device_name (str): Device name of the disk to detach (e.g.,
                the `deviceName` field from instance.disks[]).

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `disk_info` for re-attachment.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name}: {device_name} from {vm_name}")

        try:
            # Get current disk configuration for rollback
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            # Find the disk being detached
            disk_info = None
            for disk in vm.get('disks', []):
                if disk['deviceName'] == device_name:
                    disk_info = {
                        'source': disk['source'],
                        'boot': disk.get('boot', False),
                        'autoDelete': disk.get('autoDelete', False),
                        'deviceName': disk['deviceName'],
                        'mode': disk.get('mode', 'READ_WRITE')
                    }
                    break

            if not disk_info:
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"Disk {device_name} not found on VM"
                )

            self._log_debug(f"Detaching disk: {disk_info}")

            # Detach the disk
            operation = self.compute.instances().detachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                deviceName=device_name
            ).execute()

            # Wait for operation to complete
            if not self._wait_for_operation(operation):
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message="Timeout waiting for disk detach operation"
                )

            self._log_debug("Disk detached")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk detached",
                rollback_data={
                    'vm_name': vm_name,
                    'disk_info': disk_info
                }
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            self._log_error(f"Failed to detach disk: {error_msg}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to detach disk: {error_msg}",
                error=error_msg
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Re-attach the disk that was detached by this operation.

        Args:
            rollback_data (dict): Data from `execute()` including `vm_name`
                and `disk_info` (source, boot, autoDelete, deviceName, mode).

        Returns:
            bool: True if rollback succeeded; False if re-attachment failed.

        Raises:
            None
        """

        try:
            vm_name = rollback_data['vm_name']
            disk_info = rollback_data['disk_info']

            self._log_debug(f"Rolling back {self.name}: re-attaching disk")
            self._log_info(f"  Re-attaching disk...")

            # Re-attach the disk
            attach_body = {
                'source': disk_info['source'],
                'boot': disk_info['boot'],
                'autoDelete': disk_info['autoDelete'],
                'deviceName': disk_info['deviceName'],
                'mode': disk_info['mode']
            }

            operation = self.compute.instances().attachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=attach_body
            ).execute()

            # Wait for operation to complete
            self._wait_for_operation(operation)

            self._log_info(f"  [OK] Disk re-attached")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
