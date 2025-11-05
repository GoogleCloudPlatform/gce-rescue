"""
GCE Rescue - IAM Permissions Validator

Validates that the user has required IAM permissions for rescue operations.
"""

from googleapiclient.errors import HttpError

from validators.base import BaseValidator, ValidationResult


class IAMPermissionsValidator(BaseValidator):
    """
    Validates that user has required IAM permissions.

    This uses the testIamPermissions API to check if the user has
    all required permissions for rescue operations.

    Required permissions:
    - compute.instances.get
    - compute.instances.stop
    - compute.instances.start
    - compute.instances.attachDisk
    - compute.instances.detachDisk
    - compute.instances.setMetadata
    - compute.disks.create
    - compute.disks.delete
    - compute.disks.get
    - compute.disks.createSnapshot (default, use --no-snapshot to skip)
    - compute.snapshots.create (default, use --no-snapshot to skip)
    - compute.snapshots.get (default, use --no-snapshot to skip)
    - compute.snapshots.delete (default, use --no-snapshot to skip)

    Common failure reasons:
    - User account doesn't have compute.instanceAdmin role
    - User account doesn't have compute.storageAdmin role (for snapshots)
    - Service account missing permissions

    Example:
        validator = IAMPermissionsValidator(compute, project, zone, 'my-vm')
        result = validator.validate()

        if not result.passed:
            print(f"Missing permissions: {result.details['missing']}")
            print(f"Required roles: {result.details['required_roles']}")
    """

    # Instance-level permissions (can be tested on instance resource)
    INSTANCE_PERMISSIONS = [
        'compute.instances.get',
        'compute.instances.stop',
        'compute.instances.start',
        'compute.instances.attachDisk',
        'compute.instances.detachDisk',
        'compute.instances.setMetadata',
    ]

    # Project/disk-level permissions (cannot be tested on instance)
    # These are required but we'll just document them
    DISK_PERMISSIONS = [
        'compute.disks.create',
        'compute.disks.delete',
        'compute.disks.get',
    ]

    # Snapshot permissions (required by default, disabled with --no-snapshot)
    SNAPSHOT_PERMISSIONS = [
        'compute.disks.createSnapshot',
        'compute.snapshots.create',
        'compute.snapshots.get',
        'compute.snapshots.delete',
    ]

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "IAM Permissions"

    def validate(self) -> ValidationResult:
        """
        Check if user has required IAM permissions.

        Returns:
            ValidationResult with pass/fail
        """

        # VM name is required for this validator
        if not self.vm_name:
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message="VM name required to check permissions",
                details={"error": "vm_name not provided"}
            )

        try:
            # Test instance-level permissions using testIamPermissions API
            # Note: Disk permissions cannot be tested on instance resource
            request_body = {
                'permissions': self.INSTANCE_PERMISSIONS
            }

            result = self.compute.instances().testIamPermissions(
                project=self.project,
                zone=self.zone,
                resource=self.vm_name,
                body=request_body
            ).execute()

            # Get permissions that were granted
            granted_permissions = result.get('permissions', [])

            # Find missing instance permissions
            missing_permissions = [
                p for p in self.INSTANCE_PERMISSIONS
                if p not in granted_permissions
            ]

            if missing_permissions:
                # Some permissions are missing
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Missing {len(missing_permissions)} required permission(s)",
                    details={
                        "missing": missing_permissions,
                        "granted": granted_permissions,
                        "required_roles": [
                            "roles/compute.instanceAdmin.v1 (for VM operations)",
                            "roles/compute.storageAdmin (for disk and snapshot operations)"
                        ],
                        "fix": f"Grant required permissions to your account for project {self.project}",
                        "note": "Snapshot permissions also required (checked during execution)"
                    }
                )

            # All instance permissions granted!
            # Note: Disk and snapshot permissions are also required but can't be tested on instance
            total_required = len(self.INSTANCE_PERMISSIONS) + len(self.DISK_PERMISSIONS) + len(self.SNAPSHOT_PERMISSIONS)
            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message=f"Instance permissions OK ({len(granted_permissions)}/{len(self.INSTANCE_PERMISSIONS)})",
                details={
                    "granted": granted_permissions,
                    "instance_permissions": self.INSTANCE_PERMISSIONS,
                    "disk_permissions_required": self.DISK_PERMISSIONS,
                    "snapshot_permissions_required": self.SNAPSHOT_PERMISSIONS,
                    "note": "Disk and snapshot permissions will be validated during execution",
                    "required_roles": [
                        "roles/compute.instanceAdmin.v1 (for VM operations)",
                        "roles/compute.storageAdmin (for disk and snapshot operations)"
                    ]
                }
            )

        except HttpError as e:
            if e.resp.status == 404:
                # VM not found - this will be caught by VMStateValidator
                # For now, just pass (we'll fail later with better message)
                return ValidationResult(
                    validator_name=self.name,
                    passed=True,  # Don't fail here, let VMStateValidator handle it
                    message="Skipped (VM validation will run next)",
                    details={"note": "VM not found, will be caught by VM validator"}
                )
            else:
                # Some other API error
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Failed to check permissions: {str(e)}",
                    details={"error": str(e)}
                )

        except Exception as e:
            # Unexpected error
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message=f"Unexpected error checking permissions: {str(e)}",
                details={"error": str(e)}
            )
