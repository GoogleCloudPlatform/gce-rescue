"""
GCE Rescue - Set Metadata Operation

Sets metadata on a VM.
Rollback: Restores original metadata.
"""

import time
from operations.base import BaseOperation, OperationResult


class SetMetadataOperation(BaseOperation):
    """
    Sets metadata on a VM.

    Rollback: Restores original metadata.
    """

    @property
    def name(self) -> str:
        """Display name for this operation."""
        return "Set Metadata"

    def execute(self, vm_name: str, metadata_items: list) -> OperationResult:
        """
        Set metadata on VM.

        Args:
            vm_name: Name of the VM
            metadata_items: List of metadata items [{'key': 'k', 'value': 'v'}, ...]

        Returns:
            OperationResult with success status and rollback data
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")
        self._log_debug(f"  Setting {len(metadata_items)} metadata items")

        try:
            # Get current metadata for rollback
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            original_metadata = vm.get('metadata', {})
            fingerprint = original_metadata.get('fingerprint')

            self._log_debug(f"Original metadata has {len(original_metadata.get('items', []))} items")

            # Set new metadata
            new_metadata = {
                'fingerprint': fingerprint,
                'items': metadata_items
            }

            self.compute.instances().setMetadata(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=new_metadata
            ).execute()

            time.sleep(2)

            self._log_debug("Metadata set")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Metadata set ({len(metadata_items)} items)",
                rollback_data={
                    'vm_name': vm_name,
                    'original_metadata': original_metadata
                }
            )

        except Exception as e:
            self._log_error(f"Failed to set metadata: {str(e)}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message="Failed to set metadata",
                error=str(e)
            )

    def rollback(self, rollback_data: dict) -> bool:
        """
        Rollback: Restore original metadata.

        Args:
            rollback_data: Data from execute()

        Returns:
            True if rollback successful
        """

        try:
            vm_name = rollback_data['vm_name']
            original_metadata = rollback_data['original_metadata']

            self._log_debug(f"Rolling back {self.name}: restoring metadata")
            self._log_info(f"  Restoring original metadata...")

            # Get current fingerprint (needed for setMetadata)
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            current_fingerprint = vm.get('metadata', {}).get('fingerprint')

            # Restore original metadata with current fingerprint
            restore_metadata = {
                'fingerprint': current_fingerprint,
                'items': original_metadata.get('items', [])
            }

            self.compute.instances().setMetadata(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=restore_metadata
            ).execute()

            time.sleep(2)

            self._log_info(f"  [OK] Metadata restored")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
