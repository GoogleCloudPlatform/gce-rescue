"""Pre-flight validation helpers for CLI commands."""

import sys
from typing import Optional


def _create_tracked_client(compute, user_agent: str):
    """Create a compute client with a custom User-Agent header.

    Args:
        compute: Base compute client (used to extract credentials)
        user_agent: Full User-Agent string (from build_user_agent())

    Returns:
        Compute API client with the specified User-Agent header.
    """
    try:
        from googleapiclient import discovery
        import googleapiclient.http
        import google_auth_httplib2
        import httplib2

        # Verify compute client has real credentials (not a test mock)
        if not isinstance(getattr(compute, '_http', None), google_auth_httplib2.AuthorizedHttp):
            return compute

        credentials = compute._http.credentials

        def _request_builder(http, *args, **kwargs):
            headers = kwargs.setdefault('headers', {})
            headers['user-agent'] = user_agent
            auth_http = google_auth_httplib2.AuthorizedHttp(
                credentials, http=httplib2.Http()
            )
            return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

        return discovery.build(
            'compute', 'v1', credentials=credentials,
            cache_discovery=False, requestBuilder=_request_builder
        )
    except Exception:
        return compute


def validate_custom_rescue_image(
    compute, vm_info, image_url, session_id, command, mode,
) -> tuple:
    """Pre-flight validation of --rescue-image (URL, existence, OS, arch).

    Performs a single Compute API call (tagged for analytics) and checks:
        - URL format is parseable
        - Image exists and is accessible (HTTP 403/404 handled cleanly)
        - Image OS family matches VM OS family
        - Image architecture matches VM architecture

    Used by both handle_rescue and handle_repair so the validation logic
    stays consistent across subcommands.

    Args:
        compute: Compute API client (will be wrapped for analytics).
        vm_info: VM resource dict (from _validate_vm_exists).
        image_url: User-supplied --rescue-image value.
        session_id: Session id for analytics tagging.
        command: 'rescue' or 'repair' (analytics).
        mode: 'interactive' or 'auto' (analytics).

    Returns:
        (size_gb, None) on success, (None, error_message) on failure.
    """
    from ..utils.os_detection import detect_os_type, detect_architecture
    from ..orchestration.rescue import RescueOrchestrator
    from ..core.config import build_user_agent
    from googleapiclient.errors import HttpError

    ua = build_user_agent(
        session_id=session_id, command=command, mode=mode,
        step='image-preflight-custom',
    )
    tracked = _create_tracked_client(compute, ua)

    try:
        image_dict = RescueOrchestrator.fetch_custom_image(tracked, image_url)
    except ValueError as e:
        return None, str(e)
    except HttpError as e:
        if e.resp.status == 404:
            return None, f"Rescue image not found: {image_url}"
        elif e.resp.status == 403:
            return None, f"No permission to access rescue image: {image_url}"
        else:
            return None, f"Failed to inspect rescue image ({image_url}): {e}"

    vm_os = detect_os_type(vm_info)
    image_os = RescueOrchestrator.get_custom_image_os(image_dict)
    if image_os != vm_os:
        return None, (
            f"--rescue-image OS mismatch: VM is {vm_os}, "
            f"but image is {image_os}.\n"
            f"      Rescue image OS must match the VM's OS family."
        )

    vm_arch = detect_architecture(vm_info)
    image_arch = RescueOrchestrator.get_custom_image_architecture(image_dict)
    if image_arch != vm_arch:
        return None, (
            f"--rescue-image architecture mismatch: VM is {vm_arch}, "
            f"but image is {image_arch}.\n"
            f"      Rescue image architecture must match the VM's."
        )

    return int(image_dict.get('diskSizeGb', 0)), None


def get_gcloud_config(key: str) -> Optional[str]:
    """
    Read configuration from gcloud config.

    Args:
        key: Config key (e.g., 'core/project', 'compute/zone')

    Returns:
        Config value or None
    """
    try:
        # Try to read from gcloud config
        import subprocess
        import platform

        # On Windows, gcloud is a batch file, need shell=True
        use_shell = platform.system() == 'Windows'
        result = subprocess.run(
            ['gcloud', 'config', 'get-value', key],
            capture_output=True,
            text=True,
            timeout=5,
            shell=use_shell
        )
        value = result.stdout.strip()
        return value if value and value != '(unset)' else None
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        # gcloud not available or error
        return None


