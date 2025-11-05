"""
GCE Rescue - Create Snapshot Operation

Creates a snapshot of a disk for safety.
Rollback: Delete the snapshot (if created during this operation).
"""

import time
from operations.base import BaseOperation, OperationResult


class CreateSnapshotOperation(BaseOperation):
    """
    Creates a snapshot of a disk.

    This is the ultimate safety net - if rollback fails, you can restore
    from this snapshot.

    Rollback: Delete the snapshot we created (only if created by this operation)
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Create Snapshot"

    def execute(self, disk_name: str, snapshot_name: str = None,
                description: str = None, timeout: int = 600) -> OperationResult:
        """
        Create a snapshot of a disk.

        Args:
            disk_name: Name of disk to snapshot
            snapshot_name: Custom name (auto-generated if None)
            description: Snapshot description
            timeout: Timeout in seconds (default: 600)

        Returns:
            OperationResult with snapshot name in rollback_data
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

            # Wait for snapshot to complete
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
        Rollback: Delete the snapshot we created.

        Note: Only delete if we created it during this operation.
        User-created snapshots are preserved.

        Args:
            rollback_data: Data from execute (contains snapshot_name)

        Returns:
            True if rollback succeeded (or not needed)
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
