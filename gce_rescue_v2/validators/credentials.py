"""
GCE Rescue - Credentials Validator

Validates that Google Cloud credentials are present and valid.
"""

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request

from .base import BaseValidator, ValidationResult

# OAuth scopes required for Google Compute Engine API
SCOPES = ['https://www.googleapis.com/auth/compute']


class CredentialsValidator(BaseValidator):
    """
    Validates that Google Cloud credentials are present and valid.

    This checks:
    1. Credentials exist (user has authenticated)
    2. Credentials are valid (not expired)
    3. Credentials can be refreshed (if expired)

    Common failure reasons:
    - User hasn't run: gcloud auth application-default login
    - Credentials expired and can't be refreshed
    - No service account configured

    Example:
        validator = CredentialsValidator(compute, project, zone)
        result = validator.validate()

        if result.passed:
            print("[OK] Credentials are valid")
        else:
            print(f"[X] {result.message}")
            print(f"Fix: {result.details['fix']}")
    """

    @property
    def name(self) -> str:
        """Display name for this validator."""
        return "Credentials & Authentication"

    def validate(self) -> ValidationResult:
        """
        Check if valid credentials are available.

        Returns:
            ValidationResult with pass/fail
        """

        try:
            # Try to get default credentials with required scopes
            # This searches for credentials in order:
            # 1. GOOGLE_APPLICATION_CREDENTIALS environment variable
            # 2. User credentials from gcloud auth
            # 3. GCE metadata service (if running on GCP)
            credentials, project = google.auth.default(scopes=SCOPES)

            # Check if credentials are valid
            # Service account credentials need refresh to get access token
            if not credentials.valid:
                try:
                    # Attempt to refresh the credentials
                    credentials.refresh(Request())
                except Exception as e:
                    # Refresh failed
                    return ValidationResult(
                        validator_name=self.name,
                        passed=False,
                        message="Credentials refresh failed",
                        details={
                            "error": str(e),
                            "fix": "Check GOOGLE_APPLICATION_CREDENTIALS or run: gcloud auth application-default login"
                        }
                    )

            # Success! Credentials are valid
            return ValidationResult(
                validator_name=self.name,
                passed=True,
                message=f"Authenticated to project: {project}",
                details={
                    "project": project,
                    "credentials_type": type(credentials).__name__
                }
            )

        except DefaultCredentialsError as e:
            # No credentials found at all
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message="No credentials found",
                details={
                    "error": str(e),
                    "fix": "gcloud auth application-default login"
                }
            )

        except Exception as e:
            # Unexpected error
            return ValidationResult(
                validator_name=self.name,
                passed=False,
                message=f"Unexpected error checking credentials: {str(e)}",
                details={"error": str(e)}
            )
