"""
Create Disk operation.

Creates a new Compute Engine persistent disk from an image. Rollback deletes
the disk that was created by this operation.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message


class CreateDiskOperation(BaseOperation):
    """Operation that creates a new disk from an image.

    The operation provisions a disk with the provided size, type, and source
    image. On success, rollback metadata is returned to allow deletion of the
    created disk during rollback.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Create Disk"

    def execute(self, disk_name: str, size_gb: int = 10,
                disk_type: str = 'pd-standard',
                source_image: str = 'projects/debian-cloud/global/images/family/debian-12',
                timeout: int = 300) -> OperationResult:
        """
        Create a new persistent disk from the specified image.

        Args:
            disk_name (str): Name for the new disk.
            size_gb (int): Disk size in GiB.
            disk_type (str): Disk type resource name (e.g., `pd-standard`,
                `pd-ssd`, `pd-balanced`).
            source_image (str): Full image or image family resource to clone
                (e.g., `projects/debian-cloud/global/images/family/debian-12`).
            timeout (int): Maximum seconds to wait for the disk to reach
                status READY.

        Returns:
            OperationResult: Result containing success flag, message, and
            `rollback_data` with `disk_name` for deletion.

        Raises:
            None
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
            error_msg = extract_error_message(e)
            self._log_error(f"Failed to create disk: {error_msg}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to create disk: {error_msg}",
                error=error_msg
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Delete the disk created by this operation.

        Args:
            rollback_data (dict): Data from `execute()` containing `disk_name`.

        Returns:
            bool: True if rollback succeeded; False if delete failed.

        Raises:
            None
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
