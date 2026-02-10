"""Boot error pattern library and analysis engine for VM diagnostics.

This module provides pattern matching and analysis for common boot failures
detected in VM serial console output.
"""

from dataclasses import dataclass, field
from typing import List
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class BootErrorPattern:
    """Defines a pattern for detecting specific boot error types."""
    name: str
    category: str  # fstab, grub, kernel, filesystem
    patterns: List[str]  # Regex patterns to match
    severity: str  # critical, error, warning
    description: str
    suggested_fixes: List[str]


@dataclass
class BootError:
    """Represents a detected boot error."""
    category: str
    severity: str
    description: str
    detected_pattern: str
    suggested_fixes: List[str]
    context_lines: List[str] = field(default_factory=list)  # Lines around the error

    def format_fixes(self, vm_name: str, zone: str) -> List[str]:
        """Format suggested fixes with actual VM name and zone."""
        formatted = []
        for fix in self.suggested_fixes:
            fix = fix.replace("VM_NAME", vm_name)
            fix = fix.replace("ZONE", zone)
            formatted.append(fix)
        return formatted


@dataclass
class DiagnosisResult:
    """Complete diagnosis result for a VM."""
    vm_name: str
    zone: str
    status: str  # VM status (RUNNING, TERMINATED, etc.)
    diagnosis_status: str  # healthy, boot_errors_detected, unable_to_diagnose
    boot_errors: List[BootError] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


# MVP: Focus on fstab errors only
BOOT_ERROR_PATTERNS = [
    BootErrorPattern(
        name="fstab_uuid_not_found",
        category="fstab",
        patterns=[
            r"UUID=[\w-]+ does not exist",
            r"can't find UUID=[\w-]+",
            r"Timed out waiting for device.*UUID=",
        ],
        severity="critical",
        description="UUID specified in /etc/fstab cannot be found",
        suggested_fixes=[
            "Boot into rescue mode: gce-rescue rescue VM_NAME --zone=ZONE",
            "Check /mnt/sysroot/etc/fstab for invalid UUID entries",
            "Run 'blkid' to see available UUIDs",
            "Comment out or fix the invalid UUID entry in fstab"
        ]
    ),
    BootErrorPattern(
        name="fstab_device_not_exist",
        category="fstab",
        patterns=[
            r"special device .* does not exist",
            r"mount: .*: special device .* does not exist",
            r"Device .* does not exist",
        ],
        severity="critical",
        description="Device specified in /etc/fstab does not exist",
        suggested_fixes=[
            "Boot into rescue mode: gce-rescue rescue VM_NAME --zone=ZONE",
            "Check /mnt/sysroot/etc/fstab for invalid device paths",
            "Verify disk attachments in GCP Console",
            "Comment out or fix the invalid device entry in fstab"
        ]
    ),
    BootErrorPattern(
        name="fstab_mount_failed",
        category="fstab",
        patterns=[
            r"Dependency failed for .*\.mount",
            r"Failed to mount .*\.mount",
            r"mount.*failed",
            r"systemd.*Failed to mount",
        ],
        severity="critical",
        description="Failed to mount filesystem listed in /etc/fstab",
        suggested_fixes=[
            "Boot into rescue mode: gce-rescue rescue VM_NAME --zone=ZONE",
            "Check /mnt/sysroot/etc/fstab for mount errors",
            "Verify filesystem type and mount options are correct",
            "Check serial console for specific error messages"
        ]
    ),
    BootErrorPattern(
        name="fstab_emergency_mode",
        category="fstab",
        patterns=[
            r"You are now being dropped into an emergency shell",
            r"You are in emergency mode",
            r"Welcome to emergency mode",
            r"Entering emergency mode",
        ],
        severity="critical",
        description="System entered emergency mode due to boot failure",
        suggested_fixes=[
            "Boot into rescue mode: gce-rescue rescue VM_NAME --zone=ZONE",
            "Common cause: invalid /etc/fstab entries",
            "Check /mnt/sysroot/etc/fstab for errors",
            "Review serial console for error messages leading to emergency mode"
        ]
    ),
    BootErrorPattern(
        name="fstab_fsck_failed",
        category="fstab",
        patterns=[
            r"fsck.*failed",
            r"fsck exited with status code",
            r"FILE SYSTEM CHECK FAILED",
            r"UNEXPECTED INCONSISTENCY",
        ],
        severity="critical",
        description="Filesystem check (fsck) failed on a mounted partition",
        suggested_fixes=[
            "Boot into rescue mode: gce-rescue rescue VM_NAME --zone=ZONE",
            "Run filesystem check: fsck -y /dev/sdb1 (replace with actual device)",
            "May need to run: e2fsck -y /dev/sdb1 for ext4 filesystems",
            "If persistent, check disk health in GCP Console"
        ]
    ),
]


