"""
GCE Rescue - Diagnose Permissions Validator

Validates that the user has required IAM permissions for diagnose.
This is a lightweight check - diagnose only needs read permissions.
"""

from googleapiclient.errors import HttpError

from .base import BaseValidator, ValidationResult


class DiagnosePermissionsValidator(BaseValidator):
    """
    Validates that user has required IAM permissions for diagnose.

    Required permissions (read-only):
    - compute.instances.get (to fetch VM status)
    - compute.instances.getSerialPortOutput (to read serial console)

    Example:
        validator = DiagnosePermissionsValidator(compute, project, zone, 'my-vm')
        result = validator.validate()

        if not result.passed:
            print(f"Missing permissions: {result.details['missing']}")
    """

    REQUIRED_PERMISSIONS = [
        'compute.instances.get',
        'compute.instances.getSerialPortOutput',
    ]

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "Diagnose Permissions"

    def validate(self) -> ValidationResult:
        """Check if user has required IAM permissions for diagnose.

        Returns:
            ValidationResult with pass/fail
        """
        if not self.vm_name:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message="VM name required to check permissions",
                details={"error": "vm_name not provided"}
            )

        try:
            request_body = {
                'permissions': self.REQUIRED_PERMISSIONS
            }

            compute = (
                self._create_tracked_client(self.tracking_label)
                if self.tracking_label
                else self.compute
            )
            result = compute.instances().testIamPermissions(
                project=self.project,
                zone=self.zone,
                resource=self.vm_name,
                body=request_body
            ).execute()

            granted_permissions = result.get('permissions', [])

            missing_permissions = [
                p for p in self.REQUIRED_PERMISSIONS
                if p not in granted_permissions
            ]

            if missing_permissions:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Missing {len(missing_permissions)} required permission(s)",
                    details={
                        "missing": missing_permissions,
                        "granted": granted_permissions,
                        "required_roles": [
                            "roles/compute.viewer (for read-only access)",
                        ],
                        "fix": (
                            f"Grant compute.viewer role to your account "
                            f"for project {self.project}"
                        ),
                    }
                )

            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message=f"Permissions OK ({len(granted_permissions)}/{len(self.REQUIRED_PERMISSIONS)})",
                details={"granted": granted_permissions}
            )

        except HttpError as e:
            if e.resp.status == 404:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Instance '{self.vm_name}' not found in zone '{self.zone}'",
                    details={
                        "error": "VM not found",
                        "fix": (
                            f"Verify the instance name and zone are correct.\n"
                            f"      To list instances: "
                            f"gcloud compute instances list --project={self.project}"
                        ),
                    }
                )
            elif e.resp.status == 403:
                error_str = str(e)
                if ('insufficient authentication scopes' in error_str.lower()
                        or 'insufficientPermissions' in error_str):
                    return ValidationResult(
                        validator_name=self.name,
                        passed=False,
                        message="Insufficient authentication scopes",
                        details={
                            "fix": (
                                "Your credentials don't include Compute Engine "
                                "API scopes.\n"
                                "      Run: gcloud auth application-default login"
                            ),
                        }
                    )
                else:
                    # testIamPermissions itself requires compute.instances.list
                    # at the project level. In production environments, users
                    # often have instance-level access without project-level
                    # list. Pass through and let actual API calls validate.
                    return ValidationResult(
                        validator_name=self.name,
                        passed=True,
                        message="Skipped (insufficient access to testIamPermissions API)",
                        details={
                            "note": (
                                "Could not pre-check permissions because "
                                "testIamPermissions requires "
                                "compute.instances.list. "
                                "Actual permissions will be validated "
                                "during execution."
                            ),
                        }
                    )
            else:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Failed to check permissions: {str(e)}",
                    details={"error": str(e)}
                )

        except Exception as e:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message=f"Unexpected error checking permissions: {str(e)}",
                details={"error": str(e)}
            )
