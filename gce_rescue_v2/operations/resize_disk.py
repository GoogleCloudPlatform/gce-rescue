"""
Resize Disk operation.

Increases the size of an existing Compute Engine persistent disk.
Note: Compute Engine persistent disks can only be increased in size and
cannot be shrunk during rollback; the pre-rescue snapshot preserves the
original disk state.
"""

import time
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion


class ResizeDiskOperation(BaseOperation):
    """Operation that increases the size of a persistent disk."""

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Resize Disk"

    def execute(self, disk_name: str, add_gb: int = 5,
                new_size_gb: int = None,
                expected_previous_size_gb: int = None,
                timeout: int = 300,
                tracking_label: str = None) -> OperationResult:
        """
        Resize a persistent disk by adding `add_gb` GiB (or to `new_size_gb`).

        Args:
            disk_name (str): Name of the disk to resize.
            add_gb (int): Number of GiB to add to the current disk size.
            new_size_gb (int, optional): Explicit target size in GiB. If omitted,
                computed as `current_size_gb + add_gb`.
            expected_previous_size_gb (int, optional): Original disk size from a
                checkpoint. If the disk is already larger than this size, the
                resize already succeeded before interruption and is skipped.
            timeout (int): Maximum seconds to wait for the resize operation.
            tracking_label (str, optional): Tracking User-Agent string.

        Returns:
            OperationResult: Result containing `previous_size_gb` and
            `new_size_gb` in `rollback_data`.
        """
        self._log_debug(f"Executing {self.name}: {disk_name}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            compute = (
                self._create_tracked_client(tracking_label)
                if tracking_label else self.compute
            )
            disk_info = compute.disks().get(
                project=self.project,
                zone=self.zone,
                disk=disk_name
            ).execute()
            current_size_gb = int(disk_info.get('sizeGb', 0))

            if (expected_previous_size_gb is not None
                    and current_size_gb > int(expected_previous_size_gb)):
                msg = (
                    f"Skipping resize: {disk_name} is already "
                    f"{current_size_gb}GB (was {expected_previous_size_gb}GB "
                    f"at checkpoint start)"
                )
                self._log_debug(f"  {msg}")
                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message=msg,
                    rollback_data={
                        'disk_name': disk_name,
                        'previous_size_gb': int(expected_previous_size_gb),
                        'new_size_gb': current_size_gb,
                    }
                )

            target_size_gb = (
                int(new_size_gb)
                if new_size_gb is not None
                else current_size_gb + int(add_gb)
            )

            if target_size_gb <= current_size_gb:
                msg = (
                    f"Target disk size ({target_size_gb}GB) must be greater "
                    f"than current size ({current_size_gb}GB)"
                )
                self._log_debug(msg)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=msg,
                    error=msg
                )

            self._log_debug(
                f"  Resizing {disk_name}: {current_size_gb}GB -> {target_size_gb}GB"
            )
            start_time = time.time()
            operation = compute.disks().resize(
                project=self.project,
                zone=self.zone,
                disk=disk_name,
                body={'sizeGb': str(target_size_gb)}
            ).execute()

            self._log_debug("Waiting for disk-resize operation...")
            if not self._wait_for_operation(operation, timeout):
                op_error = self._last_operation_error or (
                    f"Timeout waiting for disk resize (>{timeout}s)"
                )
                suggestion = get_error_suggestion(op_error, operation='resize_disk')
                if suggestion:
                    error_detail = suggestion.format(
                        vm_name=None, zone=self.zone,
                        project=self.project, disk_name=disk_name
                    )
                else:
                    error_detail = f"Failed to resize disk: {op_error}"
                self._log_debug(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"Failed to resize disk: {op_error}",
                    error=error_detail
                )

            duration = time.time() - start_time
            self._log_debug(
                f"Disk resized from {current_size_gb}GB to {target_size_gb}GB "
                f"in {duration:.2f}s"
            )
            return OperationResult(
                operation_name=self.name,
                success=True,
                message=(
                    f"Disk resized {current_size_gb}GB -> {target_size_gb}GB "
                    f"({duration:.0f}s)"
                ),
                rollback_data={
                    'disk_name': disk_name,
                    'previous_size_gb': current_size_gb,
                    'new_size_gb': target_size_gb,
                }
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='resize_disk')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=None, zone=self.zone,
                    project=self.project, disk_name=disk_name
                )
            else:
                error_detail = f"Failed to resize disk: {error_msg}"
            self._log_debug(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to resize disk: {error_msg}",
                error=error_detail
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        No-op rollback for disk resize (GCP disks cannot be shrunk).

        Returns True so rollback of remaining rescue steps can proceed.
        """
        disk_name = rollback_data.get('disk_name', 'unknown')
        prev_gb = rollback_data.get('previous_size_gb')
        new_gb = rollback_data.get('new_size_gb')
        self._log_debug(
            f"Skipping shrink for {disk_name} ({prev_gb}GB -> {new_gb}GB): "
            "Compute Engine persistent disks cannot be reduced in size"
        )
        return True
