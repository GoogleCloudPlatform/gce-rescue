"""Diagnose VM boot issues by analyzing serial console output.

This operation is read-only and does not modify the VM in any way.
"""

from typing import Optional
import logging

from googleapiclient.errors import HttpError

from ..core.boot_patterns import analyze_serial_output, DiagnosisResult
from .base import BaseOperation, OperationResult, extract_error_message

logger = logging.getLogger(__name__)


class DiagnoseOperation(BaseOperation):
    """Diagnose VM boot issues by analyzing serial console output."""

    @property
    def name(self) -> str:
        """Return the operation name."""
        return "Diagnose VM"

    def execute(self, vm_name: str) -> OperationResult:
        """Execute diagnosis by fetching and analyzing serial console output.

        Args:
            vm_name: Name of the VM to diagnose

        Returns:
            OperationResult with diagnosis data in rollback_data field
        """
        try:
            self._log_info(f"Starting diagnosis for VM: {vm_name}")

            # Get VM status (non-fatal if 403 - user may only have
            # serial console read permission on this project)
            vm_status = 'UNKNOWN'
            try:
                self._log_debug("Fetching VM instance details")
                vm_instance = self.compute.instances().get(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()
                vm_status = vm_instance.get('status', 'UNKNOWN')
                self._log_info(f"VM status: {vm_status}")
            except HttpError as e:
                if e.resp.status == 403:
                    self._log_debug(
                        "No compute.instances.get permission, "
                        "continuing with serial console only"
                    )
                else:
                    raise

            # Fetch serial console output
            self._log_debug("Fetching serial console output")
            try:
                serial_response = self.compute.instances().getSerialPortOutput(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name,
                    port=1
                ).execute()

                serial_output = serial_response.get('contents', '')
                self._log_debug(f"Retrieved {len(serial_output)} bytes of serial output")

            except HttpError as e:
                error_msg = extract_error_message(e)
                self._log_error(f"Failed to fetch serial console: {error_msg}")

                # Handle specific error cases
                if e.resp.status == 403:
                    # Pre-flight validation already checked IAM permissions,
                    # so a 403 here means serial console access is disabled
                    # at the project or VM level.
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message="Serial console access is disabled",
                        rollback_data={
                            'vm_name': vm_name,
                            'zone': self.zone,
                            'status': vm_status,
                            'diagnosis_status': 'unable_to_diagnose',
                            'boot_errors': [],
                            'recommendations': [
                                "Serial console access is disabled for this VM or project",
                                "Enable on VM: gcloud compute instances add-metadata "
                                f"{vm_name} --zone={self.zone} --metadata serial-port-enable=TRUE",
                                "Enable on project: gcloud compute project-info add-metadata "
                                "--metadata serial-port-enable=TRUE",
                                "Then wait a few minutes for logs to accumulate and try again"
                            ]
                        }
                    )
                else:
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message=f"Failed to fetch serial console: {error_msg}",
                        rollback_data={
                            'vm_name': vm_name,
                            'zone': self.zone,
                            'status': vm_status,
                            'diagnosis_status': 'unable_to_diagnose',
                            'boot_errors': [],
                            'recommendations': [
                                f"Unable to fetch serial console output: {error_msg}",
                                "Check VM permissions and try again"
                            ]
                        }
                    )

            # Analyze serial output
            self._log_info("Analyzing serial console output for boot errors")
            diagnosis: DiagnosisResult = analyze_serial_output(
                serial_output=serial_output,
                vm_name=vm_name,
                zone=self.zone,
                vm_status=vm_status
            )

            # Convert diagnosis to dict for rollback_data
            diagnosis_dict = {
                'vm_name': diagnosis.vm_name,
                'zone': diagnosis.zone,
                'status': diagnosis.status,
                'diagnosis_status': diagnosis.diagnosis_status,
                'boot_errors': [
                    {
                        'category': err.category,
                        'severity': err.severity,
                        'description': err.description,
                        'detected_pattern': err.detected_pattern,
                        'suggested_fixes': err.suggested_fixes,
                        'context_lines': err.context_lines,
                        'matched_line_index': err.matched_line_index
                    }
                    for err in diagnosis.boot_errors
                ],
                'recommendations': diagnosis.recommendations
            }

            # Determine success based on diagnosis status
            if diagnosis.diagnosis_status == "boot_errors_detected":
                message = f"Found {len(diagnosis.boot_errors)} boot error(s)"
                success = True  # Operation succeeded, even though errors were found
            elif diagnosis.diagnosis_status == "healthy":
                message = "No boot errors detected"
                success = True
            else:  # unable_to_diagnose
                message = "Unable to complete diagnosis"
                success = False

            self._log_info(f"Diagnosis complete: {message}")

            return OperationResult(
                operation_name=self.name,
                success=success,
                message=message,
                rollback_data=diagnosis_dict
            )

        except HttpError as e:
            error_msg = extract_error_message(e)
            self._log_debug(f"HTTP error during diagnosis: {error_msg}")

            if e.resp.status == 404:
                message = f"Instance '{vm_name}' not found in zone '{self.zone}'"
                recommendations = [
                    "Verify the instance name and zone are correct:",
                    f"  gcloud compute instances list --project={self.project}",
                ]
            else:
                message = f"Failed to diagnose VM: {error_msg}"
                recommendations = [
                    "Check VM exists and you have necessary permissions",
                ]

            return OperationResult(
                operation_name=self.name,
                success=False,
                message=message,
                rollback_data={
                    'vm_name': vm_name,
                    'zone': self.zone,
                    'status': 'UNKNOWN',
                    'diagnosis_status': 'unable_to_diagnose',
                    'boot_errors': [],
                    'recommendations': recommendations
                }
            )

        except Exception as e:
            self._log_error(f"Unexpected error during diagnosis: {e}")
            return OperationResult(
                operation_name=self.name,
                success=False,
                message=f"Unexpected error: {str(e)}",
                rollback_data={
                    'vm_name': vm_name,
                    'zone': self.zone,
                    'status': 'UNKNOWN',
                    'diagnosis_status': 'unable_to_diagnose',
                    'boot_errors': [],
                    'recommendations': [
                        f"Unexpected error occurred: {str(e)}",
                        "Check logs for details"
                    ]
                }
            )

    def rollback(self, rollback_data: dict) -> bool:
        """No rollback needed for read-only diagnosis operation.

        Args:
            rollback_data: Unused (diagnosis is read-only)

        Returns:
            Always returns True
        """
        self._log_debug("Diagnose operation is read-only, no rollback needed")
        return True
