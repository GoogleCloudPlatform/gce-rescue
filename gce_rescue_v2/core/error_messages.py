# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Error messages with actionable suggestions for GCE Rescue V2.

Provides user-friendly error messages with:
- Clear explanation of what went wrong
- Possible causes
- Actionable steps to resolve
"""

from typing import Optional

from ..utils.colors import error_prefix


class ErrorSuggestion:
    """Container for error message with suggestions."""

    def __init__(self, message: str, causes: list = None, suggestions: list = None,
                 commands: list = None):
        self.message = message
        self.causes = causes or []
        self.suggestions = suggestions or []
        self.commands = commands or []

    def format(self, vm_name: str = None, zone: str = None, project: str = None,
               disk_name: str = None) -> str:
        """Format the error with context-specific details."""
        lines = [f"{error_prefix()} {self.message}"]

        if self.causes:
            lines.append("")
            lines.append("Possible causes:")
            for cause in self.causes:
                lines.append(f"  - {cause}")

        if self.suggestions:
            lines.append("")
            lines.append("Suggestions:")
            for suggestion in self.suggestions:
                lines.append(f"  - {suggestion}")

        if self.commands:
            lines.append("")
            lines.append("Helpful commands:")
            for cmd in self.commands:
                # Replace placeholders
                cmd = cmd.replace("{vm_name}", vm_name or "<VM_NAME>")
                cmd = cmd.replace("{zone}", zone or "<ZONE>")
                cmd = cmd.replace("{project}", project or "<PROJECT>")
                cmd = cmd.replace("{disk_name}", disk_name or "<DISK_NAME>")
                lines.append(f"  $ {cmd}")

        return "\n".join(lines)


# =============================================================================
# VM Operations Errors
# =============================================================================

VM_NOT_FOUND = ErrorSuggestion(
    message="VM not found",
    causes=[
        "VM name is misspelled",
        "VM is in a different zone",
        "VM is in a different project",
        "VM was recently deleted",
    ],
    suggestions=[
        "Verify the VM name and zone are correct",
        "Check if you're using the correct project",
    ],
    commands=[
        "gcloud compute instances list --filter='name={vm_name}'",
        "gcloud compute instances describe {vm_name} --zone={zone}",
    ]
)

VM_STOP_TIMEOUT = ErrorSuggestion(
    message="Timeout waiting for VM to stop",
    causes=[
        "VM has processes that are slow to shut down",
        "VM guest agent is not responding",
        "VM is stuck in a stopping state",
    ],
    suggestions=[
        "Wait a few minutes and try again",
        "Force stop the VM from GCP Console if needed",
        "Check if there are long-running processes in the VM",
    ],
    commands=[
        "gcloud compute instances describe {vm_name} --zone={zone} --format='value(status)'",
        "gcloud compute instances stop {vm_name} --zone={zone}",
    ]
)

VM_START_TIMEOUT = ErrorSuggestion(
    message="Timeout waiting for VM to start",
    causes=[
        "VM boot disk has issues",
        "VM configuration is invalid",
        "Resource quota exceeded",
        "Zone capacity issues",
    ],
    suggestions=[
        "Check VM serial console output for boot errors",
        "Verify the boot disk is healthy",
        "Check project quotas",
    ],
    commands=[
        "gcloud compute instances describe {vm_name} --zone={zone} --format='value(status)'",
        "gcloud compute instances get-serial-port-output {vm_name} --zone={zone}",
        "gcloud compute project-info describe --format='value(quotas)'",
    ]
)

VM_ALREADY_IN_RESCUE = ErrorSuggestion(
    message="VM is already in rescue mode",
    causes=[
        "A previous rescue operation was not restored",
    ],
    suggestions=[
        "Run restore command to exit rescue mode first",
        "Or use --force to re-rescue (not recommended)",
    ],
    commands=[
        "gce-rescue restore {vm_name} --zone={zone} --project={project}",
    ]
)

VM_NOT_IN_RESCUE = ErrorSuggestion(
    message="VM is not in rescue mode",
    causes=[
        "VM was already restored",
        "VM was never put in rescue mode",
        "Rescue metadata was manually removed",
    ],
    suggestions=[
        "Verify the VM is in rescue mode before restoring",
        "Run rescue command first if needed",
    ],
    commands=[
        "gcloud compute instances describe {vm_name} --zone={zone} --format='value(metadata.items)'",
    ]
)

# =============================================================================
# Disk Operations Errors
# =============================================================================

DISK_ATTACH_FAILED = ErrorSuggestion(
    message="Failed to attach disk",
    causes=[
        "Disk is already attached to another VM",
        "Disk is in a different zone than the VM",
        "Maximum number of disks already attached",
        "Disk type incompatible with VM",
    ],
    suggestions=[
        "Verify the disk is not attached to another VM",
        "Ensure disk and VM are in the same zone",
        "Check if VM has reached disk attachment limit (typically 128)",
    ],
    commands=[
        "gcloud compute disks describe {disk_name} --zone={zone} --format='value(users)'",
        "gcloud compute instances describe {vm_name} --zone={zone} --format='table(disks[].source)'",
    ]
)

DISK_DETACH_FAILED = ErrorSuggestion(
    message="Failed to detach disk",
    causes=[
        "Disk is the only boot disk",
        "Disk is not attached to this VM",
        "VM must be stopped to detach boot disk",
    ],
    suggestions=[
        "Ensure VM is stopped before detaching boot disk",
        "Verify the disk is actually attached to this VM",
    ],
    commands=[
        "gcloud compute instances describe {vm_name} --zone={zone} --format='table(disks[].source,disks[].boot)'",
    ]
)

DISK_CREATE_FAILED = ErrorSuggestion(
    message="Failed to create disk",
    causes=[
        "Disk quota exceeded",
        "Disk name already exists",
        "Source image not found",
        "Invalid disk size or type",
    ],
    suggestions=[
        "Check project disk quota",
        "Use a unique disk name",
        "Verify the source image exists",
    ],
    commands=[
        "gcloud compute disks list --filter='name~rescue'",
        "gcloud compute project-info describe --format='value(quotas)'",
        "gcloud compute images list --filter='family=debian-12'",
    ]
)

DISK_DELETE_FAILED = ErrorSuggestion(
    message="Failed to delete disk",
    causes=[
        "Disk is still attached to a VM",
        "Disk is being used by a snapshot",
        "Permission denied",
    ],
    suggestions=[
        "Detach the disk from all VMs first",
        "Wait for any pending snapshots to complete",
    ],
    commands=[
        "gcloud compute disks describe {disk_name} --zone={zone} --format='value(users)'",
    ]
)

# =============================================================================
# Snapshot Errors
# =============================================================================

SNAPSHOT_CREATE_FAILED = ErrorSuggestion(
    message="Failed to create snapshot",
    causes=[
        "Snapshot quota exceeded",
        "Snapshot name already exists",
        "Source disk not found",
        "Permission denied",
    ],
    suggestions=[
        "Check project snapshot quota",
        "Delete old snapshots if quota exceeded",
        "Use --no-snapshot to skip snapshot creation",
    ],
    commands=[
        "gcloud compute snapshots list --filter='sourceDisk~{disk_name}'",
        "gcloud compute project-info describe --format='value(quotas)'",
    ]
)

SNAPSHOT_TIMEOUT = ErrorSuggestion(
    message="Snapshot creation timed out",
    causes=[
        "Large disk takes longer to snapshot",
        "High disk activity during snapshot",
        "GCP service experiencing delays",
    ],
    suggestions=[
        "Snapshot may still be creating in background",
        "Check snapshot status in GCP Console",
        "Use --no-snapshot for faster rescue (riskier)",
    ],
    commands=[
        "gcloud compute snapshots list --filter='name~pre-rescue-{vm_name}'",
        "gcloud compute operations list --filter='targetLink~{vm_name}' --limit=5",
    ]
)

# =============================================================================
# Permission Errors
# =============================================================================

PERMISSION_DENIED = ErrorSuggestion(
    message="Permission denied",
    causes=[
        "Insufficient OAuth scopes (re-authenticate to fix)",
        "Missing required IAM roles",
        "Service account doesn't have access",
    ],
    suggestions=[
        "Re-authenticate: gcloud auth login",
        "Or: gcloud auth application-default login",
        "Required IAM roles: compute.instanceAdmin.v1, compute.storageAdmin",
    ],
    commands=[
        "gcloud auth list",
        "gcloud projects get-iam-policy {project} --flatten='bindings[].members' --filter='bindings.members:$(gcloud auth list --filter=status:ACTIVE --format=\"value(account)\")' --format='table(bindings.role)'",
    ]
)

INSUFFICIENT_SCOPES = ErrorSuggestion(
    message="Insufficient authentication scopes",
    causes=[
        "Your credentials don't include Compute Engine API scopes",
        "Application default credentials were created without compute scopes",
    ],
    suggestions=[
        "Re-authenticate with gcloud to get fresh credentials with correct scopes",
    ],
    commands=[
        "gcloud auth login",
        "gcloud auth application-default login",
    ]
)

CREDENTIALS_INVALID = ErrorSuggestion(
    message="Invalid or expired credentials",
    causes=[
        "Credentials have expired",
        "Not logged in to gcloud",
        "Application default credentials not set",
    ],
    suggestions=[
        "Re-authenticate with gcloud",
        "Set up application default credentials",
    ],
    commands=[
        "gcloud auth login",
        "gcloud auth application-default login",
        "gcloud auth list",
    ]
)

# =============================================================================
# Metadata Errors
# =============================================================================

METADATA_SET_FAILED = ErrorSuggestion(
    message="Failed to set VM metadata",
    causes=[
        "Metadata size limit exceeded (512KB)",
        "Invalid metadata key format",
        "Concurrent metadata modification",
    ],
    suggestions=[
        "Check total metadata size",
        "Retry the operation",
    ],
    commands=[
        "gcloud compute instances describe {vm_name} --zone={zone} --format='value(metadata)'",
    ]
)

# =============================================================================
# Startup Verification Errors
# =============================================================================

STARTUP_VERIFICATION_TIMEOUT = ErrorSuggestion(
    message="Startup script did not complete within timeout period",
    causes=[
        "VM boot is very slow (Windows can take 5-10 minutes)",
        "Startup script crashed or encountered errors",
        "Disk failed to attach or mount",
        "Serial console output is not being captured",
    ],
    suggestions=[
        "Check serial console output for errors",
        "Increase timeout with longer --startup-verification-timeout if needed",
        "Try rescue operation again (VM may be slow on first boot)",
        "Use --skip-startup-verification to bypass (not recommended)",
    ],
    commands=[
        "gcloud compute instances get-serial-port-output {vm_name} --zone={zone}",
        "gcloud compute instances describe {vm_name} --zone={zone}",
        "Check /var/log/gce-rescue.log (Linux) or C:\\gce-rescue.log (Windows) via SSH/RDP",
    ]
)

# =============================================================================
# Helper function to get error suggestion based on error message
# =============================================================================

def get_error_suggestion(error_msg: str, operation: str = None) -> Optional[ErrorSuggestion]:
    """
    Get appropriate error suggestion based on error message content.

    Args:
        error_msg: The error message string
        operation: Optional operation context (e.g., 'stop_vm', 'attach_disk')

    Returns:
        ErrorSuggestion if a match is found, None otherwise
    """
    error_lower = error_msg.lower()

    # Insufficient OAuth scopes (check before generic permission errors)
    if 'insufficient authentication scopes' in error_lower or 'insufficientpermissions' in error_lower:
        return INSUFFICIENT_SCOPES

    # Permission errors
    if 'permission' in error_lower or 'forbidden' in error_lower or '403' in error_lower:
        return PERMISSION_DENIED

    # Authentication errors
    if 'credential' in error_lower or 'token' in error_lower:
        return CREDENTIALS_INVALID

    # Not found errors
    if 'not found' in error_lower or '404' in error_lower:
        if operation and 'disk' in operation:
            return DISK_CREATE_FAILED
        return VM_NOT_FOUND

    # Timeout errors
    if 'timeout' in error_lower:
        if operation == 'stop_vm':
            return VM_STOP_TIMEOUT
        elif operation == 'start_vm':
            return VM_START_TIMEOUT
        elif operation == 'snapshot':
            return SNAPSHOT_TIMEOUT

    # Already exists
    if 'already exists' in error_lower or 'already attached' in error_lower:
        if 'disk' in error_lower:
            return DISK_ATTACH_FAILED

    # Quota errors
    if 'quota' in error_lower or 'limit' in error_lower:
        if 'snapshot' in error_lower:
            return SNAPSHOT_CREATE_FAILED
        return DISK_CREATE_FAILED

    # Rescue mode errors
    if 'rescue-mode' in error_lower or 'rescue mode' in error_lower:
        if 'already' in error_lower:
            return VM_ALREADY_IN_RESCUE
        return VM_NOT_IN_RESCUE

    return None
