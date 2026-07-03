"""Diagnostic report formatter for diagnose command.

Formats diagnosis results into clean, professional CLI output
following gcloud conventions and trivy/kubectl-style design.
"""

import re
from typing import Dict, Any, List, Optional
from ..core.diagnosis import SEVERITY_ORDER
from ..core.fix_catalog import CATEGORY_FIX_GUIDANCE
from .colors import red, yellow, green, bold, dim


class DiagnosisReportFormatter:
    """Formats DiagnosisResult dicts into human-readable CLI reports."""

    def format_report(self, diagnosis: Dict[str, Any],
                      skip_fix_section: bool = False) -> str:
        """Format a complete diagnosis report.

        Args:
            diagnosis: Diagnosis result dict with keys:
                vm_name, zone, status, diagnosis_status,
                boot_errors, recommendations
            skip_fix_section: If True, omit the "To fix this issue:" section.
                Used by repair command which shows its own repair plan.

        Returns:
            Formatted report string
        """
        status = diagnosis.get('diagnosis_status', '')

        if status == 'boot_errors_detected':
            return self._format_errors_report(diagnosis, skip_fix_section)
        elif status == 'healthy':
            return self._format_healthy(diagnosis)
        else:
            return self._format_unable(diagnosis)

    def _format_header(self, diagnosis: Dict[str, Any]) -> str:
        """Format the header block with VM info."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        vm_status = diagnosis['status']
        os_line = self._format_os_line(diagnosis)
        header = (
            f"Diagnosis: {vm_name} ({zone})\n"
            f"Status:    {vm_status}"
        )
        if os_line:
            header += f"\n{os_line}"
        return header

    def _format_os_line(self, diagnosis: Dict[str, Any]) -> str:
        """Format the OS info line.

        Returns empty string if no OS info is available.
        """
        os_type = diagnosis.get('os_type', 'unknown')
        os_flavor = diagnosis.get('os_flavor', 'unknown')
        architecture = diagnosis.get('architecture', 'unknown')
        license_type = diagnosis.get('license_type', 'unknown')

        if os_type == 'unknown':
            return "OS:        Unknown"

        display_name = 'Windows' if os_type == 'windows' else 'Linux'
        details = []
        if os_flavor != 'unknown':
            details.append(os_flavor)
        if architecture != 'unknown':
            details.append(architecture)
        if license_type != 'unknown':
            details.append(license_type.upper() if license_type == 'payg'
                           else license_type.capitalize())

        if details:
            return f"OS:        {display_name} ({', '.join(details)})"
        return f"OS:        {display_name}"

    def _format_result_line(self, diagnosis: Dict[str, Any]) -> str:
        """Format the result summary line with severity counts."""
        errors = diagnosis.get('boot_errors', [])
        total = len(errors)

        if total == 0:
            return f"Result:    {green('No boot issues detected')}"

        counts = {}
        for err in errors:
            sev = err['severity']
            counts[sev] = counts.get(sev, 0) + 1

        # Build severity breakdown
        parts = []
        for sev in ('critical', 'error', 'warning'):
            if sev in counts:
                parts.append(f"{counts[sev]} {sev}")

        issue_word = "issue" if total == 1 else "issues"
        breakdown = ", ".join(parts)
        summary = f"{total} {issue_word} found ({breakdown})"

        return f"Result:    {red(summary)}"

    def _format_errors_report(self, diagnosis: Dict[str, Any],
                              skip_fix_section: bool = False) -> str:
        """Format a report with detected errors."""
        lines = []

        # Header
        lines.append(self._format_header(diagnosis))
        lines.append(self._format_result_line(diagnosis))
        lines.append("")

        # Sort errors by severity
        errors = sorted(
            diagnosis['boot_errors'],
            key=lambda e: SEVERITY_ORDER.get(e['severity'], 99)
        )

        # Format each issue
        for error in errors:
            lines.append(self._format_single_issue(error, diagnosis))

        # Consolidated fix section (omitted when repair command handles it)
        if not skip_fix_section:
            lines.append(self._format_fix_section(diagnosis))

        return "\n".join(lines)

    def _format_single_issue(
        self, error: Dict[str, Any], diagnosis: Dict[str, Any]
    ) -> str:
        """Format a single detected issue."""
        severity = error['severity'].upper()
        category = error['category'].upper()
        description = error['description']
        context = error.get('context_lines', [])
        matched_idx = error.get('matched_line_index', -1)
        fixes = error.get('suggested_fixes', [])

        # Style the severity label (bold, no color)
        sev_label = bold(f"{severity:<9}")

        lines = []

        # Severity + category + description
        lines.append(f"{sev_label} [{category}] {description}")

        indent = " " * 10
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']

        # Show extracted identifier (UUID, device, etc.) when available
        identifier = _extract_identifier(error.get('detected_pattern', ''))
        if identifier:
            lines.append(f"{indent}Identifier: {bold(identifier)}")

        # Serial console log lines
        if context:
            lines.append(f"{indent}Serial console:")
            max_width = 100
            for i, ctx_line in enumerate(context):
                if not ctx_line.strip():
                    continue
                ctx_line = _decode_systemd_escapes(ctx_line)
                if len(ctx_line) > max_width:
                    ctx_line = ctx_line[:max_width] + "..."
                if i == matched_idx:
                    lines.append(f"{indent}  {dim(ctx_line)}  {red('<--')}")
                else:
                    lines.append(f"{indent}  {dim(ctx_line)}")

        # Per-issue fixes moved to _format_fix_section (manual step 2)

        lines.append("")
        return "\n".join(lines)

    def _format_fix_section(self, diagnosis: Dict[str, Any]) -> str:
        """Format the consolidated fix section at the bottom."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        errors = diagnosis.get('boot_errors', [])

        issue_word = "this issue" if len(errors) == 1 else "these issues"
        lines = [f"To fix {issue_word}:", ""]

        # Collect unique categories
        categories = []
        seen = set()
        for err in errors:
            cat = err['category']
            if cat not in seen:
                seen.add(cat)
                categories.append(cat)

        # Detect-only categories (e.g. cpu_lockup) are runtime conditions:
        # there is nothing to edit on the rescued disk, so rescue/restore
        # steps do not apply to them.
        from ..core.fix_catalog import (
            SUPPORTED_FIX_CATEGORIES,
            DETECT_ONLY_CATEGORIES,
        )
        disk_categories = [
            c for c in categories if c not in DETECT_ONLY_CATEGORIES
        ]
        detect_only = [c for c in categories if c in DETECT_ONLY_CATEGORIES]
        disk_errors = [
            e for e in errors if e['category'] not in DETECT_ONLY_CATEGORIES
        ]
        detect_only_errors = [
            e for e in errors if e['category'] in DETECT_ONLY_CATEGORIES
        ]

        # All findings are detect-only: print guidance without the
        # rescue/restore workflow (rescue mode would not help here).
        if not disk_categories:
            lines.extend(self._format_detect_only_steps(
                detect_only, detect_only_errors, vm_name, zone))
            return "\n".join(lines)

        categories = disk_categories
        errors = disk_errors

        # Check if auto-repair is available AND can identify targets
        auto_fixable = [c for c in categories if c in SUPPORTED_FIX_CATEGORIES]

        # Auto-repair needs extractable identifiers to target specific entries.
        # If no error has an extractable identifier, repair would bail out.
        if auto_fixable:
            has_targets = any(
                _extract_identifier(err.get('detected_pattern', ''))
                for err in errors
                if err['category'] in SUPPORTED_FIX_CATEGORIES
            )
            if not has_targets:
                auto_fixable = []

        # Lead with auto-repair when available
        if auto_fixable:
            lines.append("  Auto-repair (recommended):")
            lines.append(f"    $ gce-rescue repair {vm_name} --zone={zone}")
            lines.append("")
            lines.append("  Or fix manually:")

        # Manual steps
        lines.append("    1. Enter rescue mode:")
        lines.append(f"       $ gce-rescue rescue {vm_name} --zone={zone}")

        # Category-aware fix guidance + per-issue fix suggestions
        if len(categories) == 1:
            cat = categories[0]
            guidance = CATEGORY_FIX_GUIDANCE.get(cat)
            if guidance:
                label = _category_label(cat)
                lines.append(f"    2. {label}:")
                lines.append(f"       $ {guidance}")
        else:
            lines.append("    2. Repair boot configuration:")
            for cat in categories:
                guidance = CATEGORY_FIX_GUIDANCE.get(cat)
                if guidance:
                    lines.append(f"       - {guidance}")

        # Add per-issue fix suggestions under step 2
        all_fixes = []
        seen_fixes = set()
        for err in errors:
            for fix in err.get('suggested_fixes', []):
                fix = fix.replace('VM_NAME', vm_name).replace('ZONE', zone)
                if fix not in seen_fixes:
                    seen_fixes.add(fix)
                    all_fixes.append(fix)
        if all_fixes:
            for fix in all_fixes:
                lines.append(f"       - {fix}")

        lines.append("    3. Restore the VM:")
        lines.append(f"       $ gce-rescue restore {vm_name} --zone={zone}")

        # Detect-only findings get their own guidance block, outside the
        # rescue/restore steps.
        if detect_only:
            lines.append("")
            lines.extend(self._format_detect_only_steps(
                detect_only, detect_only_errors, vm_name, zone))

        return "\n".join(lines)

    def _format_detect_only_steps(
        self, categories: List[str], errors: List[Dict[str, Any]],
        vm_name: str, zone: str
    ) -> List[str]:
        """Format guidance for detect-only categories (no rescue workflow).

        Detect-only findings (e.g. CPU lockups) are runtime conditions, so
        the guidance is rendered as plain steps, never as shell commands
        inside a rescue/restore cycle.
        """
        lines: List[str] = []
        for cat in categories:
            lines.append(f"  {_category_label(cat)}:")
            seen = set()
            guidance = CATEGORY_FIX_GUIDANCE.get(cat)
            if guidance:
                guidance = guidance.replace(
                    'VM_NAME', vm_name).replace('ZONE', zone)
                seen.add(guidance)
                lines.append(f"    - {guidance}")
            for err in errors:
                if err['category'] != cat:
                    continue
                for fix in err.get('suggested_fixes', []):
                    fix = fix.replace('VM_NAME', vm_name).replace('ZONE', zone)
                    if fix not in seen:
                        seen.add(fix)
                        lines.append(f"    - {fix}")
        return lines

    def _format_healthy(self, diagnosis: Dict[str, Any]) -> str:
        """Format a healthy VM report."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        project = diagnosis.get('project', '')

        lines = []
        lines.append(self._format_header(diagnosis))
        lines.append(f"Result:    {green('No boot issues detected')}")

        vm_status = diagnosis.get('status', '')
        if vm_status == 'TERMINATED':
            lines.append("")
            lines.append("Note: VM is currently stopped. Start it with:")
            lines.append(
                f"  $ gcloud compute instances start {vm_name} --zone={zone}"
            )

        lines.append("")
        lines.append(dim("Note: Currently checks fstab errors only. "
                         "If the VM still won't boot, check the serial console:"))
        gcloud_cmd = f"  $ gcloud compute instances get-serial-port-output {vm_name} --zone={zone}"
        if project:
            gcloud_cmd += f" --project={project}"
        lines.append(dim(gcloud_cmd))
        if project:
            lines.append(dim(
                f"  Console: https://console.cloud.google.com/compute/instancesDetail"
                f"/zones/{zone}/instances/{vm_name}/console?project={project}&port=1"
            ))

        return "\n".join(lines)

    def _format_unable(self, diagnosis: Dict[str, Any]) -> str:
        """Format an unable-to-diagnose report."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        recommendations = diagnosis.get('recommendations', [])

        lines = []
        lines.append(self._format_header(diagnosis))
        lines.append(f"Result:    {red('Unable to diagnose')}")
        lines.append("")

        # Show the first recommendation as the main error detail
        if recommendations:
            for rec in recommendations:
                rec = rec.replace('VM_NAME', vm_name).replace('ZONE', zone)
                lines.append(rec)
        else:
            lines.append("No diagnostic information available.")

        lines.append("")
        lines.append("Then run diagnosis again:")
        lines.append(f"  $ gce-rescue diagnose {vm_name} --zone={zone}")

        return "\n".join(lines)


