"""
Set Metadata operation.

Manages instance metadata for rescue operations. Preserves existing metadata
by backing up conflicting keys with a prefix, and supports full restoration.
"""

import time
from googleapiclient import discovery
import googleapiclient.http
import google_auth_httplib2
import httplib2
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, METADATA_SET_FAILED
from ..core.config import VERSION


# Prefix used to backup original metadata keys that conflict with rescue keys
RESCUE_BACKUP_PREFIX = 'rescue-backup-'

# Keys that rescue mode needs to set (may conflict with existing metadata)
RESCUE_CONFLICT_KEYS = [
    'startup-script',
    'windows-startup-script-ps1',
]

# All rescue-related keys (for cleanup during restore)
RESCUE_METADATA_KEYS = [
    'rescue-mode',
    'rescue-original-disk',
    'rescue-os-type',
    'startup-script',
    'windows-startup-script-ps1',
]


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

    def execute(self, vm_name: str, metadata_items: list, preserve_existing: bool = True, operation_type: str = None) -> OperationResult:
        """
        Set metadata on the specified VM instance, preserving existing metadata.

        When preserve_existing=True (default):
        - Existing metadata keys are preserved
        - Conflicting keys (e.g., startup-script) are backed up with prefix
        - Backed up keys can be restored later using restore_backup_keys()

        Args:
            vm_name (str): Name of the VM instance.
            metadata_items (list): List of metadata items as dictionaries of
                form {'key': str, 'value': str}.
            preserve_existing (bool): If True, merge with existing metadata
                and backup conflicting keys. Default True.
            operation_type (str): Operation type for tracking ('rescue' or 'restore').
                Sets unique User-Agent for analytics.

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `original_metadata` for restoration.
        """

        self._log_debug(f"Executing {self.name} for {vm_name}")
        self._log_debug(f"  Setting {len(metadata_items)} metadata items (preserve_existing={preserve_existing})")
        if operation_type:
            self._log_debug(f"  Operation tracking: {operation_type}")

        try:
            # Get current metadata for rollback
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            original_metadata = vm.get('metadata', {})
            fingerprint = original_metadata.get('fingerprint')
            original_items = original_metadata.get('items', [])

            self._log_debug(f"Original metadata has {len(original_items)} items")

            if preserve_existing:
                # Build final metadata: existing + backup conflicting + new items
                final_items = self._merge_with_backup(original_items, metadata_items)
            else:
                final_items = metadata_items

            # Set new metadata
            new_metadata = {
                'fingerprint': fingerprint,
                'items': final_items
            }

            # Use custom compute client with unique User-Agent if operation_type provided (for tracking)
            if operation_type:
                compute = self._create_tracked_client(operation_type)
            else:
                compute = self.compute

            operation = compute.instances().setMetadata(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=new_metadata
            ).execute()

            # Wait for operation to complete
            if not self._wait_for_operation(operation):
                error_detail = METADATA_SET_FAILED.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project
                )
                self._log_error(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message="Timeout waiting for metadata operation",
                    error=error_detail
                )

            self._log_debug("Metadata set")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Metadata set ({len(final_items)} items)",
                rollback_data={
                    'vm_name': vm_name,
                    'original_metadata': original_metadata
                }
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='set_metadata')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project
                )
            else:
                error_detail = f"Failed to set metadata: {error_msg}"
            self._log_error(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to set metadata: {error_msg}",
                error=error_detail
            )

    def _create_tracked_client(self, operation_type: str):
        """
        Create a compute client with unique User-Agent for usage tracking.

        Args:
            operation_type: Operation type ('rescue' or 'restore')

        Returns:
            Compute API client with custom User-Agent header
        """
        # Get credentials from the base compute client
        credentials = self.compute._http.credentials

        # Build unique User-Agent for tracking
        user_agent = f'gce-rescue-{VERSION}-{operation_type}'

        def _request_builder(http, *args, **kwargs):
            """Inject custom User-Agent header."""
            headers = kwargs.setdefault('headers', {})
            headers['user-agent'] = user_agent
            auth_http = google_auth_httplib2.AuthorizedHttp(
                credentials,
                http=httplib2.Http()
            )
            return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

        # Create compute client with custom request builder
        return discovery.build(
            'compute',
            'v1',
            credentials=credentials,
            cache_discovery=False,
            requestBuilder=_request_builder
        )

    def _merge_with_backup(self, original_items: list, new_items: list) -> list:
        """
        Merge new metadata items with existing ones, backing up conflicts.

        For keys that exist in both original and new items:
        - If it's a conflict key (like startup-script), backup original with prefix
        - Then set the new value

        Args:
            original_items: Existing metadata items from VM
            new_items: New items to set (rescue metadata)

        Returns:
            List of merged metadata items with backups
        """
        # Get keys we're about to set
        new_keys = {item['key'] for item in new_items}

        # Build result starting with original items
        result = {}

        for item in original_items:
            key = item['key']
            value = item['value']

            # Skip if this is already a backup key (don't double-backup)
            if key.startswith(RESCUE_BACKUP_PREFIX):
                result[key] = value
                continue

            # Check if this key will be overwritten by new items
            if key in new_keys and key in RESCUE_CONFLICT_KEYS:
                # Backup this key with prefix
                backup_key = f"{RESCUE_BACKUP_PREFIX}{key}"
                result[backup_key] = value
                self._log_debug(f"  Backing up '{key}' as '{backup_key}'")
            elif key not in new_keys:
                # Keep original key as-is (not being overwritten)
                result[key] = value

        # Add all new items
        for item in new_items:
            result[item['key']] = item['value']

        # Convert back to list format
        return [{'key': k, 'value': v} for k, v in result.items()]

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

            operation = self.compute.instances().setMetadata(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=restore_metadata
            ).execute()

            # Wait for operation to complete
            self._wait_for_operation(operation)

            self._log_info(f"  [OK] Metadata restored")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
