"""
GCE Rescue - Validators Module

This module provides validators for pre-flight checks before rescue/restore operations.

Usage:
    from validators import (
        ValidationRunner,
        CredentialsValidator,
        IAMPermissionsValidator,
        VMStateValidator
    )

    # Create validator runner
    runner = ValidationRunner()

    # Add validators
    runner.add(CredentialsValidator(compute, project, zone))
    runner.add(IAMPermissionsValidator(compute, project, zone, vm_name))
    runner.add(VMStateValidator(compute, project, zone, vm_name))

    # Run all validators
    results = runner.run_all()

    if not results.all_passed():
        results.print_failures()
        return False
"""

from validators.base import (
    BaseValidator,
    ValidationResult,
    ValidationResults,
    ValidationRunner
)
from validators.credentials import CredentialsValidator
from validators.iam_permissions import IAMPermissionsValidator
from validators.vm_state import VMStateValidator, VMRestoreStateValidator

__all__ = [
    # Base classes
    'BaseValidator',
    'ValidationResult',
    'ValidationResults',
    'ValidationRunner',

    # Validators
    'CredentialsValidator',
    'IAMPermissionsValidator',
    'VMStateValidator',
    'VMRestoreStateValidator',
]
