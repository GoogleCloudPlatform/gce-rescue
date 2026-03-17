"""
GCE Rescue - Custom Exception Classes

This module defines all custom exceptions used in GCE Rescue.
Each exception provides clear error messages with troubleshooting steps.
"""


class GCERescueError(Exception):
    """
    Base exception for all GCE Rescue errors.

    All custom exceptions inherit from this, making it easy to catch
    any GCE Rescue-specific error with a single except clause.
    """
    pass


class AuthenticationError(GCERescueError):
    """
    Raised when authentication fails.

    Common causes:
    - No credentials configured
    - Credentials expired
    - Invalid credentials
    """

    def __init__(self, message: str, fix: str = None):
        """
        Args:
            message: Error description
            fix: Suggested fix command (e.g., "gcloud auth login")
        """
        self.fix = fix
        full_message = f"{message}"
        if fix:
            full_message += f"\n\nFix: {fix}"
        super().__init__(full_message)


class PermissionError(GCERescueError):
    """
    Raised when user lacks required IAM permissions.

    Common causes:
    - Missing compute.instances.* permissions
    - Missing compute.disks.* permissions
    """

    def __init__(self, message: str, missing_permissions: list = None, required_roles: list = None):
        """
        Args:
            message: Error description
            missing_permissions: List of missing permissions
            required_roles: List of roles that grant required permissions
        """
        self.missing_permissions = missing_permissions or []
        self.required_roles = required_roles or []

        full_message = f"{message}"

        if missing_permissions:
            full_message += f"\n\nMissing permissions:"
            for perm in missing_permissions:
                full_message += f"\n  - {perm}"

        if required_roles:
            full_message += f"\n\nRequired roles (grant one of these):"
            for role in required_roles:
                full_message += f"\n  - {role}"

        super().__init__(full_message)


class VMNotFoundError(GCERescueError):
    """
    Raised when the specified VM doesn't exist.
    """

    def __init__(self, vm_name: str, zone: str, project: str):
        """
        Args:
            vm_name: Name of the VM that wasn't found
            zone: Zone where we looked
            project: Project where we looked
        """
        self.vm_name = vm_name
        self.zone = zone
        self.project = project

        message = f"VM '{vm_name}' not found in zone '{zone}' (project: {project})"
        message += f"\n\nTroubleshooting:"
        message += f"\n  1. Check VM name spelling"
        message += f"\n  2. Verify the zone is correct"
        message += f"\n  3. Verify the project is correct"
        message += f"\n\nList VMs in this zone:"
        message += f"\n  gcloud compute instances list --zone={zone} --project={project}"

        super().__init__(message)


class VMStateError(GCERescueError):
    """
    Raised when VM is in an invalid state for the operation.

    Examples:
    - Trying to rescue a VM that's already in rescue mode
    - Trying to restore a VM that's not in rescue mode
    - VM is STOPPING or SUSPENDING (transitional state)
    """

    def __init__(self, vm_name: str, current_state: str, required_state: str = None):
        """
        Args:
            vm_name: Name of the VM
            current_state: Current VM state (e.g., 'STOPPING')
            required_state: Required state for operation (e.g., 'RUNNING or TERMINATED')
        """
        self.vm_name = vm_name
        self.current_state = current_state
        self.required_state = required_state

        message = f"VM '{vm_name}' is in state: {current_state}"
        if required_state:
            message += f"\nRequired state: {required_state}"

        super().__init__(message)


class DiskNotFoundError(GCERescueError):
    """
    Raised when a disk doesn't exist.
    """

    def __init__(self, disk_name: str, zone: str, project: str):
        """
        Args:
            disk_name: Name of the disk that wasn't found
            zone: Zone where we looked
            project: Project where we looked
        """
        self.disk_name = disk_name
        self.zone = zone
        self.project = project

        message = f"Disk '{disk_name}' not found in zone '{zone}' (project: {project})"
        super().__init__(message)


class QuotaExceededError(GCERescueError):
    """
    Raised when GCP quota is exceeded.

    Examples:
    - Too many disks
    - Not enough CPU quota
    """

    def __init__(self, resource: str, current: int, limit: int):
        """
        Args:
            resource: Resource type (e.g., 'disks', 'cpus')
            current: Current usage
            limit: Quota limit
        """
        self.resource = resource
        self.current = current
        self.limit = limit

        message = f"Quota exceeded for {resource}: {current}/{limit}"
        message += f"\n\nYou need to either:"
        message += f"\n  1. Delete unused {resource}"
        message += f"\n  2. Request quota increase in GCP Console"

        super().__init__(message)


class OperationFailedError(GCERescueError):
    """
    Raised when a GCP operation fails.
    """

    def __init__(self, operation_name: str, reason: str):
        """
        Args:
            operation_name: Name of the operation (e.g., 'Stop VM')
            reason: Why it failed
        """
        self.operation_name = operation_name
        self.reason = reason

        message = f"Operation '{operation_name}' failed: {reason}"
        super().__init__(message)


class RollbackError(GCERescueError):
    """
    Raised when rollback fails.

    This is serious - the VM may be in an inconsistent state.
    """

    def __init__(self, message: str, failed_operations: list = None):
        """
        Args:
            message: Error description
            failed_operations: List of operations that failed to rollback
        """
        self.failed_operations = failed_operations or []

        full_message = f"CRITICAL: Rollback failed - {message}"

        if failed_operations:
            full_message += f"\n\nFailed to rollback these operations:"
            for op in failed_operations:
                full_message += f"\n  - {op}"
            full_message += f"\n\nManual intervention required!"

        super().__init__(full_message)


class ValidationError(GCERescueError):
    """
    Raised when pre-flight validation fails.
    """

    def __init__(self, validator_name: str, message: str, fix: str = None):
        """
        Args:
            validator_name: Name of the validator that failed
            message: What failed
            fix: Suggested fix
        """
        self.validator_name = validator_name
        self.fix = fix

        full_message = f"Validation failed: {validator_name}\n{message}"
        if fix:
            full_message += f"\n\nFix: {fix}"

        super().__init__(full_message)
