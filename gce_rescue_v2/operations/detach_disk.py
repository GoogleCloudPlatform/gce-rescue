"""
Detach Disk operation.

Detaches a Compute Engine persistent disk from a VM instance. Rollback re-
attaches the disk using the captured original configuration.
"""

import time
from googleapiclient import discovery
import googleapiclient.http
import google_auth_httplib2
import httplib2
from .base import BaseOperation, OperationResult, extract_error_message
from ..core.error_messages import get_error_suggestion, DISK_DETACH_FAILED
from ..core.config import VERSION


class DetachDiskOperation(BaseOperation):
    """Operation that detaches a disk from a VM.

    The operation captures the current attachment configuration to enable
    accurate re-attachment during rollback.
    """

    @property
    def name(self) -> str:
        """Display name for this operation.

        Returns:
            str: Human-friendly operation name.
        """
        return "Detach Disk"

    def execute(self, vm_name: str, device_name: str, tracking_label: str = None) -> OperationResult:
        """
        Detach a disk from the specified VM instance.

        Args:
            vm_name (str): Name of the target VM instance.
            device_name (str): Device name of the disk to detach (e.g.,
                the `deviceName` field from instance.disks[]).
            tracking_label (str): Tracking label for usage analytics.
                Format: '{operation_type}-{action_group}-{action_detail}'
                Example: 'restore-disk-detach-rescue'

        Returns:
            OperationResult: Result including `rollback_data` with `vm_name`
            and `disk_info` for re-attachment.

        Raises:
            None
        """

        self._log_debug(f"Executing {self.name}: {device_name} from {vm_name}")
        if tracking_label:
            self._log_debug(f"  Operation tracking: {tracking_label}")

        try:
            # Get current disk configuration for rollback
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=vm_name
            ).execute()

            # Find the disk being detached
            disk_info = None
            for disk in vm.get('disks', []):
                if disk['deviceName'] == device_name:
                    disk_info = {
                        'source': disk['source'],
                        'boot': disk.get('boot', False),
                        'autoDelete': disk.get('autoDelete', False),
                        'deviceName': disk['deviceName'],
                        'mode': disk.get('mode', 'READ_WRITE')
                    }
                    break

            if not disk_info:
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message=f"Disk {device_name} not found on VM"
                )

            self._log_debug(f"Detaching disk: {disk_info}")

            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute
            # Detach the disk
            operation = compute.instances().detachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                deviceName=device_name
            ).execute()

            # Wait for operation to complete
            if not self._wait_for_operation(operation):
                error_detail = DISK_DETACH_FAILED.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project,
                    disk_name=device_name
                )
                self._log_error(error_detail)
                return OperationResult(
                    operation_name=self.name,
                    success=False,
                    message="Timeout waiting for disk detach operation",
                    error=error_detail
                )

            self._log_debug("Disk detached")

            return OperationResult(
                operation_name=self.name,
                success=True,
                message=f"Disk detached",
                rollback_data={
                    'vm_name': vm_name,
                    'disk_info': disk_info
                }
            )

        except Exception as e:
            error_msg = extract_error_message(e)
            suggestion = get_error_suggestion(error_msg, operation='detach_disk')
            if suggestion:
                error_detail = suggestion.format(
                    vm_name=vm_name,
                    zone=self.zone,
                    project=self.project,
                    disk_name=device_name
                )
            else:
                error_detail = f"Failed to detach disk: {error_msg}"
            self._log_error(error_detail)
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Failed to detach disk: {error_msg}",
                error=error_detail
            )

    def _create_tracked_client(self, tracking_label: str):
        """
        Create a compute client with unique User-Agent for usage tracking.

        Args:
            tracking_label: Tracking label in format '{operation_type}-{action_group}-{action_detail}'
                Example: 'restore-disk-detach-rescue'

        Returns:
            Compute API client with custom User-Agent header
        """
        # Get credentials from the base compute client
        credentials = self.compute._http.credentials

        # Build unique User-Agent for tracking
        # Format: gce-rescue-{VERSION}-{tracking_label}
        user_agent = f'gce-rescue-{VERSION}-{tracking_label}'

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

    def rollback(self, rollback_data: dict) -> bool:
        """
        Re-attach the disk that was detached by this operation.

        Args:
            rollback_data (dict): Data from `execute()` including `vm_name`
                and `disk_info` (source, boot, autoDelete, deviceName, mode).

        Returns:
            bool: True if rollback succeeded; False if re-attachment failed.

        Raises:
            None
        """

        try:
            vm_name = rollback_data['vm_name']
            disk_info = rollback_data['disk_info']

            self._log_debug(f"Rolling back {self.name}: re-attaching disk")
            self._log_info(f"  Re-attaching disk...")

            # Re-attach the disk
            attach_body = {
                'source': disk_info['source'],
                'boot': disk_info['boot'],
                'autoDelete': disk_info['autoDelete'],
                'deviceName': disk_info['deviceName'],
                'mode': disk_info['mode']
            }

            operation = self.compute.instances().attachDisk(
                project=self.project,
                zone=self.zone,
                instance=vm_name,
                body=attach_body
            ).execute()

            # Wait for operation to complete
            self._wait_for_operation(operation)

            self._log_info(f"  [OK] Disk re-attached")
            return True

        except Exception as e:
            self._log_error(f"Rollback failed: {str(e)}")
            return False
