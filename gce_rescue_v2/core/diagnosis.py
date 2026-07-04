"""Analysis engine for VM boot diagnostics.

Loads detection patterns and fix suggestions from YAML files in the
diagnose_rules/ directory, then analyzes serial console output for
common boot failures.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple
import re
import logging

import yaml

logger = logging.getLogger(__name__)

VALID_SEVERITIES = {'critical', 'error', 'warning'}


@dataclass
class BootErrorPattern:
    """Defines a pattern for detecting specific boot error types."""
    name: str
    category: str  # fstab, grub, kernel, filesystem
    patterns: List[str]  # Regex patterns to match
    severity: str  # critical, error, warning
    description: str
    fixes: List[str] = field(default_factory=list)  # Suggested fixes
    # Category-level flags, copied onto each pattern of the category:
    # survives_boot_success: findings are NOT cleared by the RUNNING +
    #   boot-success-marker suppression (failures that don't block boot).
    # detect_only: runtime condition, not on-disk boot config — never a
    #   suppressing "root cause" in dedupe.
    survives_boot_success: bool = False
    detect_only: bool = False


@dataclass
class DetectedError:
    """Represents a detected boot error."""
    name: str  # Pattern name (e.g. fstab_uuid_not_found) for fix lookup
    category: str
    severity: str
    description: str
    detected_pattern: str
    suggested_fixes: List[str]
    context_lines: List[str] = field(default_factory=list)  # Lines around the error
    matched_line_index: int = -1  # Index of the matched line within context_lines

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
    boot_errors: List[DetectedError] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


# Severity ordering for sorted display (lower = higher priority)
SEVERITY_ORDER = {'critical': 0, 'error': 1, 'warning': 2}


def _validate_pattern_file(data: dict, filename: str) -> None:
    """Validate YAML structure and regex syntax at load time.

    Args:
        data: Parsed YAML data
        filename: Name of the YAML file (for error messages)

    Raises:
        ValueError: If the YAML structure is invalid
    """
    required_top_level = ['category', 'patterns']
    for key in required_top_level:
        if key not in data:
            raise ValueError(f"{filename}: missing required field '{key}'")

    if not isinstance(data['patterns'], list) or len(data['patterns']) == 0:
        raise ValueError(f"{filename}: 'patterns' must be a non-empty list")

    for flag in ('survives_boot_success', 'detect_only'):
        if flag in data and not isinstance(data[flag], bool):
            raise ValueError(
                f"{filename}: '{flag}' must be a boolean"
            )

    required_pattern_fields = ['name', 'severity', 'description', 'regex']
    for i, pattern in enumerate(data['patterns']):
        for pf in required_pattern_fields:
            if pf not in pattern:
                raise ValueError(
                    f"{filename}: pattern #{i + 1} missing required field '{pf}'"
                )

        if pattern['severity'] not in VALID_SEVERITIES:
            raise ValueError(
                f"{filename}: pattern '{pattern['name']}' has invalid severity "
                f"'{pattern['severity']}' (must be one of: {', '.join(sorted(VALID_SEVERITIES))})"
            )

        if not isinstance(pattern['regex'], list) or len(pattern['regex']) == 0:
            raise ValueError(
                f"{filename}: pattern '{pattern['name']}' must have at least one regex"
            )

        for regex in pattern['regex']:
            try:
                re.compile(regex)
            except re.error as e:
                raise ValueError(
                    f"{filename}: pattern '{pattern['name']}' has invalid regex "
                    f"'{regex}': {e}"
                ) from e


def _load_patterns_from_yaml(
    patterns_dir: Path = None,
) -> List[BootErrorPattern]:
    """Load patterns from YAML files in the diagnose_rules/ directory.

    Args:
        patterns_dir: Directory containing YAML pattern files.
            Defaults to the diagnose_rules/ directory next to this module.

    Returns:
        List of BootErrorPattern objects.

    Raises:
        ValueError: If no YAML files found or validation fails
    """
    if patterns_dir is None:
        patterns_dir = Path(__file__).parent / 'diagnose_rules'

    yaml_files = sorted(patterns_dir.glob('*.yaml'))
    if not yaml_files:
        raise ValueError(f"No pattern YAML files found in {patterns_dir}")

    all_patterns: List[BootErrorPattern] = []

    for yaml_file in yaml_files:
        data = yaml.safe_load(yaml_file.read_text(encoding='utf-8'))
        _validate_pattern_file(data, yaml_file.name)

        category = data['category']
        survives = bool(data.get('survives_boot_success', False))
        detect_only = bool(data.get('detect_only', False))

        for p in data['patterns']:
            all_patterns.append(BootErrorPattern(
                name=p['name'],
                category=category,
                patterns=p['regex'],
                severity=p['severity'],
                description=p['description'],
                fixes=list(p.get('fixes', [])),
                survives_boot_success=survives,
                detect_only=detect_only,
            ))

    return all_patterns


# Load patterns at module level (fail fast if patterns are broken)
BOOT_ERROR_PATTERNS = _load_patterns_from_yaml()

# Category behavior sets derived from the YAML flags — the analysis engine
# never hardcodes category names.
SURVIVES_BOOT_SUCCESS_CATEGORIES = frozenset(
    p.category for p in BOOT_ERROR_PATTERNS if p.survives_boot_success)
DETECT_ONLY_CATEGORIES = frozenset(
    p.category for p in BOOT_ERROR_PATTERNS if p.detect_only)


def _extract_context_lines(
    serial_output: str, match_text: str, context_lines: int = 3,
    match_pos: int = None
) -> Tuple[List[str], int]:
    """Extract lines around a matched pattern for context.

    Args:
        serial_output: Full serial console output
        match_text: The matched text to find context for
        context_lines: Number of lines before and after to include
        match_pos: Character offset of the actual regex match. When given,
            the matched line is derived from this offset directly — never
            by re-searching for match_text. This matters when the matched
            text also appears on earlier lines (e.g. a generic
            'Kernel panic - not syncing' whose lookahead rejected an older
            panic line): re-searching would anchor the evidence on the
            wrong (first) occurrence, while the matcher deliberately uses
            the last one.

    Returns:
        Tuple of (context lines cleaned of ANSI codes, index of matched line)
    """
    # Split output into lines
    lines = serial_output.split('\n')

    if match_pos is not None:
        # Line index = number of newlines before the match offset
        match_line_idx = serial_output.count('\n', 0, match_pos)
        if match_line_idx >= len(lines):
            match_line_idx = -1
    else:
        # Fallback: find the first line containing the match text
        match_line_idx = -1
        for i, line in enumerate(lines):
            if match_text in line:
                match_line_idx = i
                break

    if match_line_idx == -1:
        return [match_text], 0  # Fallback: single line, index 0

    # Extract context window
    start_idx = max(0, match_line_idx - context_lines)
    end_idx = min(len(lines), match_line_idx + context_lines + 1)

    # Track matched line's position relative to the window
    relative_match_idx = match_line_idx - start_idx

    raw_context = lines[start_idx:end_idx]

    # Clean ANSI escape codes, filter empty lines, track index shift
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    cleaned = []
    final_match_idx = -1
    for i, line in enumerate(raw_context):
        if not line.strip():
            continue
        cleaned_line = ansi_escape.sub('', line).strip()
        if i == relative_match_idx:
            final_match_idx = len(cleaned)
        cleaned.append(cleaned_line)

    # Cap at 7 lines
    cleaned = cleaned[:7]
    if final_match_idx >= 7:
        final_match_idx = -1  # Matched line got truncated

    return cleaned, final_match_idx


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

    # Strip ANSI escape codes that interfere with pattern matching.
    # Serial console output from systemd often contains color/formatting
    # codes like \x1b[0;31m that break regex matches.
    if serial_output:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\r')
        serial_output = ansi_escape.sub('', serial_output)

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

    detected_errors: List[DetectedError] = []

    # Check each pattern — use the LAST match in the serial output.
    # Serial console buffers accumulate across reboots, so the first
    # match may be from an old boot.  We want the most recent occurrence.
    for pattern_def in BOOT_ERROR_PATTERNS:
        for regex_pattern in pattern_def.patterns:
            try:
                match = None
                for m in re.finditer(regex_pattern, serial_output, re.MULTILINE | re.IGNORECASE):
                    match = m
                if match:
                    logger.info(f"Detected pattern: {pattern_def.name} - {match.group(0)}")

                    # Avoid duplicate errors for the same category
                    if not any(err.category == pattern_def.category and err.description == pattern_def.description
                              for err in detected_errors):
                        # Extract context around the error, anchored on the
                        # actual match offset (not a re-search of the text)
                        context, match_idx = _extract_context_lines(
                            serial_output, match.group(0),
                            context_lines=1,
                            match_pos=match.start()
                        )

                        # Use inline fixes from the pattern definition
                        fixes = list(pattern_def.fixes)

                        detected_errors.append(DetectedError(
                            name=pattern_def.name,
                            category=pattern_def.category,
                            severity=pattern_def.severity,
                            description=pattern_def.description,
                            detected_pattern=match.group(0),
                            suggested_fixes=fixes,
                            context_lines=context,
                            matched_line_index=match_idx
                        ))
                    break  # Move to next pattern_def after first match
            except re.error as e:
                logger.error(f"Invalid regex pattern: {regex_pattern} - {e}")
                continue

    # Deduplicate with two tiers:
    # Tier 1 (catch-all): emergency mode - remove if ANY other error exists
    # Tier 2 (generic symptom): mount failed, dependency failed - remove
    #         only if a specific root cause error exists
    _CATCH_ALL = {
        "System entered emergency mode due to boot failure",
    }
    _GENERIC_SYMPTOM = {
        "Failed to mount filesystem listed in /etc/fstab",
        "Mount point dependency failed (device not available)",
    }
    # Snapshot the full evidence set before dedupe: the boot-success
    # suppression below must reason over everything that matched (positions,
    # emergency-mode presence), not just the deduped survivors.
    all_detected = list(detected_errors)

    # A finding only counts as a suppressing "root cause" if it can actually
    # explain a boot failure: detect-only categories (YAML flag
    # 'detect_only') describe runtime conditions, not on-disk boot config;
    # survives-boot-success categories (e.g. ssh) describe failures that by
    # definition do NOT block boot and so can never explain emergency mode;
    # and warnings are informational. None of these may hide critical
    # boot-failure findings like emergency mode.
    def _is_boot_root_cause(err: DetectedError) -> bool:
        return (err.category not in DETECT_ONLY_CATEGORIES
                and err.category not in SURVIVES_BOOT_SUCCESS_CATEGORIES
                and err.severity != 'warning')

    if len(detected_errors) > 1:
        # Tier 1 is additionally gated on CRITICAL severity: emergency mode
        # is itself a critical boot-blocker, so only a finding that names a
        # critical root cause may replace it. Error-level companions (e.g.
        # systemd_no_console, which fires on EVERY emergency entry because
        # root is locked on GCP images, or an ordering-cycle report) are
        # symptoms/context — letting them suppress the catch-all demotes a
        # real emergency incident to a lone ERROR whose fix text points at
        # a "failure reported above" that no longer exists.
        has_non_catchall = any(
            _is_boot_root_cause(e) and e.severity == 'critical'
            and e.description not in _CATCH_ALL
            for e in detected_errors
        )
        if has_non_catchall:
            detected_errors = [
                e for e in detected_errors
                if e.description not in _CATCH_ALL
            ]
    if len(detected_errors) > 1:
        # Tier 2 is category-scoped AND root-cause gated: a generic symptom
        # is only demoted when a genuine root-cause finding of the SAME
        # category exists. A finding from an unrelated category (e.g. a
        # stale ssh auth error or a cpu_lockup runtime condition in the
        # serial buffer) must never erase fstab boot-blockers, and
        # warnings/runtime findings never demote anything.
        root_cause_categories = {
            e.category for e in detected_errors
            if _is_boot_root_cause(e) and e.description not in _GENERIC_SYMPTOM
        }
        detected_errors = [
            e for e in detected_errors
            if e.description not in _GENERIC_SYMPTOM
            or e.category not in root_cause_categories
        ]

    # Boot success detection: if VM is RUNNING and the LATEST boot completed
    # successfully, clear non-emergency errors entirely.
    # The tool's purpose is diagnosing boot failures — if the VM booted,
    # nofail/timeout errors are not actionable and just add noise.
    #
    # Key: we check ordering — the boot success marker must appear AFTER
    # the last detected error. If errors appear after the last "Startup
    # finished", the VM failed on the most recent boot.
    _BOOT_SUCCESS_MARKERS = [
        r'Startup finished in',
        r'Reached target .*multi-user\.target',
        r'Started .*OpenBSD Secure Shell server',
        r'Started .*Google Compute Engine Startup Scripts',
    ]
    # Categories flagged 'survives_boot_success' in their YAML (e.g. ssh,
    # filesystem) describe failures that do not block boot — sshd dies but
    # boot completes, or a corrupt nofail secondary disk lets the VM boot
    # while the disk stays broken. A "Startup finished" marker does not mean
    # they are resolved, so they are exempt from boot-success suppression.
    suppressible = [
        e for e in detected_errors
        if e.category not in SURVIVES_BOOT_SUCCESS_CATEGORIES
    ]
    if vm_status == 'RUNNING' and suppressible:
        # Find the position of the last boot success marker
        last_success_pos = -1
        for marker in _BOOT_SUCCESS_MARKERS:
            for match in re.finditer(marker, serial_output, re.IGNORECASE):
                last_success_pos = max(last_success_pos, match.end())

        # Find the position of the last detected error. Use the full
        # pre-dedupe evidence set: a finding removed by dedupe (e.g.
        # emergency mode) still proves the latest boot failed.
        # Skip survives-boot-success categories: they are exempt from
        # suppression anyway, so a post-marker ssh/filesystem line must not
        # veto the clearing of stale boot-blocker noise from an older boot.
        last_error_pos = -1
        for err in all_detected:
            if err.category in SURVIVES_BOOT_SUCCESS_CATEGORIES:
                continue
            for match in re.finditer(
                re.escape(err.detected_pattern), serial_output, re.IGNORECASE
            ):
                last_error_pos = max(last_error_pos, match.end())

        # Boot succeeded only if success marker appears AFTER all errors
        boot_completed = (
            last_success_pos > 0 and last_success_pos > last_error_pos
        )

        if boot_completed:
            # No emergency-mode veto here: boot_completed already requires
            # the last success marker to sit AFTER the last occurrence of
            # every non-surviving finding in all_detected — including the
            # emergency-mode line itself (fstab/initramfs, neither of which
            # survives boot success). Any emergency evidence at this point
            # is therefore provably from an older, resolved boot in the
            # accumulating serial buffer; keeping a presence-based veto
            # made every resolved emergency incident report CRITICAL
            # forever until the buffer rotated.
            logger.debug(
                f"VM booted successfully (success at pos {last_success_pos}, "
                f"last error at pos {last_error_pos}) — clearing "
                f"{len(suppressible)} non-blocking error(s)"
            )
            detected_errors = [
                e for e in detected_errors
                if e.category in SURVIVES_BOOT_SUCCESS_CATEGORIES
            ]

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
