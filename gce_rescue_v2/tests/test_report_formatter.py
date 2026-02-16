"""Tests for DiagnosisReportFormatter."""

import pytest
from unittest.mock import patch
from gce_rescue_v2.utils.report_formatter import DiagnosisReportFormatter


@pytest.fixture
def formatter():
    return DiagnosisReportFormatter()


@pytest.fixture
def healthy_diagnosis():
    return {
        'vm_name': 'test-vm',
        'zone': 'us-central1-a',
        'status': 'RUNNING',
        'os_type': 'linux',
        'os_flavor': 'debian-12',
        'architecture': 'x86_64',
        'license_type': 'free',
        'diagnosis_status': 'healthy',
        'boot_errors': [],
        'recommendations': [
            'No boot errors detected in serial console output',
            'VM appears to be booting normally',
            'VM is currently running',
        ],
    }


@pytest.fixture
def healthy_terminated_diagnosis():
    return {
        'vm_name': 'test-vm',
        'zone': 'us-central1-a',
        'status': 'TERMINATED',
        'os_type': 'linux',
        'os_flavor': 'debian-12',
        'architecture': 'x86_64',
        'license_type': 'free',
        'diagnosis_status': 'healthy',
        'boot_errors': [],
        'recommendations': [
            'No boot errors detected in serial console output',
            'VM appears to be booting normally',
            'Note: VM is currently stopped',
        ],
    }


@pytest.fixture
def single_error_diagnosis():
    return {
        'vm_name': 'test-vm',
        'zone': 'us-central1-a',
        'status': 'TERMINATED',
        'os_type': 'linux',
        'os_flavor': 'debian-12',
        'architecture': 'x86_64',
        'license_type': 'free',
        'diagnosis_status': 'boot_errors_detected',
        'boot_errors': [
            {
                'category': 'fstab',
                'severity': 'critical',
                'description': 'UUID specified in /etc/fstab cannot be found',
                'detected_pattern': 'UUID=abc123-def456 does not exist',
                'suggested_fixes': [
                    "Check /mnt/sysroot/etc/fstab for invalid UUID entries",
                    "Run 'blkid' to see available UUIDs",
                    "Comment out or fix the invalid UUID entry",
                ],
                'context_lines': [
                    'systemd[1]: Starting File System Check...',
                    'UUID=abc123-def456 does not exist',
                    'systemd[1]: Dependency failed for /data.mount',
                ],
                'matched_line_index': 1,
            }
        ],
        'recommendations': [
            'Found 1 boot error(s) in serial console output',
        ],
    }


@pytest.fixture
def multi_error_diagnosis():
    return {
        'vm_name': 'test-vm',
        'zone': 'us-central1-a',
        'status': 'TERMINATED',
        'os_type': 'linux',
        'os_flavor': 'rhel-9',
        'architecture': 'x86_64',
        'license_type': 'payg',
        'diagnosis_status': 'boot_errors_detected',
        'boot_errors': [
            {
                'category': 'fstab',
                'severity': 'warning',
                'description': 'Non-critical mount point failed',
                'detected_pattern': 'mount failed for /opt',
                'suggested_fixes': ['Check /etc/fstab'],
                'context_lines': ['mount failed for /opt'],
                'matched_line_index': 0,
            },
            {
                'category': 'fstab',
                'severity': 'critical',
                'description': 'UUID specified in /etc/fstab cannot be found',
                'detected_pattern': 'UUID=abc123 does not exist',
                'suggested_fixes': [
                    'Check /mnt/sysroot/etc/fstab for invalid UUID entries',
                ],
                'context_lines': ['UUID=abc123 does not exist'],
                'matched_line_index': 0,
            },
            {
                'category': 'filesystem',
                'severity': 'error',
                'description': 'Filesystem corruption detected',
                'detected_pattern': 'UNEXPECTED INCONSISTENCY',
                'suggested_fixes': ['Run fsck'],
                'context_lines': ['UNEXPECTED INCONSISTENCY'],
                'matched_line_index': 0,
            },
        ],
        'recommendations': [],
    }


@pytest.fixture
def unable_diagnosis():
    return {
        'vm_name': 'test-vm',
        'zone': 'us-central1-a',
        'status': 'RUNNING',
        'os_type': 'unknown',
        'os_flavor': 'unknown',
        'architecture': 'unknown',
        'license_type': 'unknown',
        'diagnosis_status': 'unable_to_diagnose',
        'boot_errors': [],
        'recommendations': [
            'Serial console is disabled for this VM',
            'Enable it with: gcloud compute instances add-metadata VM_NAME --metadata serial-port-enable=TRUE',
        ],
    }


