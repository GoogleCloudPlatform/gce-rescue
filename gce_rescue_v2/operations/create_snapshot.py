"""
Create Snapshot operation.

Creates a safety snapshot of a Compute Engine disk prior to rescue
operations. Rollback removes only snapshots created by this operation.
"""

import time
from operations.base import BaseOperation, OperationResult


class CreateSnapshotOperation(BaseOperation):
    """Operation that creates a safety snapshot for a disk.

    The snapshot acts as a restore point before entering rescue mode. The
    rollback phase deletes the snapshot only when it was created by this
    operation (pre-existing snapshots are preserved).
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Create Snapshot"

    def execute(self, disk_name: str, snapshot_name: str = None,
                description: str = None, timeout: int = 600, wait: bool = True) -> OperationResult:
        """
        Create a snapshot of the specified disk.

        In synchronous mode (`wait=True`), polls until the snapshot reaches
        status READY or a timeout occurs. In asynchronous mode (`wait=False`),
        starts the snapshot and returns immediately.

        Args:
            disk_name (str): Name of the source disk to snapshot.
            snapshot_name (str, optional): Custom snapshot name. If omitted,
                an auto-generated name is used.
            description (str, optional): Snapshot description text. Defaults
                to a safety message when not provided.
            timeout (int): Maximum seconds to wait in synchronous mode.
                Default is 600.
            wait (bool): Whether to wait for completion (synchronous). If
                False, runs in async mode and returns immediately. Default True.

        Returns:
            OperationResult: Result object containing success flag, message,
            and `rollback_data` with `snapshot_name`, `disk_name`, and
            creation metadata.

        Raises:
            None
        """

        # Auto-generate name if not provided
        if not snapshot_name:
            timestamp = int(time.time())
            snapshot_name = f"pre-rescue-{disk_name}-{timestamp}"

        # Add description
        if not description:
            description = f"Pre-rescue safety snapshot of {disk_name}"

        self._log_debug(f"Executing {self.name}: {snapshot_name}")
        self._log_debug(f"  Disk: {disk_name}")
        self._log_debug(f"  Description: {description}")

        try:
            # Create snapshot
            body = {
                'name': snapshot_name,
                'description': description
            }

            self._log_debug(f"Creating snapshot with config: {body}")

            self.compute.disks().createSnapshot(
                project=self.project,
                zone=self.zone,
                disk=disk_name,
                body=body
            ).execute()

            # If async mode, return immediately without waiting
            if not wait:
                self._log_debug(f"Snapshot creation started (async mode - not waiting)")
                return OperationResult(
                    operation_name=self.name,
                    success=True,
                    message=f"Snapshot started: {snapshot_name} (not waiting for completion)",
                    rollback_data={
                        'snapshot_name': snapshot_name,
                        'disk_name': disk_name,
                        'created_by_operation': True,
                        'async_mode': True  # Mark as async for reference
                    }
                )

            # Wait for snapshot to complete (sync mode)
            self._log_debug("Waiting for snapshot creation...")
            start_time = time.time()

            while True:
                # Check timeout
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    self._log_error(f"Snapshot creation timeout after {timeout}s")
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Snapshot creation timeout after {timeout}s",
                        error="timeout"
                    )

                # Get snapshot status
                try:
                    snapshot = self.compute.snapshots().get(
                        project=self.project,
                        snapshot=snapshot_name
                    ).execute()

                    status = snapshot.get('status', 'UNKNOWN')
                    self._log_debug(f"Current status: {status}, Target: READY")

                    if status == 'READY':
                        duration = time.time() - start_time
                        self._log_debug(f"Snapshot created in {duration:.2f}s")

                        return OperationResult(
                            operation_name=self.name,
                            success=True,
                            message=f"Snapshot created: {snapshot_name} ({duration:.0f}s)",
                            rollback_data={
                                'snapshot_name': snapshot_name,
                                'disk_name': disk_name,
                                'created_by_operation': True  # We created it, so we can delete it
                            }
                        )
                    elif status == 'FAILED':
                        self._log_error("Snapshot creation failed")
                        return OperationResult(
                            operation_name=self.name,
                            success=False,
                            message="Snapshot creation failed",
                            error="snapshot_failed"
                        )

                except Exception as e:
                    # Snapshot might not exist yet, keep waiting
                    self._log_debug(f"Waiting for snapshot to appear... ({elapsed:.0f}s)")

                time.sleep(5)

        except Exception as e:
            self._log_error(f"Failed to create snapshot: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to create snapshot",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Delete the snapshot created by this operation.

        Only snapshots marked as created by this operation are deleted.
        Pre-existing snapshots (not created here) are preserved.

        Args:
            rollback_data (dict): Data from `execute`, including
                `snapshot_name` and `created_by_operation`.

        Returns:
            bool: True if rollback succeeded or was not required.

        Raises:
            None
        """

        if not rollback_data.get('created_by_operation'):
            # Snapshot existed before operation, don't delete it
            self._log_debug("Snapshot existed before operation, preserving it")
            return True

        snapshot_name = rollback_data.get('snapshot_name')

        if not snapshot_name:
            self._log_debug("No snapshot to rollback")
            return True

        self._log_debug(f"Rolling back {self.name}: deleting {snapshot_name}")

        try:
            self.compute.snapshots().delete(
                project=self.project,
                snapshot=snapshot_name
            ).execute()

            # Wait a bit for deletion
            time.sleep(2)

            self._log_info(f"Cleaned up snapshot: {snapshot_name}")
            return True

        except Exception as e:
            self._log_error(f"Failed to delete snapshot: {str(e)}")
            self._log_error(f"You can delete it manually: gcloud compute snapshots delete {snapshot_name}")
            # Don't fail rollback if snapshot delete fails
            # User can delete manually later
            return True
