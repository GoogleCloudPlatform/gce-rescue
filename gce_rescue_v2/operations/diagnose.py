"""Diagnose VM boot issues by analyzing serial console output.

This operation is read-only and does not modify the VM in any way.
"""

import time
from typing import Optional
import logging

from googleapiclient.errors import HttpError

from ..core.diagnosis import analyze_serial_output, DiagnosisResult
from ..utils.os_detection import (
    detect_os_type, detect_os_flavor, detect_architecture, detect_license_type,
)
from .base import BaseOperation, OperationResult, extract_error_message

logger = logging.getLogger(__name__)


class DiagnoseOperation(BaseOperation):
    """Diagnose VM boot issues by analyzing serial console output."""

    @property
    def name(self) -> str:
        """Return the operation name."""
        return "Diagnose VM"

    def execute(self, vm_name: str, tracking_label: str = None,
                stabilize: bool = False) -> OperationResult:
        """Execute diagnosis by fetching and analyzing serial console output.

        Args:
            vm_name: Name of the VM to diagnose
            tracking_label: Optional tracking label for usage analytics.
            stabilize: When True and VM is RUNNING, poll until diagnosis
                stabilizes (2 consecutive identical results) before returning.

        Returns:
            OperationResult with diagnosis data in rollback_data field
        """
        try:
            self._log_debug(f"Starting diagnosis for VM: {vm_name}")
            if tracking_label:
                self._log_debug(f"  Operation tracking: {tracking_label}")

            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(tracking_label) if tracking_label else self.compute

            # Get VM status and OS info (non-fatal if 403 - user may only
            # have serial console read permission on this project)
            vm_status = 'UNKNOWN'
            os_type = 'unknown'
            os_flavor = 'unknown'
            architecture = 'unknown'
            license_type = 'unknown'
            try:
                self._log_debug("Fetching VM instance details")
                vm_instance = compute.instances().get(
                    project=self.project,
                    zone=self.zone,
                    instance=vm_name
                ).execute()
                vm_status = vm_instance.get('status', 'UNKNOWN')
                self._log_debug(f"VM status: {vm_status}")

                # Detect OS info
                os_type = detect_os_type(vm_instance)
                os_flavor = detect_os_flavor(vm_instance)
                architecture = detect_architecture(vm_instance)
                license_type = detect_license_type(vm_instance)
                self._log_debug(
                    f"OS: {os_type}, flavor: {os_flavor}, "
                    f"arch: {architecture}, license: {license_type}"
                )
            except HttpError as e:
                if e.resp.status == 403:
                    self._log_debug(
                        "No compute.instances.get permission, "
                        "continuing with serial console only"
                    )
                else:
                    raise

            # Status-aware warnings
            if vm_status == 'SUSPENDED':
                self._log_debug(
                    "VM is suspended, serial logs may not contain "
                    "recent boot activity"
                )
            elif vm_status in ('STAGING', 'PROVISIONING'):
                self._log_debug(
                    "VM is still starting, serial output may be incomplete"
                )

            # Use stabilization polling for RUNNING VMs when requested
            if stabilize and vm_status == 'RUNNING':
                diagnosis_dict = self._stabilize_diagnosis(
                    compute, vm_name, vm_status,
                    os_type=os_type, os_flavor=os_flavor,
                    architecture=architecture, license_type=license_type
                )
            else:
                # Single-pass: fetch and analyze once
                diagnosis_dict = self._single_pass_diagnosis(
                    compute, vm_name, vm_status,
                    os_type=os_type, os_flavor=os_flavor,
                    architecture=architecture, license_type=license_type
                )

            # _single_pass_diagnosis returns None on serial fetch failure
            # (it returns an OperationResult directly in that case)
            if isinstance(diagnosis_dict, OperationResult):
                return diagnosis_dict

            # Determine success based on diagnosis status
            if diagnosis_dict['diagnosis_status'] == "boot_errors_detected":
                message = f"Found {len(diagnosis_dict['boot_errors'])} boot error(s)"
                success = True  # Operation succeeded, even though errors were found
            elif diagnosis_dict['diagnosis_status'] == "healthy":
                message = "No boot errors detected"
                success = True
            else:  # unable_to_diagnose
                message = "Unable to complete diagnosis"
                success = False

            self._log_debug(f"Diagnosis complete: {message}")

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
                    'os_type': 'unknown',
                    'os_flavor': 'unknown',
                    'architecture': 'unknown',
                    'license_type': 'unknown',
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
                    'os_type': 'unknown',
                    'os_flavor': 'unknown',
                    'architecture': 'unknown',
                    'license_type': 'unknown',
                    'diagnosis_status': 'unable_to_diagnose',
                    'boot_errors': [],
                    'recommendations': [
                        f"Unexpected error occurred: {str(e)}",
                        "Check logs for details"
                    ]
                }
            )

    def _fetch_serial_output(self, compute, vm_name: str) -> str:
        """Fetch raw serial console output from the VM.

        Args:
            compute: Compute API client to use.
            vm_name: Name of the VM.

        Returns:
            Raw serial output string.

        Raises:
            HttpError: On API failure (caller handles).
        """
        self._log_debug("Fetching serial console output")
        serial_response = compute.instances().getSerialPortOutput(
            project=self.project,
            zone=self.zone,
            instance=vm_name,
            port=1
        ).execute()

        serial_output = serial_response.get('contents', '')
        self._log_debug(f"Retrieved {len(serial_output)} bytes of serial output")
        return serial_output

    def _analyze_serial(self, serial_output: str, vm_name: str,
                        vm_status: str, os_type: str, os_flavor: str,
                        architecture: str, license_type: str) -> dict:
        """Analyze serial output and return diagnosis dict.

        Args:
            serial_output: Raw serial console text.
            vm_name: VM name.
            vm_status: Current VM status string.
            os_type: Detected OS type.
            os_flavor: Detected OS flavor.
            architecture: Detected architecture.
            license_type: Detected license type.

        Returns:
            Diagnosis dict with all standard fields.
        """
        diagnosis: DiagnosisResult = analyze_serial_output(
            serial_output=serial_output,
            vm_name=vm_name,
            zone=self.zone,
            vm_status=vm_status
        )

        return {
            'vm_name': diagnosis.vm_name,
            'zone': diagnosis.zone,
            'status': diagnosis.status,
            'os_type': os_type,
            'os_flavor': os_flavor,
            'architecture': architecture,
            'license_type': license_type,
            'diagnosis_status': diagnosis.diagnosis_status,
            'boot_errors': [
                {
                    'name': err.name,
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

    def _diagnosis_signature(self, diagnosis_dict: dict) -> tuple:
        """Extract a comparable signature from a diagnosis dict.

        Two diagnoses are "the same" when the status and set of error
        names match — we intentionally ignore detected_pattern text and
        context_lines which can shift as the serial buffer grows.

        Returns:
            (diagnosis_status, frozenset of error names)
        """
        error_names = frozenset(
            err['name'] for err in diagnosis_dict.get('boot_errors', [])
        )
        return (diagnosis_dict.get('diagnosis_status'), error_names)

    def _single_pass_diagnosis(self, compute, vm_name: str, vm_status: str,
                               os_type: str = 'unknown',
                               os_flavor: str = 'unknown',
                               architecture: str = 'unknown',
                               license_type: str = 'unknown'):
        """Fetch serial output once and analyze it.

        Returns:
            Diagnosis dict on success, or OperationResult on serial fetch failure.
        """
        try:
            serial_output = self._fetch_serial_output(compute, vm_name)
        except HttpError as e:
            error_msg = extract_error_message(e)
            self._log_error(f"Failed to fetch serial console: {error_msg}")

            if e.resp.status == 403:
                # Distinguish between OAuth scope errors and serial port access
                error_lower = error_msg.lower()
                if ('insufficient authentication scopes' in error_lower
                        or 'insufficientPermissions' in error_msg):
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message="Insufficient authentication scopes",
                        rollback_data={
                            'vm_name': vm_name,
                            'zone': self.zone,
                            'status': vm_status,
                            'os_type': os_type,
                            'os_flavor': os_flavor,
                            'architecture': architecture,
                            'license_type': license_type,
                            'diagnosis_status': 'unable_to_diagnose',
                            'boot_errors': [],
                            'recommendations': [
                                "Your credentials don't include Compute Engine "
                                "API scopes",
                                "Run: gcloud auth application-default login",
                                "Then try again"
                            ]
                        }
                    )
                else:
                    return OperationResult(
                        operation_name=self.name,
                        success=False,
                        message="Serial console access is disabled",
                        rollback_data={
                            'vm_name': vm_name,
                            'zone': self.zone,
                            'status': vm_status,
                            'os_type': os_type,
                            'os_flavor': os_flavor,
                            'architecture': architecture,
                            'license_type': license_type,
                            'diagnosis_status': 'unable_to_diagnose',
                            'boot_errors': [],
                            'recommendations': [
                                "Serial console access is disabled for this VM "
                                "or project",
                                "Enable on VM: gcloud compute instances "
                                "add-metadata "
                                f"{vm_name} --zone={self.zone} "
                                "--metadata serial-port-enable=TRUE",
                                "Enable on project: gcloud compute project-info "
                                "add-metadata --metadata serial-port-enable=TRUE",
                                "Then wait a few minutes for logs to accumulate "
                                "and try again"
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
                        'os_type': os_type,
                        'os_flavor': os_flavor,
                        'architecture': architecture,
                        'license_type': license_type,
                        'diagnosis_status': 'unable_to_diagnose',
                        'boot_errors': [],
                        'recommendations': [
                            "Unable to fetch serial console output: "
                            f"{error_msg}",
                            "Check VM permissions and try again"
                        ]
                    }
                )

        self._log_debug("Analyzing serial console output for boot errors")
        return self._analyze_serial(
            serial_output, vm_name, vm_status,
            os_type, os_flavor, architecture, license_type
        )

    def _stabilize_diagnosis(
        self, compute, vm_name: str, vm_status: str,
        max_seconds: int = 30, poll_interval: int = 5,
        required_stable: int = 2,
        os_type: str = 'unknown', os_flavor: str = 'unknown',
        architecture: str = 'unknown', license_type: str = 'unknown'
    ) -> dict:
        """Poll serial console until the diagnosis result stabilizes.

        Only meaningful for RUNNING VMs where new serial output may appear.
        Returns as soon as ``required_stable`` consecutive polls produce the
        same diagnosis signature (status + set of error names).

        Args:
            compute: Compute API client.
            vm_name: VM name.
            vm_status: Current VM status.
            max_seconds: Hard timeout in seconds (default 30).
            poll_interval: Seconds between polls (default 5).
            required_stable: Consecutive identical results needed (default 2).
            os_type: Detected OS type.
            os_flavor: Detected OS flavor.
            architecture: Detected architecture.
            license_type: Detected license type.

        Returns:
            Diagnosis dict from the last poll.
        """
        self._log_debug(
            f"Stabilization polling: max {max_seconds}s, "
            f"interval {poll_interval}s, require {required_stable} stable"
        )

        deadline = time.monotonic() + max_seconds
        prev_signature = None
        stable_count = 0
        last_result = None

        while True:
            serial_output = self._fetch_serial_output(compute, vm_name)
            diagnosis_dict = self._analyze_serial(
                serial_output, vm_name, vm_status,
                os_type, os_flavor, architecture, license_type
            )
            last_result = diagnosis_dict

            current_sig = self._diagnosis_signature(diagnosis_dict)
            if current_sig == prev_signature:
                stable_count += 1
            else:
                stable_count = 1
                prev_signature = current_sig

            self._log_debug(
                f"Poll: status={current_sig[0]}, "
                f"errors={set(current_sig[1])}, "
                f"stable={stable_count}/{required_stable}"
            )

            if stable_count >= required_stable:
                self._log_debug("Diagnosis stabilized")
                return last_result

            if time.monotonic() >= deadline:
                self._log_debug(
                    f"Stabilization timeout ({max_seconds}s), "
                    "returning last result"
                )
                return last_result

            time.sleep(poll_interval)

    def rollback(self, rollback_data: dict) -> bool:
        """No rollback needed for read-only diagnosis operation.

        Args:
            rollback_data: Unused (diagnosis is read-only)

        Returns:
            Always returns True
        """
        self._log_debug("Diagnose operation is read-only, no rollback needed")
        return True