class TestHealthyReport:
    """Tests for healthy VM reports."""

    def test_healthy_running_is_compact(self, formatter, healthy_diagnosis):
        """Healthy running VM should have header + result + note."""
        report = formatter.format_report(healthy_diagnosis)
        lines = [l for l in report.split('\n') if l.strip()]
        assert len(lines) == 6

    def test_healthy_contains_header(self, formatter, healthy_diagnosis):
        """Healthy report should have Diagnosis/Status/OS/Result header."""
        report = formatter.format_report(healthy_diagnosis)
        assert 'Diagnosis: test-vm (us-central1-a)' in report
        assert 'Status:    RUNNING' in report
        assert 'OS:        Linux (debian-12, x86_64, Free)' in report
        assert 'No boot issues detected' in report

    def test_healthy_no_borders(self, formatter, healthy_diagnosis):
        """Healthy report should not contain ===== borders."""
        report = formatter.format_report(healthy_diagnosis)
        assert '=====' not in report

    def test_healthy_terminated_shows_start_hint(
        self, formatter, healthy_terminated_diagnosis
    ):
        """Terminated healthy VM should show start command."""
        report = formatter.format_report(healthy_terminated_diagnosis)
        assert 'VM is currently stopped' in report
        assert 'gcloud compute instances start test-vm' in report


class TestErrorsReport:
    """Tests for error report formatting."""

    def test_single_error_header(self, formatter, single_error_diagnosis):
        """Single error report should have correct result line."""
        report = formatter.format_report(single_error_diagnosis)
        assert '1 issue found (1 critical)' in report

    def test_single_error_uses_singular(self, formatter, single_error_diagnosis):
        """Single error should say 'this issue' not 'these issues'."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'To fix this issue:' in report

    def test_multi_error_uses_plural(self, formatter, multi_error_diagnosis):
        """Multiple errors should say 'these issues'."""
        report = formatter.format_report(multi_error_diagnosis)
        assert 'To fix these issues:' in report

    def test_severity_sorting(self, formatter, multi_error_diagnosis):
        """Errors should be sorted: critical first, then error, then warning."""
        report = formatter.format_report(multi_error_diagnosis)
        # Find positions of each severity
        critical_pos = report.find('CRITICAL')
        error_pos = report.find('ERROR')
        warning_pos = report.find('WARNING')
        assert critical_pos < error_pos < warning_pos

    def test_severity_count_breakdown(self, formatter, multi_error_diagnosis):
        """Result line should show severity counts."""
        report = formatter.format_report(multi_error_diagnosis)
        assert '3 issues found (1 critical, 1 error, 1 warning)' in report

    def test_context_arrow_on_matched_line(self, formatter, single_error_diagnosis):
        """The matched line in context should have a <-- arrow."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'UUID=abc123-def456 does not exist  <--' in report

    def test_non_matched_context_no_arrow(self, formatter, single_error_diagnosis):
        """Non-matched context lines should NOT have <-- arrow."""
        report = formatter.format_report(single_error_diagnosis)
        for line in report.split('\n'):
            if 'Starting File System Check' in line:
                assert '<--' not in line

    def test_rescue_command_in_fix_section(self, formatter, single_error_diagnosis):
        """Fix section should contain rescue command exactly once."""
        report = formatter.format_report(single_error_diagnosis)
        rescue_count = report.count('gce-rescue-v2 rescue test-vm')
        assert rescue_count == 1

    def test_restore_command_in_fix_section(self, formatter, single_error_diagnosis):
        """Fix section should contain restore command."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'gce-rescue-v2 restore test-vm --zone=us-central1-a' in report

    def test_no_borders(self, formatter, single_error_diagnosis):
        """Error report should not contain ===== borders."""
        report = formatter.format_report(single_error_diagnosis)
        assert '=====' not in report

    def test_category_label_in_brackets(self, formatter, single_error_diagnosis):
        """Error should show category in brackets like [FSTAB]."""
        report = formatter.format_report(single_error_diagnosis)
        assert '[FSTAB]' in report

    def test_multi_category_fix_section(self, formatter, multi_error_diagnosis):
        """Multiple categories should list each fix guidance."""
        report = formatter.format_report(multi_error_diagnosis)
        assert 'Repair boot configuration:' in report
        assert 'fstab' in report.lower()
        assert 'filesystem' in report.lower()

    def test_serial_console_label(self, formatter, single_error_diagnosis):
        """Context section should be labeled 'Serial console:'."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'Serial console:' in report
        assert 'Context:' not in report


