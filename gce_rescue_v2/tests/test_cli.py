"""
Unit tests for CLI module.

Tests:
- Argument parsing
- Input validation
- Output formatting
"""

import time
from unittest.mock import Mock

import pytest

from gce_rescue_v2 import cli


class TestCLIArguments:
    """Tests for CLI argument handling."""

    def setup_method(self):
        self.parser = cli.create_parser()

    def test_rescue_requires_vm_name(self):
        """Positional instance name is required."""
        with pytest.raises(SystemExit):
            self.parser.parse_args(["rescue", "--zone", "us-central1-a"])

    def test_rescue_requires_zone(self):
        """Zone flag is required for rescue."""
        with pytest.raises(SystemExit):
            self.parser.parse_args(["rescue", "vm-1"])

    def test_restore_requires_vm_name(self):
        """Restore also requires instance name."""
        with pytest.raises(SystemExit):
            self.parser.parse_args(["restore", "--zone", "us-central1-a"])


class TestCLIExecution:
    """Tests for CLI execution helpers."""

    def test_project_from_gcloud_config(self, monkeypatch):
        """Project should fallback to gcloud config when not provided."""
        parser = cli.create_parser()
        args = parser.parse_args(["rescue", "vm-1", "--zone", "us-central1-a", "--quiet", "--format", "disable"])

        from gce_rescue_v2.cli import preflight

        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "cfg-project")

        # Mock AuthManager
        mock_auth = Mock()
        mock_compute = Mock()
        mock_compute.instances.return_value.get.return_value.execute.return_value = {"disks": []}
        mock_auth.get_client.return_value = (mock_compute, "cfg-project")
        monkeypatch.setattr("gce_rescue_v2.core.auth.AuthManager", lambda: mock_auth)

        # Mock RescueOrchestrator so no real API calls happen
        class FakeOrchestrator:
            def __init__(self, **kwargs):
                self.os_type = 'linux'
                self.snapshot_name = None
                self.verification_succeeded = True
            def validate(self):
                return True
            def execute(self):
                return True

        monkeypatch.setattr(
            "gce_rescue_v2.cli.rescue.RescueOrchestrator", FakeOrchestrator
        )

        exit_code = cli.handle_rescue(args)

        assert exit_code == 0

    def test_handle_restore_failure(self, monkeypatch):
        """Handle restore returning failure sets non-zero exit code."""
        parser = cli.create_parser()
        args = parser.parse_args(["restore", "vm-1", "--zone", "us-central1-a", "--quiet", "--format", "disable"])

        from gce_rescue_v2.cli import preflight

        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "cfg-project")

        # Mock AuthManager
        mock_auth = Mock()
        mock_compute = Mock()
        mock_auth.get_client.return_value = (mock_compute, "cfg-project")
        monkeypatch.setattr("gce_rescue_v2.core.auth.AuthManager", lambda: mock_auth)

        # Mock RestoreOrchestrator to simulate failure
        class FakeOrchestrator:
            def __init__(self, **kwargs):
                self.original_disk_name = None
            def validate(self):
                return True
            def execute(self):
                return False

        monkeypatch.setattr(
            "gce_rescue_v2.cli.restore.RestoreOrchestrator", FakeOrchestrator
        )

        exit_code = cli.handle_restore(args)
        assert exit_code == 1


class TestCLIOutput:
    """Tests for CLI output formatting."""

    def test_table_format(self):
        """Table formatter should include keys and values."""
        data = {"a": 1, "b": "two"}
        out = cli.OutputFormatter.format_output(data, "table")
        assert "a" in out and "b" in out

    def test_json_format(self):
        """JSON formatter should produce JSON string."""
        data = {"a": 1}
        out = cli.OutputFormatter.format_output(data, "json")
        assert out.strip().startswith("{")

    def test_csv_format(self):
        """CSV formatter should include headers and values."""
        data = {"a": 1, "b": 2}
        out = cli.OutputFormatter.format_output(data, "csv")
        assert "a,b" in out
        assert "1,2" in out


