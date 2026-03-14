"""
GCE Rescue - IAM Permissions Validator

Validates that the user has required IAM permissions for rescue operations.
"""

from googleapiclient.errors import HttpError

from .base import BaseValidator, ValidationResult


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

    All permissions are included in roles/compute.instanceAdmin.v1.

    Common failure reasons:
    - User account doesn't have compute.instanceAdmin.v1 role
    - Service account missing permissions

    Example:
        validator = IAMPermissionsValidator(compute, project, zone, 'my-vm')
        result = validator.validate()

        if not result.passed:
            print(f"Missing permissions: {result.details['missing']}")
            print(f"Required roles: {result.details['required_roles']}")
    """

    # Instance-level permissions (tested on instance resource)
    INSTANCE_PERMISSIONS = [
        'compute.instances.get',
        'compute.instances.stop',
        'compute.instances.start',
        'compute.instances.attachDisk',
        'compute.instances.detachDisk',
        'compute.instances.setMetadata',
    ]

    # Disk-level permissions (tested on boot disk resource)
    DISK_PERMISSIONS = [
        'compute.disks.create',
        'compute.disks.delete',
        'compute.disks.get',
        'compute.disks.createSnapshot',
    ]

    # Snapshot permissions (tested on boot disk resource where possible)
    # compute.snapshots.create/get/delete are project-level and cannot
    # be tested via testIamPermissions on a disk. However, if the user
    # has compute.disks.createSnapshot, they almost certainly have
    # compute.snapshots.* too (both are in instanceAdmin.v1).
    SNAPSHOT_PERMISSIONS = [
        'compute.snapshots.create',
        'compute.snapshots.get',
        'compute.snapshots.delete',
    ]

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "IAM Permissions"

    def _get_boot_disk_name(self, compute) -> str:
        """Get the boot disk name from the VM.

        Returns:
            Boot disk name, or None if not found.
        """
        try:
            vm = compute.instances().get(
                project=self.project,
                zone=self.zone,
                instance=self.vm_name
            ).execute()
            for disk in vm.get('disks', []):
                if disk.get('boot'):
                    return disk['source'].split('/')[-1]
        except Exception:
            pass
        return None

    def _test_disk_permissions(self, compute, disk_name: str) -> list:
        """Test disk-level permissions on the boot disk.

        Args:
            compute: Compute API client.
            disk_name: Name of the disk to test permissions on.

        Returns:
            List of missing disk permissions.
        """
        try:
            result = compute.disks().testIamPermissions(
                project=self.project,
                zone=self.zone,
                resource=disk_name,
                body={'permissions': self.DISK_PERMISSIONS}
            ).execute()
            granted = result.get('permissions', [])
            return [p for p in self.DISK_PERMISSIONS if p not in granted]
        except HttpError:
            # If we can't test disk permissions (e.g., disk not found
            # during restore), skip and let execution validate
            return []

    def validate(self) -> ValidationResult:
        """
        Check if user has required IAM permissions.

        Tests instance permissions on the VM resource and disk permissions
        on the boot disk resource.

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
            # Use tracked client if tracking_label provided
            compute = self._create_tracked_client(self.tracking_label) if self.tracking_label else self.compute

            # 1. Test instance-level permissions
            result = compute.instances().testIamPermissions(
                project=self.project,
                zone=self.zone,
                resource=self.vm_name,
                body={'permissions': self.INSTANCE_PERMISSIONS}
            ).execute()

            granted_instance = result.get('permissions', [])
            missing_instance = [
                p for p in self.INSTANCE_PERMISSIONS
                if p not in granted_instance
            ]

            # 2. Test disk-level permissions on boot disk
            missing_disk = []
            boot_disk = self._get_boot_disk_name(compute)
            if boot_disk:
                missing_disk = self._test_disk_permissions(compute, boot_disk)

            # Combine all missing permissions
            all_missing = missing_instance + missing_disk

            if all_missing:
                return ValidationResult(
                    validator_name=self.name,
                    passed=False,
                    message=f"Missing {len(all_missing)} required permission(s)",
                    details={
                        "missing": all_missing,
                        "granted": granted_instance,
                        "required_roles": [
                            "roles/compute.instanceAdmin.v1"
                        ],
                    }
                )

            # All checked permissions granted
            total_checked = len(granted_instance) + len(self.DISK_PERMISSIONS) - len(missing_disk)
            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message=f"Permissions OK ({total_checked} checked)",
                details={
                    "granted": granted_instance,
                    "boot_disk_checked": boot_disk,
                    "required_roles": [
                        "roles/compute.instanceAdmin.v1"
                    ],
                }
            )

        except HttpError as e:
            if e.resp.status == 404:
                # VM not found - this will be caught by VMStateValidator
                return ValidationResult(
                    validator_name=self.name,
                    passed=True,  # Don't fail here, let VMStateValidator handle it
                    message="Skipped (VM validation will run next)",
                    details={"note": "VM not found, will be caught by VM validator"}
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
                                "      Re-authenticate with:\n"
                                "        $ gcloud auth login\n"
                                "        $ gcloud auth application-default login"
                            ),
                        }
                    )
                else:
                    return ValidationResult(
                        validator_name=self.name,
                        passed=False,
                        message="Permission denied",
                        details={
                            "missing": self.INSTANCE_PERMISSIONS + self.DISK_PERMISSIONS,
                            "required_roles": [
                                "roles/compute.instanceAdmin.v1"
                            ],
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