class TestArrowPlacement:
    """Tests that the <-- arrow is placed by index, not substring match."""

    def test_arrow_on_correct_index(self, formatter):
        """Arrow should appear on matched_line_index, not substring match."""
        diagnosis = {
            'vm_name': 'test-vm',
            'zone': 'us-central1-a',
            'status': 'TERMINATED',
            'os_type': 'linux',
            'os_flavor': 'debian-12',
            'architecture': 'x86_64',
            'license_type': 'free',
            'diagnosis_status': 'boot_errors_detected',
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'description': 'Test error',
                    'detected_pattern': 'some regex match',
                    'suggested_fixes': ['Fix it'],
                    'context_lines': [
                        'line before',
                        'the actual matched line from serial',
                        'line after',
                    ],
                    'matched_line_index': 1,
                }
            ],
            'recommendations': [],
        }
        report = formatter.format_report(diagnosis)
        for line in report.split('\n'):
            if 'the actual matched line' in line:
                assert '<--' in line
            if 'line before' in line:
                assert '<--' not in line
            if 'line after' in line:
                assert '<--' not in line

    def test_no_arrow_when_index_missing(self, formatter):
        """No arrow should appear if matched_line_index is -1."""
        diagnosis = {
            'vm_name': 'test-vm',
            'zone': 'us-central1-a',
            'status': 'TERMINATED',
            'os_type': 'linux',
            'os_flavor': 'debian-12',
            'architecture': 'x86_64',
            'license_type': 'free',
            'diagnosis_status': 'boot_errors_detected',
            'boot_errors': [
                {
                    'category': 'fstab',
                    'severity': 'critical',
                    'description': 'Test error',
                    'detected_pattern': 'something',
                    'suggested_fixes': ['Fix it'],
                    'context_lines': ['line A', 'line B'],
                    'matched_line_index': -1,
                }
            ],
            'recommendations': [],
        }
        report = formatter.format_report(diagnosis)
        # No line in Serial console section should have <--
        in_serial = False
        for line in report.split('\n'):
            if 'Serial console:' in line:
                in_serial = True
                continue
            if in_serial and 'Fix:' in line:
                break
            if in_serial:
                assert '<--' not in line


class TestUnableReport:
    """Tests for unable-to-diagnose reports."""

    def test_unable_shows_result(self, formatter, unable_diagnosis):
        """Unable report should show 'Unable to diagnose'."""
        report = formatter.format_report(unable_diagnosis)
        assert 'Unable to diagnose' in report

    def test_unable_shows_recommendations(self, formatter, unable_diagnosis):
        """Unable report should display recommendations."""
        report = formatter.format_report(unable_diagnosis)
        assert 'Serial console is disabled' in report

    def test_unable_placeholder_replacement(self, formatter, unable_diagnosis):
        """VM_NAME placeholder in recommendations should be replaced."""
        report = formatter.format_report(unable_diagnosis)
        assert 'VM_NAME' not in report
        assert 'test-vm' in report

    def test_unable_shows_retry_hint(self, formatter, unable_diagnosis):
        """Unable report should show how to retry diagnosis."""
        report = formatter.format_report(unable_diagnosis)
        assert 'gce-rescue-v2 diagnose test-vm' in report


class TestSerialConsoleStyling:
    """Tests for serial console log line styling."""

    @patch('gce_rescue_v2.utils.colors._is_tty', return_value=True)
    def test_log_lines_are_dimmed_on_tty(
        self, mock_tty, formatter, single_error_diagnosis
    ):
        """Serial console log lines should use dim ANSI code on TTY."""
        report = formatter.format_report(single_error_diagnosis)
        # DIM = \033[2m should appear in the log line area
        assert '\033[2m' in report

    @patch('gce_rescue_v2.utils.colors._is_tty', return_value=False)
    def test_log_lines_plain_when_piped(
        self, mock_tty, formatter, single_error_diagnosis
    ):
        """Serial console log lines should not have ANSI when piped."""
        report = formatter.format_report(single_error_diagnosis)
        assert '\033[2m' not in report


class TestNoAnsiWhenPiped:
    """Tests that ANSI codes are stripped when not a TTY."""

    @patch('gce_rescue_v2.utils.colors._is_tty', return_value=False)
    def test_no_ansi_in_healthy(self, mock_tty, formatter, healthy_diagnosis):
        """No ANSI codes when piped."""
        report = formatter.format_report(healthy_diagnosis)
        assert '\033[' not in report

    @patch('gce_rescue_v2.utils.colors._is_tty', return_value=False)
    def test_no_ansi_in_errors(self, mock_tty, formatter, single_error_diagnosis):
        """No ANSI codes when piped."""
        report = formatter.format_report(single_error_diagnosis)
        assert '\033[' not in report

    @patch('gce_rescue_v2.utils.colors._is_tty', return_value=False)
    def test_no_ansi_in_unable(self, mock_tty, formatter, unable_diagnosis):
        """No ANSI codes when piped."""
        report = formatter.format_report(unable_diagnosis)
        assert '\033[' not in report


