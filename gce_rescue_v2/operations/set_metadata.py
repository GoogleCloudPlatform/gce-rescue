"""
Set Metadata operation.

Replaces instance metadata with the provided items, preserving the original
metadata for rollback. Rollback restores the prior metadata set.
"""

import time
from operations.base import BaseOperation, OperationResult


class SetMetadataOperation(BaseOperation):
    """Operation that sets instance metadata.

    Uses the current metadata fingerprint to update the VM's metadata. On
    success, stores the previous metadata to enable rollback.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Set Metadata"

    def execute(self, vm_name: str, metadata_items: list) -> OperationResult:
        """
        Replace the metadata of the specified VM instance.

        Args:
            vm_name (str): Name of the VM instance.
            metadata_items (list): List of metadata items as dictionaries of
                form {'key': str, 'value': str}.

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `original_metadata` for restoration.

        Raises:
            None
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
        Restore the original metadata captured during execution.

        Args:
            rollback_data (dict): Data from `execute()` including `vm_name`
                and `original_metadata`.

        Returns:
            bool: True if rollback succeeded; False otherwise.

        Raises:
            None
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
