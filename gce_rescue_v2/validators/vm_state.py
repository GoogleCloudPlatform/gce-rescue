"""
GCE Rescue - VM State Validator

Validates that the VM exists and is in a valid state for rescue operations.
"""

from googleapiclient.errors import HttpError

from validators.base import BaseValidator, ValidationResult


class VMStateValidator(BaseValidator):
    """
    Validates that VM exists and is in a valid state.

    This checks:
    1. VM exists in the specified zone
    2. VM is in a valid state (RUNNING or TERMINATED)
    3. VM has a boot disk
    4. VM is not already in rescue mode

    Valid states for rescue:
    - RUNNING: VM is running normally
    - TERMINATED: VM is stopped

    Invalid states:
    - STOPPING: VM is in transition
    - STARTING: VM is in transition
    - SUSPENDED: VM is suspended
    - PROVISIONING: VM is being created

    Example:
        validator = VMStateValidator(compute, project, zone, 'my-vm')
        result = validator.validate()

        if not result.passed:
            print(f"VM state: {result.details['current_state']}")
            print(f"Required: {result.details['valid_states']}")
    """

    # Valid states for rescue operation
    VALID_STATES = ['RUNNING', 'TERMINATED']

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "VM State"

    def validate(self) -> ValidationResult:
        """
        Check if VM exists and is in valid state.

        Returns:
            ValidationResult with pass/fail
        """

        # VM name is required
        if not self.vm_name:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message="VM name required",
                details={"error": "vm_name not provided"}
            )

        try:
            # Try to get VM
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()

            # Check VM state
            current_state = vm['status']

            # Check if already in rescue mode
            metadata = vm.get('metadata', {})
            metadata_items = metadata.get('items', [])
            in_rescue_mode = any(
                item.get('key') == 'rescue-mode'
                for item in metadata_items
            )

            if in_rescue_mode:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message="VM is already in rescue mode",
                    details={
                        "current_state": current_state,
                        "fix": f"Use 'restore' command to exit rescue mode first"
                    }
                )

            # Check if state is valid
            if current_state not in self.VALID_STATES:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"VM is in invalid state: {current_state}",
                    details={
                        "current_state": current_state,
                        "valid_states": self.VALID_STATES,
                        "fix": "Wait for VM to reach RUNNING or TERMINATED state"
                    }
                )

            # Check boot disk
            boot_disk = None
            for disk in vm.get('disks', []):
                if disk.get('boot'):
                    boot_disk = disk['source'].split('/')[-1]
                    break

            if not boot_disk:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message="VM has no boot disk",
                    details={"error": "No boot disk found"}
                )

            # All good!
            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message=f"VM exists and is {current_state}",
                details={
                    "vm_name": self.vm_name,
                    "current_state": current_state,
                    "boot_disk": boot_disk,
                    "zone": self.zone,
                    "project": self.project
                }
            )

        except HttpError as e:
            if e.resp.status == 404:
                # VM not found
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"VM '{self.vm_name}' not found in zone '{self.zone}'",
                    details={
                        "vm_name": self.vm_name,
                        "zone": self.zone,
                        "project": self.project,
                        "fix": f"gcloud compute instances list --zone={self.zone} --project={self.project}"
                    }
                )
            else:
                # Some other API error
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Failed to get VM info: {str(e)}",
                    details={"error": str(e)}
                )

        except Exception as e:
            # Unexpected error
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message=f"Unexpected error checking VM: {str(e)}",
                details={"error": str(e)}
            )


class VMRestoreStateValidator(BaseValidator):
    """
    Validates that VM is in rescue mode and ready for restore.

    This checks:
    1. VM exists
    2. VM is in rescue mode (has rescue-mode metadata)
    3. VM has both rescue disk and original disk

    Example:
        validator = VMRestoreStateValidator(compute, project, zone, 'my-vm')
        result = validator.validate()
    """

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "VM Restore State"

    def validate(self) -> ValidationResult:
        """
        Check if VM is in rescue mode and ready for restore.

        Returns:
            ValidationResult with pass/fail
        """

        # VM name is required
        if not self.vm_name:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message="VM name required",
                details={"error": "vm_name not provided"}
            )

        try:
            # Get VM
            vm = self.compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()

            # Check if in rescue mode
            metadata = vm.get('metadata', {})
            metadata_items = metadata.get('items', [])
            in_rescue_mode = any(
                item.get('key') == 'rescue-mode'
                for item in metadata_items
            )

            if not in_rescue_mode:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message="VM is not in rescue mode",
                    details={
                        "fix": "Use 'rescue' command first to enter rescue mode"
                    }
                )

            # Check disks
            disks = vm.get('disks', [])
            rescue_disk = None
            original_disk = None

            for disk in disks:
                disk_name = disk['source'].split('/')[-1]
                if 'rescue-disk' in disk_name:
                    rescue_disk = disk_name
                elif not disk.get('boot'):
                    original_disk = disk_name

            if not rescue_disk:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message="Rescue disk not found",
                    details={"error": "Cannot find rescue disk"}
                )

            if not original_disk:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message="Original disk not found",
                    details={"error": "Cannot find original disk"}
                )

            # All good!
            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message="VM is in rescue mode and ready for restore",
                details={
                    "rescue_disk": rescue_disk,
                    "original_disk": original_disk
                }
            )

        except HttpError as e:
            if e.resp.status == 404:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"VM '{self.vm_name}' not found",
                    details={"error": "VM not found"}
                )
            else:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Failed to get VM info: {str(e)}",
                    details={"error": str(e)}
                )

        except Exception as e:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message=f"Unexpected error: {str(e)}",
                details={"error": str(e)}
            )
