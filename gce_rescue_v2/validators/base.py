"""
GCE Rescue - Base Validator

This module provides the base class for all validators.
Each validator checks one thing and returns pass/fail.

Pattern: Create a new validator by inheriting from BaseValidator
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ValidationResult:
    """
    Result from a single validation check.

    Attributes:
        validator_name: Name of the validator (for display)
        passed: True if validation passed, False if failed
        message: Human-readable message about the result
        details: Optional dict with extra info (for debugging/fixes)
    """
    validator_name: str
    passed: bool
    message: str
    details: Optional[dict] = None

    def __str__(self):
        """String representation of result."""
        status = "[OK]" if self.passed else "[X]"
        return f"{status} {self.validator_name}: {self.message}"


class ValidationResults:
    """
    Collection of validation results.

    Makes it easy to check if all validations passed and print results.
    """

    def __init__(self):
        """Initialize empty results."""
        self.results: List[ValidationResult] = []

    def add(self, result: ValidationResult):
        """Add a validation result."""
        self.results.append(result)

    def all_passed(self) -> bool:
        """Check if all validations passed."""
        return all(r.passed for r in self.results)

    def get_failures(self) -> List[ValidationResult]:
        """Get only failed validations."""
        return [r for r in self.results if not r.passed]

    def print_all(self):
        """Print all validation results."""
        for result in self.results:
            print(f"  {result}")

    def print_failures(self):
        """Print only failed validations with details."""
        failures = self.get_failures()
        if not failures:
            return

        print("\nPre-flight validation failed:")
        print()
        for result in failures:
            print(f"  [X] {result.validator_name}")
            print(f"    {result.message}")

            # Print fix suggestions if available
            if result.details and 'fix' in result.details:
                print(f"    Fix: {result.details['fix']}")
            print()


class BaseValidator(ABC):
    """
    Base class for all validators.

    To create a new validator:
    1. Inherit from this class
    2. Implement the validate() method
    3. Implement the name property

    Example:
        class MyValidator(BaseValidator):
            @property
            def name(self):
                return "My Check"

            def validate(self):
                # Check something
                if all_good:
                    return ValidationResult(
                        validator_name=self.name,
                        passed=True,
                        message="Everything is good"
                    )
                else:
                    return ValidationResult(
                        validator_name=self.name,
                        passed=False,
                        message="Something is wrong",
                        details={'fix': 'Do this to fix it'}
                    )
    """

    def __init__(self, compute, project: str, zone: str, vm_name: str = None):
        """
        Initialize validator.

        Args:
            compute: GCP compute client (from google-api-python-client)
            project: GCP project ID
            zone: GCP zone (e.g., 'us-central1-a')
            vm_name: Name of the VM to validate (optional for some validators)
        """
        self.compute = compute
        self.project = project
        self.zone = zone
        self.vm_name = vm_name

    @abstractmethod
    def validate(self) -> ValidationResult:
        """
        Run the validation check.

        This method must be implemented by each validator.

        Returns:
            ValidationResult with pass/fail and message
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Human-readable name of this validator.

        Used for display in validation results.
        """
        pass


class ValidationRunner:
    """
    Runs multiple validators and collects results.

    Example:
        runner = ValidationRunner()
        runner.add(CredentialsValidator(...))
        runner.add(IAMPermissionsValidator(...))
        runner.add(VMStateValidator(...))

        results = runner.run_all()

        if not results.all_passed():
            results.print_failures()
            return False
    """

    def __init__(self):
        """Initialize empty validator list."""
        self.validators: List[BaseValidator] = []

    def add(self, validator: BaseValidator):
        """
        Add a validator to the chain.

        Args:
            validator: A validator instance
        """
        self.validators.append(validator)

    def run_all(self, logger=None) -> ValidationResults:
        """
        Run all validators and collect results.

        Args:
            logger: Optional logger for debug output

        Returns:
            ValidationResults with all results
        """
        results = ValidationResults()

        for validator in self.validators:
            # Log what we're checking (DEBUG level)
            if logger:
                logger.debug(f"Running validator: {validator.name}")

            # Run the validator
            result = validator.validate()

            # Log result (DEBUG level)
            if logger:
                status = "PASS" if result.passed else "FAIL"
                logger.debug(f"  {status}: {result.message}")

            # Add to results
            results.add(result)

            # Print result for user (INFO level)
            if logger:
                if result.passed:
                    logger.info(f"  [OK] {result.validator_name}")
                else:
                    logger.info(f"  [FAIL] {result.validator_name}")

        return results