# ---------------------------------------------------------------------------
# TestFormatDuration
# ---------------------------------------------------------------------------

class TestFormatDuration:
    """Tests for _format_duration() helper function."""

    def test_format_duration_less_than_60_seconds(self):
        """Seconds less than 60 should format as 'Ns'."""
        assert cli._format_duration(0) == "0s"
        assert cli._format_duration(1) == "1s"
        assert cli._format_duration(42) == "42s"
        assert cli._format_duration(59) == "59s"

    def test_format_duration_exactly_60_seconds(self):
        """60 seconds should format as '1m'."""
        assert cli._format_duration(60) == "1m"

    def test_format_duration_1_minute_30_seconds(self):
        """90 seconds should format as '1m 30s'."""
        assert cli._format_duration(90) == "1m 30s"

    def test_format_duration_multiple_minutes(self):
        """Multiple minutes should format correctly."""
        assert cli._format_duration(120) == "2m"
        assert cli._format_duration(121) == "2m 1s"
        assert cli._format_duration(180) == "3m"
        assert cli._format_duration(200) == "3m 20s"

    def test_format_duration_large_values(self):
        """Large durations should format correctly."""
        assert cli._format_duration(3600) == "60m"
        assert cli._format_duration(3661) == "61m 1s"

    def test_format_duration_float_input(self):
        """Should handle float input (truncates to int)."""
        assert cli._format_duration(42.9) == "42s"
        assert cli._format_duration(60.5) == "1m"
        assert cli._format_duration(90.1) == "1m 30s"

    def test_format_duration_no_trailing_zero_seconds(self):
        """When seconds are 0, should not append ' 0s'."""
        assert cli._format_duration(60) == "1m"
        assert cli._format_duration(120) == "2m"
        assert "0s" not in cli._format_duration(60)


# ---------------------------------------------------------------------------
# TestSpinner
# ---------------------------------------------------------------------------

class TestSpinner:
    """Tests for _Spinner class."""

    def test_spinner_init(self):
        """_Spinner should initialize with a message."""
        spinner = cli._Spinner("Loading")
        assert spinner._message == "Loading"
        assert spinner._stop is False
        assert spinner._thread is None

    def test_spinner_start(self):
        """start() should create and start a daemon thread."""
        spinner = cli._Spinner("Loading")
        spinner.start()
        assert spinner._thread is not None
        assert spinner._thread.daemon is True
        spinner.stop()

    def test_spinner_stop(self):
        """stop() should set _stop flag and wait for thread."""
        spinner = cli._Spinner("Loading")
        spinner.start()
        time.sleep(0.2)  # Let spinner run a bit
        spinner.stop()
        assert spinner._stop is True
        # Thread should have finished
        spinner._thread.join(timeout=1)
        assert not spinner._thread.is_alive()

    def test_spinner_stop_clears_output(self):
        """stop() with clear=True should print clearing characters."""
        import sys
        from io import StringIO

        spinner = cli._Spinner("Loading")
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        try:
            spinner.start()
            time.sleep(0.2)
            spinner.stop(clear=True)
            output = sys.stdout.getvalue()
            # Should contain carriage return for clearing
            assert "\r" in output
        finally:
            sys.stdout = old_stdout

    def test_spinner_stop_no_clear(self):
        """stop() with clear=False should not clear output."""
        spinner = cli._Spinner("Loading")
        spinner.start()
        time.sleep(0.1)
        # Should not raise with clear=False
        spinner.stop(clear=False)
        assert spinner._stop is True

    def test_spinner_multiple_start_stop(self):
        """Multiple start/stop cycles should work."""
        spinner = cli._Spinner("Loading")
        for _ in range(3):
            spinner.start()
            time.sleep(0.1)
            spinner.stop()

    def test_spinner_message_in_output(self):
        """Spinner output should contain the message."""
        import sys
        from io import StringIO

        spinner = cli._Spinner("Checking")
        old_stdout = sys.stdout
        sys.stdout = StringIO()

        try:
            spinner.start()
            time.sleep(0.2)
            spinner.stop(clear=False)
            output = sys.stdout.getvalue()
            assert "Checking" in output
        finally:
            sys.stdout = old_stdout