def _parse_api_error(e: Exception, vm_name: str, zone: str, project: str = None) -> str:
    """Parse GCP API error and return user-friendly message."""
    error_str = str(e)

    if 'was not found' in error_str or 'notFound' in error_str:
        list_cmd = (f"gcloud compute instances list --project={project}"
                    if project else "gcloud compute instances list")
        lines = [
            f"Instance [{vm_name}] not found.",
            f"  Zone: {zone}",
        ]
        if project:
            lines.append(f"  Project: {project}")
        lines.append("")
        lines.append("To see available instances, run:")
        lines.append(f"  $ {list_cmd}")
        lines.append("")
        return "\n".join(lines)

    if 'Unknown zone' in error_str or (
        "Invalid value for field" in error_str and "'zone'" in error_str
    ):
        lines = [
            f"Invalid zone '{zone}'.",
            "",
            "To see available zones, run:",
            "  $ gcloud compute zones list",
            ""
        ]
        return "\n".join(lines)

    if 'forbidden' in error_str.lower() or 'permission' in error_str.lower() or '403' in error_str:
        # Distinguish between OAuth scope errors and IAM permission errors
        if 'insufficient authentication scopes' in error_str.lower() or 'insufficientPermissions' in error_str:
            lines = [
                "Insufficient authentication scopes.",
                "",
                "gce-rescue cannot access Compute Engine APIs with your current credentials.",
                "Run this to authenticate:",
                "  $ gcloud auth application-default login",
                ""
            ]
        else:
            lines = [
                "Permission denied.",
                "",
                "Your account may be missing required IAM roles.",
                "Required role: roles/compute.instanceAdmin.v1",
                "",
                "To check your roles:",
                f"  $ gcloud projects get-iam-policy {project or 'PROJECT_ID'} \\",
                "      --flatten='bindings[].members' \\",
                "      --filter='bindings.members:YOUR_EMAIL' \\",
                "      --format='table(bindings.role)'",
                "",
                "To grant access:",
                f"  $ gcloud projects add-iam-policy-binding {project or 'PROJECT_ID'} \\",
                "      --member='user:YOUR_EMAIL' \\",
                "      --role='roles/compute.instanceAdmin.v1'",
                ""
            ]
        return "\n".join(lines)

    if 'Invalid value for field' in error_str:
        lines = [
            "Invalid request parameters.",
            "",
            "Verify the following are correct:",
            f"  Instance: {vm_name}",
            f"  Zone: {zone}",
        ]
        if project:
            lines.append(f"  Project: {project}")
        lines.append("")
        return "\n".join(lines)

    # Fallback: return simplified error
    return f"API error: {error_str[:200]}\n"


def _validate_vm_exists(compute, project: str, zone: str, vm_name: str,
                        user_agent: str = None) -> tuple:
    """
    Validate VM exists and is in a valid state for rescue.

    Returns:
        (success: bool, vm_info: dict or None, error_message: str or None)
    """
    try:
        client = _create_tracked_client(compute, user_agent) if user_agent else compute
        vm = client.instances().get(
            project=project,
            zone=zone,
            instance=vm_name
        ).execute()

        # Check if already in rescue mode
        metadata = vm.get('metadata', {}).get('items', [])
        for item in metadata:
            if item.get('key') == 'rescue-mode':
                lines = [
                    f"Instance [{vm_name}] is already in rescue mode.",
                    "",
                    "To exit rescue mode and restore the VM, run:",
                    f"  $ gce-rescue restore {vm_name} --zone={zone} --project={project}",
                    ""
                ]
                return (False, None, "\n".join(lines))

        # Check VM state
        status = vm.get('status', 'UNKNOWN')
        invalid_states = ['STAGING', 'PROVISIONING', 'SUSPENDING', 'SUSPENDED', 'REPAIRING']
        if status in invalid_states:
            lines = [
                f"Instance [{vm_name}] is in state '{status}'.",
                "",
                "The VM must be in RUNNING or TERMINATED state to rescue.",
                "",
                "To check the current VM status, run:",
                f"  $ gcloud compute instances describe {vm_name} --zone={zone}"
                f" --project={project} --format='value(status)'",
                ""
            ]
            return (False, None, "\n".join(lines))

        return (True, vm, None)

    except Exception as e:
        return (False, None, _parse_api_error(e, vm_name, zone, project))


def _check_local_ssds(vm_info: dict) -> list:
    """Check if VM has Local SSDs attached. Returns list of Local SSD names."""
    if not vm_info:
        return []

    local_ssds = []
    for disk in vm_info.get('disks', []):
        if disk.get('type') == 'SCRATCH':
            local_ssds.append(disk.get('deviceName', 'unknown'))
    return local_ssds


def _validate_vm_for_restore(compute, project: str, zone: str, vm_name: str,
                             user_agent: str = None) -> tuple:
    """
    Validate VM exists and is in rescue mode for restore.

    Returns:
        (success: bool, vm_info: dict or None, error_message: str or None)
    """
    try:
        client = _create_tracked_client(compute, user_agent) if user_agent else compute
        vm = client.instances().get(
            project=project,
            zone=zone,
            instance=vm_name
        ).execute()

        # Check if in rescue mode
        metadata = vm.get('metadata', {}).get('items', [])
        in_rescue_mode = False
        for item in metadata:
            if item.get('key') == 'rescue-mode':
                in_rescue_mode = True
                break

        if not in_rescue_mode:
            lines = [
                f"Instance [{vm_name}] is not in rescue mode.",
                "",
                "To put the VM into rescue mode first, run:",
                f"  $ gce-rescue rescue {vm_name} --zone={zone} --project={project}",
                ""
            ]
            return (False, None, "\n".join(lines))

        return (True, vm, None)

    except Exception as e:
        return (False, None, _parse_api_error(e, vm_name, zone, project))