class TestSkipFixSection:
    """Tests for skip_fix_section parameter to skip manual fix guidance."""

    def test_skip_fix_section_true_omits_guidance(
        self, formatter, single_error_diagnosis
    ):
        """When skip_fix_section=True, 'To fix' section should be omitted."""
        report = formatter.format_report(
            single_error_diagnosis, skip_fix_section=True
        )
        assert 'To fix this issue:' not in report
        assert 'gce-rescue-v2 rescue' not in report
        assert 'gce-rescue-v2 restore' not in report

    def test_skip_fix_section_true_keeps_errors(
        self, formatter, single_error_diagnosis
    ):
        """Even with skip_fix_section=True, error details should remain."""
        report = formatter.format_report(
            single_error_diagnosis, skip_fix_section=True
        )
        # Error details should still be there
        assert 'UUID=abc123-def456 does not exist' in report
        assert '[FSTAB]' in report
        assert 'CRITICAL' in report

    def test_skip_fix_section_false_includes_guidance(
        self, formatter, single_error_diagnosis
    ):
        """When skip_fix_section=False (default), fix section should be present."""
        report = formatter.format_report(
            single_error_diagnosis, skip_fix_section=False
        )
        assert 'To fix this issue:' in report
        assert 'gce-rescue-v2 rescue' in report

    def test_skip_fix_section_default_includes_guidance(
        self, formatter, single_error_diagnosis
    ):
        """Default behavior should include fix section."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'To fix this issue:' in report

    def test_skip_fix_section_multi_error(
        self, formatter, multi_error_diagnosis
    ):
        """skip_fix_section should work with multiple errors."""
        report_with = formatter.format_report(
            multi_error_diagnosis, skip_fix_section=False
        )
        report_without = formatter.format_report(
            multi_error_diagnosis, skip_fix_section=True
        )
        assert 'To fix these issues:' in report_with
        assert 'To fix these issues:' not in report_without


class TestAutoRepairSuggestion:
    """Tests for 'Or auto-repair' suggestion when categories support auto-fix."""

    def test_auto_repair_shown_for_fstab(
        self, formatter, single_error_diagnosis
    ):
        """When fstab error is detected, auto-repair suggestion should appear."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'Or auto-repair:' in report
        assert 'gce-rescue-v2 repair' in report

    def test_auto_repair_shown_for_multi_error_with_fstab(
        self, formatter, multi_error_diagnosis
    ):
        """When any fixable category is present, auto-repair should appear."""
        report = formatter.format_report(multi_error_diagnosis)
        assert 'Or auto-repair:' in report
        assert 'gce-rescue-v2 repair' in report

    def test_no_auto_repair_for_unfixable_only(self, formatter):
        """When no fixable categories, auto-repair should not appear."""
        diagnosis = {
            'vm_name': 'test-vm',
            'zone': 'us-central1-a',
            'status': 'TERMINATED',
            'os_type': 'linux',
            'os_flavor': 'debian-12',
            'architecture': 'x86_64',
            'license_type': 'free',
            'diagnosis_status': 'boot_errors_detected',
            'boot_errors': [
                {
                    'category': 'grub',
                    'severity': 'critical',
                    'description': 'GRUB missing',
                    'detected_pattern': 'grub pattern',
                    'suggested_fixes': ['Fix GRUB'],
                    'context_lines': ['grub error'],
                    'matched_line_index': 0,
                }
            ],
            'recommendations': [],
        }
        report = formatter.format_report(diagnosis)
        assert 'Or auto-repair:' not in report
        assert 'gce-rescue-v2 repair' not in report

    def test_auto_repair_line_has_vm_name(
        self, formatter, single_error_diagnosis
    ):
        """Auto-repair suggestion should include the VM name."""
        report = formatter.format_report(single_error_diagnosis)
        assert 'gce-rescue-v2 repair test-vm' in report

    def test_auto_repair_line_has_zone(
        self, formatter, single_error_diagnosis
    ):
        """Auto-repair suggestion should include the zone."""
        report = formatter.format_report(single_error_diagnosis)
        assert '--zone=us-central1-a' in report
