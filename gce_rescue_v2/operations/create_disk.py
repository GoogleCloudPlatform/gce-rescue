"""
GCE Rescue - Create Disk Operation

Creates a new disk from an image.
Rollback: Deletes the disk.
"""

import time
from operations.base import BaseOperation, OperationResult


class CreateDiskOperation(BaseOperation):
    """
    Creates a new disk from an image.

    Rollback: Deletes the created disk.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Create Disk"

    def execute(self, disk_name: str, size_gb: int = 10,
                disk_type: str = 'pd-standard',
                source_image: str = 'projects/debian-cloud/global/images/family/debian-11',
                timeout: int = 300) -> OperationResult:
        """
        Create a new disk.

        Args:
            disk_name: Name for the new disk
            size_gb: Size in GB
            disk_type: Type of disk (pd-standard, pd-ssd, pd-balanced)
            source_image: Source image to use
            timeout: Maximum seconds to wait

        Returns:
            OperationResult with success status and rollback data
        """

        self._log_debug(f"Executing {self.name}: {disk_name}")
        self._log_debug(f"  Size: {size_gb}GB, Type: {disk_type}")
        self._log_debug(f"  Image: {source_image}")

        try:
            # Prepare disk configuration
            disk_body = {
                'name': disk_name,
                'sizeGb': str(size_gb),
                'type': f'projects/{self.project}/zones/{self.zone}/diskTypes/{disk_type}',
                'sourceImage': source_image
            }

            self._log_debug(f"Creating disk with config: {disk_body}")

            # Create the disk
            self.compute.disks().insert(
                project=self.project,
                zone=self.zone,
                body=disk_body
            ).execute()

            # Wait for disk creation
            self._log_debug("Waiting for disk creation...")
            start_time = time.time()

            def get_status():
                try:
                    disk = self.compute.disks().get(
                        project=self.project,
                        zone=self.zone,
                        disk=disk_name
                    ).execute()
                    return disk['status']
                except:
                    return 'CREATING'

            if not self._wait_for_status(get_status, 'READY', timeout):
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"Timeout waiting for disk creation (>{timeout}s)"
                )

            duration = time.time() - start_time
            self._log_debug(f"Disk created in {duration:.2f}s")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk created ({duration:.0f}s)",
                rollback_data={
                    'disk_name': disk_name
                }
            )

        except Exception as e:
            self._log_error(f"Failed to create disk: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to create disk",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Delete the created disk.

        Args:
            rollback_data: Data from execute()

        Returns:
            True if rollback successful
        """

        try:
            disk_name = rollback_data['disk_name']

            self._log_debug(f"Rolling back {self.name}: deleting {disk_name}")
            self._log_info(f"  Deleting disk {disk_name}...")

            self.compute.disks().delete(
                project=self.project,
                zone=self.zone,
                disk=disk_name
            ).execute()

            # Wait a bit for deletion to complete
            time.sleep(3)

            self._log_info(f"  [OK] Disk deleted")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