def _decode_systemd_escapes(text: str) -> str:
    """Decode systemd hex escape sequences (e.g. \\x2d -> '-') for readability."""
    import re
    return re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), text)


def _extract_identifier(detected_pattern: str) -> Optional[str]:
    """Extract a human-readable identifier (UUID, device, mount) from a detected pattern.

    Returns the first match with a descriptive prefix, or None if nothing found.
    """
    if not detected_pattern:
        return None

    # Decode systemd escapes first (e.g. \x2d -> -)
    decoded = _decode_systemd_escapes(detected_pattern)

    patterns = [
        # UUID= style
        (r'UUID=([\w-]+)', 'UUID={}'),
        # /dev/disk/by-uuid/ style
        (r'/by-uuid/([\w-]+)', 'UUID={}'),
        # systemd escaped: dev-disk-by-uuid-UUID.device
        (r'dev-disk-by-uuid-([\w-]+)\.device', 'UUID={}'),
        # PARTUUID= style
        (r'PARTUUID=([\w-]+)', 'PARTUUID={}'),
        # systemd escaped: dev-disk-by-partuuid-UUID.device
        (r'dev-disk-by-partuuid-([\w-]+)\.device', 'PARTUUID={}'),
        # /dev/sdXN device
        (r'/dev/(sd[a-z]+\d*)', '/dev/{}'),
        # LABEL= style
        (r'LABEL=([\w-]+)', 'LABEL={}'),
        # /dev/disk/by-label/ style
        (r'/by-label/([\w-]+)', 'LABEL={}'),
        # systemd mount unit: xxx.mount -> /xxx
        (r'for ([\w.-]+)\.mount', '/{}'),
    ]

    for regex, fmt in patterns:
        match = re.search(regex, decoded, re.IGNORECASE)
        if match:
            value = match.group(1)
            if fmt == '/{}':
                value = value.replace('-', '/')
            return fmt.format(value)

    return None


def _category_label(category: str) -> str:
    """Return a human-readable label for a fix step based on category."""
    labels = {
        'fstab': 'Fix /etc/fstab',
        'grub': 'Repair GRUB',
        'kernel': 'Check kernel',
        'filesystem': 'Fix filesystem',
        'initramfs': 'Rebuild initramfs',
        'disk_full': 'Free disk space',
        'ssh': 'Fix SSH configuration',
        'cpu_lockup': 'Investigate CPU lockup',
    }
    return labels.get(category, f'Fix {category}')
