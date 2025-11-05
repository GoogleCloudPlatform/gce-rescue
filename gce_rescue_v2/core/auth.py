"""
GCE Rescue - Authentication Manager

This module handles Google Cloud authentication and client creation.
It extracts and enhances the authentication logic from the original get_client() function.
"""

import google.auth
from google.auth.exceptions import DefaultCredentialsError
from google.auth.transport.requests import Request
from googleapiclient import discovery
import googleapiclient.http
import google_auth_httplib2
import httplib2

from core.exceptions import AuthenticationError
from core.config import VERSION


class AuthManager:
    """
    Manages Google Cloud authentication and API client creation.

    This class:
    1. Gets credentials using Application Default Credentials (ADC)
    2. Validates and refreshes credentials if needed
    3. Creates authenticated GCP Compute API clients
    4. Provides clear error messages when authentication fails

    Usage:
        auth = AuthManager()
        compute, project = auth.get_client()
    """

    def __init__(self):
        """Initialize the authentication manager."""
        self._credentials = None
        self._project = None
        self._compute = None

    def get_credentials(self):
        """
        Get and validate Google Cloud credentials.

        This uses Application Default Credentials (ADC), which searches for
        credentials in this order:
        1. GOOGLE_APPLICATION_CREDENTIALS environment variable
        2. User credentials from gcloud auth application-default login
        3. GCE metadata service (if running on Google Cloud)

        Returns:
            tuple: (credentials, project_id)

        Raises:
            AuthenticationError: If credentials not found or invalid
        """

        try:
            # Get default credentials
            credentials, project = google.auth.default()

            # Validate credentials
            if not credentials.valid:
                # Try to refresh if possible
                if credentials.expired and credentials.refresh_token:
                    try:
                        print("  Refreshing expired credentials...")
                        credentials.refresh(Request())
                    except Exception as e:
                        raise AuthenticationError(
                            "Credentials expired and refresh failed",
                            fix="gcloud auth application-default login"
                        )
                else:
                    raise AuthenticationError(
                        "Credentials are invalid",
                        fix="gcloud auth application-default login"
                    )

            return credentials, project

        except DefaultCredentialsError as e:
            # No credentials found
            raise AuthenticationError(
                "No credentials found. You need to authenticate first.",
                fix="gcloud auth application-default login"
            )

    def get_client(self, project=None):
        """
        Get authenticated Google Compute Engine API client.

        This is the main entry point - replaces the original get_client() function.

        Args:
            project: GCP project ID (optional). If not provided, uses the
                    project from credentials.

        Returns:
            tuple: (compute_client, project_id)
                - compute_client: Authenticated GCP Compute API client
                - project_id: GCP project ID being used

        Raises:
            AuthenticationError: If authentication fails

        Example:
            auth = AuthManager()
            compute, project = auth.get_client()

            # Now use compute client for API calls
            vm = compute.instances().get(
                project=project,
                zone='us-central1-a',
                instance='my-vm'
            ).execute()
        """

        # Get credentials if we don't have them yet
        if not self._credentials:
            self._credentials, self._project = self.get_credentials()

        # Use provided project or fall back to default
        project = project or self._project

        # Create compute client if we don't have one yet
        if not self._compute:
            try:
                # Build the Compute Engine API client with custom User-Agent
                # cache_discovery=False avoids warnings about cache

                def _request_builder(http, *args, **kwargs):
                    """Inject User-Agent header for usage tracking."""
                    headers = kwargs.setdefault('headers', {})
                    headers['user-agent'] = f'gce_rescue-{VERSION}'
                    auth_http = google_auth_httplib2.AuthorizedHttp(
                        self._credentials,
                        http=httplib2.Http()
                    )
                    return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

                self._compute = discovery.build(
                    'compute',
                    'v1',
                    credentials=self._credentials,
                    cache_discovery=False,
                    requestBuilder=_request_builder
                )
            except Exception as e:
                raise AuthenticationError(
                    f"Failed to create GCP API client: {str(e)}"
                )

        return self._compute, project

    def get_project(self):
        """
        Get the current GCP project ID.

        Returns:
            str: Project ID from credentials

        Raises:
            AuthenticationError: If not authenticated
        """
        if not self._project:
            self._credentials, self._project = self.get_credentials()

        return self._project

    def is_authenticated(self):
        """
        Check if we have valid credentials.

        Returns:
            bool: True if authenticated, False otherwise
        """
        try:
            self.get_credentials()
            return True
        except AuthenticationError:
            return False


# Backwards compatibility: provide a simple function like the original
def get_client(project=None):
    """
    Get authenticated compute client (original function interface).

    This function maintains compatibility with the original simple_rescue.py
    while using the new AuthManager internally.

    Args:
        project: GCP project ID (optional)

    Returns:
        tuple: (compute_client, project_id)

    Example:
        compute, project = get_client()
    """
    auth = AuthManager()
    return auth.get_client(project)
