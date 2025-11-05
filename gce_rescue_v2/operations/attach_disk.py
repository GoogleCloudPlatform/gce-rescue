"""
GCE Rescue - Attach Disk Operation

Attaches a disk to a VM.
Rollback: Detaches the disk.
"""

import time
from operations.base import BaseOperation, OperationResult


class AttachDiskOperation(BaseOperation):
    """
    Attaches a disk to a VM.

    Rollback: Detaches the disk.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Attach Disk"

    def execute(self, vm_name: str, disk_name: str, boot: bool = False,
                auto_delete: bool = False, read_only: bool = False) -> OperationResult:
        """
        Attach a disk to VM.

        Args:
            vm_name: Name of the VM
            disk_name: Name of the disk to attach
            boot: Whether this is the boot disk
            auto_delete: Whether to auto-delete disk when VM deleted
            read_only: Whether to attach as read-only

        Returns:
            OperationResult with success status and rollback data
        """

        self._log_debug(f"Executing {self.name}: {disk_name} to {vm_name}")
        self._log_debug(f"  Boot: {boot}, AutoDelete: {auto_delete}, ReadOnly: {read_only}")

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

            # Attach the disk
            self.compute.instances().attachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=attach_body
            ).execute()

            # Wait a bit for attachment
            time.sleep(3)

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
            self._log_error(f"Failed to attach disk: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to attach disk",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Detach the disk.

        Args:
            rollback_data: Data from execute()

        Returns:
            True if rollback successful
        """

        try:
            vm_name = rollback_data['vm_name']
            device_name = rollback_data['device_name']

            self._log_debug(f"Rolling back {self.name}: detaching disk")
            self._log_info(f"  Detaching disk...")

            self.compute.instances().detachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                deviceName=device_name
            ).execute()

            time.sleep(2)

            self._log_info(f"  [OK] Disk detached")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
