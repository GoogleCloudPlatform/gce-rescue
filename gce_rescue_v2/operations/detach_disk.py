"""
GCE Rescue - Detach Disk Operation

Detaches a disk from a VM.
Rollback: Re-attaches the disk.
"""

import time
from operations.base import BaseOperation, OperationResult


class DetachDiskOperation(BaseOperation):
    """
    Detaches a disk from a VM.

    Rollback: Re-attaches the disk with original settings.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Detach Disk"

    def execute(self, vm_name: str, device_name: str) -> OperationResult:
        """
        Detach a disk from VM.

        Args:
            vm_name: Name of the VM
            device_name: Device name of disk to detach

        Returns:
            OperationResult with success status and rollback data
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
            self.compute.instances().detachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                deviceName=device_name
            ).execute()

            # Wait a bit for detachment
            time.sleep(2)

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
            self._log_error(f"Failed to detach disk: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to detach disk",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Re-attach the disk.

        Args:
            rollback_data: Data from execute()

        Returns:
            True if rollback successful
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

            self.compute.instances().attachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=attach_body
            ).execute()

            time.sleep(2)

            self._log_info(f"  [OK] Disk re-attached")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
