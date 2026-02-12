"""Diagnostic report formatter for diagnose-boot command.

Formats diagnosis results into clean, professional CLI output
following gcloud conventions and trivy/kubectl-style design.
"""

from typing import Dict, Any, List
from ..core.boot_patterns import SEVERITY_ORDER, CATEGORY_FIX_GUIDANCE
from .colors import red, yellow, green, bold, dim


class DiagnosisReportFormatter:
    """Formats DiagnosisResult dicts into human-readable CLI reports."""

    def format_report(self, diagnosis: Dict[str, Any]) -> str:
        """Format a complete diagnosis report.

        Args:
            diagnosis: Diagnosis result dict with keys:
                vm_name, zone, status, diagnosis_status,
                boot_errors, recommendations

        Returns:
            Formatted report string
        """
        status = diagnosis.get('diagnosis_status', '')

        if status == 'boot_errors_detected':
            return self._format_errors_report(diagnosis)
        elif status == 'healthy':
            return self._format_healthy(diagnosis)
        else:
            return self._format_unable(diagnosis)

    def _format_header(self, diagnosis: Dict[str, Any]) -> str:
        """Format the 3-line header block."""
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        vm_status = diagnosis['status']
        return (
            f"Diagnosis: {vm_name} ({zone})\n"
            f"Status:    {vm_status}"
        )

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

    def _format_errors_report(self, diagnosis: Dict[str, Any]) -> str:
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

        # Consolidated fix section
        lines.append(self._format_fix_section(diagnosis))

        return "\n".join(lines)

    def _format_single_issue(
        self, error: Dict[str, Any], diagnosis: Dict[str, Any]
    ) -> str:
        """Format a single detected issue."""
        severity = error['severity'].upper()
        category = error['category'].upper()
        description = error['description']
        matched = error.get('detected_pattern', '')
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

        # Matched pattern
        indent = " " * 10
        vm_name = diagnosis['vm_name']
        zone = diagnosis['zone']
        formatted_matched = matched.replace('VM_NAME', vm_name).replace('ZONE', zone)
        lines.append(f"{indent}Matched: {formatted_matched}")

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
        lines.append(f"     $ gce-rescue rescue {vm_name} --zone={zone}")
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
        lines.append(f"     $ gce-rescue restore {vm_name} --zone={zone}")

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
        lines.append(f"  $ gce-rescue diagnose-boot {vm_name} --zone={zone}")

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