def _extract_context_lines(serial_output: str, match_text: str, context_lines: int = 3) -> List[str]:
    """Extract lines around a matched pattern for context.

    Args:
        serial_output: Full serial console output
        match_text: The matched text to find context for
        context_lines: Number of lines before and after to include

    Returns:
        List of context lines (cleaned of ANSI codes)
    """
    # Split output into lines
    lines = serial_output.split('\n')

    # Find the line containing the match
    match_line_idx = -1
    for i, line in enumerate(lines):
        if match_text in line:
            match_line_idx = i
            break

    if match_line_idx == -1:
        return [match_text]  # Fallback if we can't find it

    # Extract context lines
    start_idx = max(0, match_line_idx - context_lines)
    end_idx = min(len(lines), match_line_idx + context_lines + 1)

    context = lines[start_idx:end_idx]

    # Clean ANSI escape codes
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    context = [ansi_escape.sub('', line).strip() for line in context if line.strip()]

    return context[:7]  # Limit to 7 lines max


def analyze_serial_output(serial_output: str, vm_name: str, zone: str, vm_status: str) -> DiagnosisResult:
    """Analyze serial console output for boot errors.

    Args:
        serial_output: Raw serial console output from VM
        vm_name: Name of the VM
        zone: Zone where VM is located
        vm_status: Current VM status (RUNNING, TERMINATED, etc.)

    Returns:
        DiagnosisResult with detected errors and recommendations
    """
    logger.debug(f"Analyzing serial output for {vm_name} (status: {vm_status})")

    # Check if serial output is empty or too short
    if not serial_output or len(serial_output.strip()) < 50:
        logger.warning("Serial output is empty or too short")
        return DiagnosisResult(
            vm_name=vm_name,
            zone=zone,
            status=vm_status,
            diagnosis_status="unable_to_diagnose",
            recommendations=[
                "Serial console output is empty or too short to analyze",
                "VM may be newly created or serial console may be disabled",
                "Try again after VM has had time to boot and generate logs"
            ]
        )

    detected_errors: List[BootError] = []

    # Check each pattern
    for pattern_def in BOOT_ERROR_PATTERNS:
        for regex_pattern in pattern_def.patterns:
            try:
                match = re.search(regex_pattern, serial_output, re.MULTILINE | re.IGNORECASE)
                if match:
                    logger.info(f"Detected pattern: {pattern_def.name} - {match.group(0)}")

                    # Avoid duplicate errors for the same category
                    if not any(err.category == pattern_def.category and err.description == pattern_def.description
                              for err in detected_errors):
                        # Extract context around the error
                        context = _extract_context_lines(serial_output, match.group(0))

                        detected_errors.append(BootError(
                            category=pattern_def.category,
                            severity=pattern_def.severity,
                            description=pattern_def.description,
                            detected_pattern=match.group(0),
                            suggested_fixes=pattern_def.suggested_fixes,
                            context_lines=context
                        ))
                    break  # Move to next pattern_def after first match
            except re.error as e:
                logger.error(f"Invalid regex pattern: {regex_pattern} - {e}")
                continue

    # Determine diagnosis status and recommendations
    if detected_errors:
        diagnosis_status = "boot_errors_detected"
        recommendations = [
            f"Found {len(detected_errors)} boot error(s) in serial console output",
            f"Use 'gce-rescue rescue {vm_name} --zone={zone}' to enter rescue mode",
            "Review the suggested fixes above for each detected error"
        ]
    else:
        diagnosis_status = "healthy"
        recommendations = [
            "No boot errors detected in serial console output",
            "VM appears to be booting normally"
        ]

        # Additional context based on VM status
        if vm_status == "TERMINATED":
            recommendations.append("Note: VM is currently stopped")
        elif vm_status == "RUNNING":
            recommendations.append("VM is currently running")

    return DiagnosisResult(
        vm_name=vm_name,
        zone=zone,
        status=vm_status,
        diagnosis_status=diagnosis_status,
        boot_errors=detected_errors,
        recommendations=recommendations
    )
