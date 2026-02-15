"""Diagnostic report formatter for diagnose command.

Formats diagnosis results into clean, professional CLI output
following gcloud conventions and trivy/kubectl-style design.
"""

from typing import Dict, Any, List
from ..core.boot_patterns import SEVERITY_ORDER, CATEGORY_FIX_GUIDANCE
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

        # Color the severity label
        if severity == 'CRITICAL':
            sev_label = red(f"{'CRITICAL':<9}")
        elif severity == 'ERROR':
            sev_label = red(f"{'ERROR':<9}")
        else:
            sev_label = yellow(f"{'WARNING':<9}")

        lines = []

        # Severity + category + description
        lines.append(f"{sev_label} [{category}] {description}")

        indent = " " * 10
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']

        # Serial console log lines
        if context:
            lines.append(f"{indent}Serial console:")
            for i, ctx_line in enumerate(context):
                if not ctx_line.strip():
                    continue
                if i == matched_idx:
                    lines.append(f"{indent}  {dim(ctx_line)}  <--")
                else:
                    lines.append(f"{indent}  {dim(ctx_line)}")

        # Per-issue fixes
        if fixes:
            lines.append(f"{indent}Fix:")
            for fix in fixes:
                fix = fix.replace('VM_NAME', vm_name).replace('ZONE', zone)
                lines.append(f"{indent}  - {fix}")

        lines.append("")
        return "\n".join(lines)

    def _format_fix_section(self, diagnosis: Dict[str, Any]) -> str:
        """Format the consolidated fix section at the bottom."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        errors = diagnosis.get('boot_errors', [])

        issue_word = "this issue" if len(errors) == 1 else "these issues"
        lines = [f"To fix {issue_word}:", ""]

        # Step 1: Enter rescue mode
        lines.append("  1. Enter rescue mode:")
        lines.append(f"     $ gce-rescue-v2 rescue {vm_name} --zone={zone}")
        lines.append("")

        # Step 2: Category-aware fix guidance
        categories = []
        seen = set()
        for err in errors:
            cat = err['category']
            if cat not in seen:
                seen.add(cat)
                categories.append(cat)

        if len(categories) == 1:
            # Single category - use specific guidance
            cat = categories[0]
            guidance = CATEGORY_FIX_GUIDANCE.get(cat)
            if guidance:
                label = _category_label(cat)
                lines.append(f"  2. {label}:")
                lines.append(f"     $ {guidance}")
        else:
            # Multiple categories - list each
            lines.append("  2. Repair boot configuration:")
            for cat in categories:
                guidance = CATEGORY_FIX_GUIDANCE.get(cat)
                if guidance:
                    lines.append(f"     - {guidance}")

        lines.append("")

        # Step 3: Restore
        lines.append("  3. Restore the VM:")
        lines.append(f"     $ gce-rescue-v2 restore {vm_name} --zone={zone}")

        # Show auto-repair alternative if any category supports it
        from ..orchestration.repair import SUPPORTED_FIX_CATEGORIES
        auto_fixable = [c for c in categories if c in SUPPORTED_FIX_CATEGORIES]
        if auto_fixable:
            lines.append("")
            lines.append("Or auto-repair:")
            lines.append(f"  $ gce-rescue-v2 repair {vm_name} --zone={zone}")

        return "\n".join(lines)

    def _format_healthy(self, diagnosis: Dict[str, Any]) -> str:
        """Format a healthy VM report."""
        lines = []
        lines.append(self._format_header(diagnosis))
        lines.append(f"Result:    {green('No boot issues detected')}")

        vm_status = diagnosis.get('status', '')
        if vm_status == 'TERMINATED':
            vm_name = diagnosis['vm_name']
            zone = diagnosis['zone']
            lines.append("")
            lines.append("Note: VM is currently stopped. Start it with:")
            lines.append(
                f"  $ gcloud compute instances start {vm_name} --zone={zone}"
            )

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
        lines.append(f"  $ gce-rescue-v2 diagnose {vm_name} --zone={zone}")

        return "\n".join(lines)


def _category_label(category: str) -> str:
    """Return a human-readable label for a fix step based on category."""
    labels = {
        'fstab': 'Fix /etc/fstab',
        'grub': 'Repair GRUB',
        'kernel': 'Check kernel',
        'filesystem': 'Fix filesystem',
        'initramfs': 'Rebuild initramfs',
    }
    return labels.get(category, f'Fix {category}')
