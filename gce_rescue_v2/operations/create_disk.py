"""
Create Disk operation.

Creates a new Compute Engine persistent disk from an image. Rollback deletes
the disk that was created by this operation.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, DISK_CREATE_FAILED


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
                timeout: int = 300, tracking_label: str = None) -> OperationResult:
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
            tracking_label (str): Tracking label for usage analytics.
                Format: '{operation_type}-{action_group}-{action_detail}'
                Example: 'rescue-disk-create-rescue'

        Returns:
            OperationResult: Result containing success flag, message, and
            `rollback_data` with `disk_name` for deletion.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name}: {disk_name}")
        self._log_debug(f"  Size: {size_gb}GB, Type: {disk_type}")
        self._log_debug(f"  Image: {source_image}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            # Prepare disk configuration
            disk_body = {
                'name': disk_name,
                'sizeGb': str(size_gb),
                'type': f'projects/{self.project}/zones/{self.zone}/diskTypes/{disk_type}',
                'sourceImage': source_image
            }

            self._log_debug(f"Creating disk with config: {disk_body}")

            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute
            start_time = time.time()
            # Create the disk (returns an async zone operation)
            operation = compute.disks().insert(
                project=self.project,
                zone=self.zone,
                body=disk_body
            ).execute()

            # Wait on the OPERATION (not just the disk's status). Image/org-policy
            # restrictions (e.g. constraints/compute.trustedImageProjects) surface
            # only in the async operation result; polling the disk status would
            # just time out and hide the real cause. Waiting on the operation
            # fails fast and exposes the actual error message.
            self._log_debug("Waiting for disk-create operation...")
            if not self._wait_for_operation(operation, timeout):
                op_error = self._last_operation_error
                if op_error:
                    suggestion = get_error_suggestion(op_error, operation='create_disk')
                    if suggestion:
                        error_detail = suggestion.format(
                            vm_name=None, zone=self.zone,
                            project=self.project, disk_name=disk_name
                        )
                    else:
                        error_detail = f"Failed to create disk: {op_error}"
                    self._log_debug(error_detail)
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Failed to create disk: {op_error}",
                        error=error_detail
                    )
                error_detail = DISK_CREATE_FAILED.format(
                    vm_name=None, zone=self.zone,
                    project=self.project, disk_name=disk_name
                )
                self._log_debug(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"Timeout waiting for disk creation (>{timeout}s)",
                    error=error_detail
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
            suggestion = get_error_suggestion(error_msg, operation='create_disk')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=None,
                    zone=self.zone,
                    project=self.project,
                    disk_name=disk_name
                )
            else:
                error_detail = f"Failed to create disk: {error_msg}"
            self._log_debug(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to create disk: {error_msg}",
                error=error_detail
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