# ---------------------------------------------------------------------------
# TestShowRepairResults
# ---------------------------------------------------------------------------

class TestShowRepairResults:
    """Tests for _show_repair_results() function."""

    def test_show_repair_results_success(self, capsys):
        """Success status should show fixed count and snapshot."""
        result = {
            'status': 'success',
            'fixed_count': 2,
            'fix_lines': ['Fixed UUID for /data', 'Fixed device /dev/sdb1'],
            'error': None,
            'snapshot_name': 'backup-disk-12345',
            'duration_seconds': 45.5,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert 'Repair results:' in captured.out
        assert '2 issues fixed' in captured.out
        assert 'backup-disk-12345' in captured.out
        assert '45s' in captured.out

    def test_show_repair_results_no_issues(self, capsys):
        """no_issues status should indicate no fix was needed."""
        result = {
            'status': 'no_issues',
            'fixed_count': 0,
            'fix_lines': [],
            'error': None,
            'snapshot_name': 'backup-disk-12345',
            'duration_seconds': 60,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert 'already valid' in captured.out.lower()

    def test_show_repair_results_no_fix_available(self, capsys):
        """no_fix status should indicate no automated fix."""
        result = {
            'status': 'no_fix',
            'fixed_count': 0,
            'fix_lines': [],
            'error': None,
            'snapshot_name': None,
            'duration_seconds': 0,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert 'No automated fix' in captured.out

    def test_show_repair_results_failed(self, capsys):
        """failed status should show error."""
        result = {
            'status': 'failed',
            'fixed_count': 1,
            'fix_lines': ['Fixed one issue'],
            'error': 'Script error: invalid fstab format',
            'snapshot_name': None,
            'duration_seconds': 30,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 1
        captured = capsys.readouterr()
        assert 'Script error' in captured.err

    def test_show_repair_results_mount_failed(self, capsys):
        """mount_failed status should show error and rescue mode message."""
        result = {
            'status': 'mount_failed',
            'fixed_count': 0,
            'fix_lines': [],
            'error': 'Disk mount did not complete',
            'snapshot_name': None,
            'duration_seconds': 45,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 1
        captured = capsys.readouterr()
        assert 'rescue mode' in captured.err.lower()

    def test_show_repair_results_restore_failed(self, capsys):
        """restore_failed status should show restore instructions."""
        result = {
            'status': 'restore_failed',
            'fixed_count': 1,
            'fix_lines': ['Fixed UUID'],
            'error': None,
            'snapshot_name': None,
            'duration_seconds': 60,
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 1
        captured = capsys.readouterr()
        assert 'restore' in captured.err.lower()

    def test_show_repair_results_includes_duration(self, capsys):
        """Results should include formatted duration."""
        result = {
            'status': 'success',
            'fixed_count': 1,
            'fix_lines': ['Fixed'],
            'error': None,
            'snapshot_name': 'backup',
            'duration_seconds': 123,  # 2m 3s
        }
        exit_code = cli._show_repair_results(result, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert '2m 3s' in captured.out

    def test_show_repair_results_single_vs_plural(self, capsys):
        """Should use singular/plural for issue count."""
        result_single = {
            'status': 'success',
            'fixed_count': 1,
            'fix_lines': ['One fix'],
            'error': None,
            'snapshot_name': 'backup',
            'duration_seconds': 30,
        }
        exit_code = cli._show_repair_results(result_single, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert '1 issue fixed' in captured.out

        # Reset capsys
        capsys.readouterr()

        result_plural = {
            'status': 'success',
            'fixed_count': 3,
            'fix_lines': ['Fix 1', 'Fix 2', 'Fix 3'],
            'error': None,
            'snapshot_name': 'backup',
            'duration_seconds': 45,
        }
        exit_code = cli._show_repair_results(result_plural, 'my-vm')
        assert exit_code == 0
        captured = capsys.readouterr()
        assert '3 issues fixed' in captured.out

