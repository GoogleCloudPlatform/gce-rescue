"""
Unit tests for CLI module.

Tests:
- Argument parsing
- Input validation
- Output formatting
- Command handlers (diagnose, rescue, restore, repair)
"""

import time
from unittest.mock import Mock

import pytest

from gce_rescue_v2 import cli
from gce_rescue_v2.operations.base import OperationResult


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


# ---------------------------------------------------------------------------
# CLI Handler Tests
# ---------------------------------------------------------------------------

def _mock_auth(monkeypatch, project="test-proj"):
    """Set up auth mocks that return a mock compute client."""
    mock_auth = Mock()
    mock_compute = Mock()
    mock_auth.get_client.return_value = (mock_compute, project)
    monkeypatch.setattr("gce_rescue_v2.core.auth.AuthManager", lambda: mock_auth)
    return mock_compute


def _parse_args(command, vm="vm-1", zone="us-central1-a", extra=None):
    """Parse CLI args for a given command with common defaults."""
    parser = cli.create_parser()
    cmd = [command, vm, "--zone", zone, "--project", "test-proj", "--quiet", "--format", "disable"]
    if extra:
        cmd.extend(extra)
    return parser.parse_args(cmd)


class TestHandleDiagnose:
    """Tests for handle_diagnose CLI handler."""

    def _setup_diagnose(self, monkeypatch, boot_errors=None, boot_status="healthy"):
        """Common setup for diagnose handler tests."""
        from gce_rescue_v2.cli import preflight, diagnose as diagnose_mod
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        mock_compute = _mock_auth(monkeypatch)
        monkeypatch.setattr(preflight, "_create_tracked_client", lambda c, label: mock_compute)

        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            "status": "RUNNING",
            "disks": [{"boot": True, "source": "disk", "deviceName": "sda"}],
            "metadata": {"items": []},
        }

        # Mock ValidationRunner at source module (it's a late import in handle_diagnose)
        import gce_rescue_v2.validators as validators_mod
        _OrigRunner = validators_mod.ValidationRunner

        class FakeValidationRunner:
            def __init__(self):
                self._validators = []

            def add(self, v):
                self._validators.append(v)

            def run_all(self, logger=None):
                return Mock(all_passed=Mock(return_value=True))

        monkeypatch.setattr(validators_mod, "ValidationRunner", FakeValidationRunner)

        # Mock DiagnoseOperation at source module (also late import)
        errors = boot_errors if boot_errors is not None else []
        import gce_rescue_v2.operations as ops_mod
        import gce_rescue_v2.operations.diagnose as diagnose_op_mod

        class FakeDiagnose:
            def __init__(self, *args, **kwargs):
                pass

            def execute(self, vm_name, tracking_label=None, stabilize=False):
                diag_status = "healthy" if not errors else "boot_errors_detected"
                return OperationResult(
                    operation_name="Diagnose", success=True, message="OK",
                    rollback_data={
                        "vm_name": vm_name, "instance_name": vm_name,
                        "zone": "us-central1-a", "status": "RUNNING",
                        "boot_errors": errors, "boot_status": boot_status,
                        "diagnosis_status": diag_status,
                        "serial_console_enabled": True,
                        "os_type": "linux", "os_flavor": "debian",
                        "architecture": "x86_64",
                        "recommendations": [],
                    },
                )

        monkeypatch.setattr(ops_mod, "DiagnoseOperation", FakeDiagnose)
        monkeypatch.setattr(diagnose_op_mod, "DiagnoseOperation", FakeDiagnose)

    def test_handle_diagnose_success(self, monkeypatch):
        """Diagnose runs, shows results, returns 0."""
        self._setup_diagnose(monkeypatch, boot_errors=[
            {"category": "fstab", "severity": "error",
             "description": "Bad UUID", "detected_pattern": "UUID=xxx"}
        ], boot_status="error")

        args = _parse_args("diagnose")
        exit_code = cli.handle_diagnose(args)
        assert exit_code == 0

    def test_handle_diagnose_no_errors(self, monkeypatch):
        """Healthy VM with no boot errors returns 0."""
        self._setup_diagnose(monkeypatch, boot_errors=[], boot_status="healthy")

        args = _parse_args("diagnose")
        exit_code = cli.handle_diagnose(args)
        assert exit_code == 0

    def test_handle_diagnose_auth_error(self, monkeypatch):
        """Auth failure returns 1."""
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        monkeypatch.setattr(
            "gce_rescue_v2.core.auth.AuthManager",
            lambda: Mock(get_client=Mock(side_effect=Exception("bad creds"))),
        )

        args = _parse_args("diagnose")
        exit_code = cli.handle_diagnose(args)
        assert exit_code == 1


class TestHandleRescue:
    """Tests for handle_rescue CLI handler."""

    def _setup_rescue(self, monkeypatch, validate=True, execute=True):
        """Common setup for rescue handler tests."""
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        mock_compute = _mock_auth(monkeypatch)

        # Mock preflight._validate_vm_exists
        monkeypatch.setattr(
            preflight, "_validate_vm_exists",
            lambda c, p, z, vm, user_agent=None: (True, {"disks": [], "status": "RUNNING", "metadata": {"items": []}}, None),
        )
        monkeypatch.setattr(preflight, "_check_local_ssds", lambda vm: [])

        class FakeOrchestrator:
            def __init__(self, **kwargs):
                self.os_type = 'linux'
                self.snapshot_name = None
                self.verification_succeeded = True

            def validate(self):
                return validate

            def execute(self):
                return execute

        monkeypatch.setattr("gce_rescue_v2.cli.rescue.RescueOrchestrator", FakeOrchestrator)

    def test_handle_rescue_success(self, monkeypatch):
        """Rescue runs, shows instructions, returns 0."""
        self._setup_rescue(monkeypatch)
        args = _parse_args("rescue")
        exit_code = cli.handle_rescue(args)
        assert exit_code == 0

    def test_handle_rescue_validation_fails(self, monkeypatch):
        """Validation failure returns 1."""
        self._setup_rescue(monkeypatch, validate=False)
        args = _parse_args("rescue")
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1

    def test_handle_rescue_execute_fails(self, monkeypatch):
        """Orchestrator execute failure returns 1."""
        self._setup_rescue(monkeypatch, execute=False)
        args = _parse_args("rescue")
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1

    def _setup_rescue_with_image(self, monkeypatch, validate_returns=(50, None)):
        """Variant of _setup_rescue with mockable image pre-flight outcome.

        validate_returns: (size_gb, error_message) tuple returned by the
            mocked preflight.validate_custom_rescue_image helper.
        """
        captured_config = {}

        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")
        _mock_auth(monkeypatch)
        monkeypatch.setattr(
            preflight, "_validate_vm_exists",
            lambda c, p, z, vm, user_agent=None: (
                True,
                {"disks": [{"boot": True, "licenses": ["debian-12"]}],
                 "status": "RUNNING", "metadata": {"items": []}},
                None,
            ),
        )
        monkeypatch.setattr(preflight, "_check_local_ssds", lambda vm: [])
        # Mock the shared image pre-flight helper directly (the unit under
        # test here is the CLI handler, not the helper).
        monkeypatch.setattr(
            preflight, "validate_custom_rescue_image",
            lambda *a, **kw: validate_returns,
        )

        class FakeOrchestrator:
            def __init__(self, **kwargs):
                captured_config['config'] = kwargs.get('config')
                self.os_type = 'linux'
                self.snapshot_name = None
                self.verification_succeeded = True
            def validate(self): return True
            def execute(self): return True

        monkeypatch.setattr(
            "gce_rescue_v2.cli.rescue.RescueOrchestrator", FakeOrchestrator
        )
        return captured_config

    def test_handle_rescue_invalid_rescue_image_url(self, monkeypatch, capsys):
        """Bad --rescue-image URL is rejected with clean error, exit 1."""
        self._setup_rescue_with_image(
            monkeypatch,
            validate_returns=(None, "Unrecognized rescue image URL format: oops"),
        )
        args = _parse_args("rescue", extra=["--rescue-image", "oops"])
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1
        assert "Unrecognized rescue image URL format" in capsys.readouterr().err

    def test_handle_rescue_rescue_image_not_found(self, monkeypatch, capsys):
        """Non-existent rescue image (404) shows clean error, exit 1."""
        self._setup_rescue_with_image(
            monkeypatch,
            validate_returns=(None, "Rescue image not found: projects/my-proj/global/images/does-not-exist"),
        )
        args = _parse_args(
            "rescue",
            extra=["--rescue-image", "projects/my-proj/global/images/does-not-exist"],
        )
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1
        assert "not found" in capsys.readouterr().err.lower()

    def test_handle_rescue_resolved_size_flows_to_config(self, monkeypatch):
        """Successful image pre-check passes resolved size into RescueConfig."""
        captured_config = self._setup_rescue_with_image(
            monkeypatch, validate_returns=(50, None),
        )
        args = _parse_args(
            "rescue",
            extra=["--rescue-image", "projects/debian-cloud/global/images/family/debian-12"],
        )
        exit_code = cli.handle_rescue(args)
        assert exit_code == 0
        assert captured_config['config'].custom_rescue_image_size_gb == 50

    def test_handle_rescue_blocks_linux_vm_windows_image(self, monkeypatch, capsys):
        """Linux VM + Windows custom image is rejected (issue #101)."""
        self._setup_rescue_with_image(
            monkeypatch,
            validate_returns=(None,
                "--rescue-image OS mismatch: VM is linux, but image is windows.\n"
                "      Rescue image OS must match the VM's OS family."),
        )
        args = _parse_args(
            "rescue",
            extra=["--rescue-image", "projects/windows-cloud/global/images/family/windows-2019"],
        )
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "OS mismatch" in err
        assert "linux" in err.lower() and "windows" in err.lower()

    def test_handle_rescue_blocks_arch_mismatch(self, monkeypatch, capsys):
        """x86 VM + ARM64 custom image is rejected (issue #101)."""
        self._setup_rescue_with_image(
            monkeypatch,
            validate_returns=(None,
                "--rescue-image architecture mismatch: VM is x86_64, but image is arm64.\n"
                "      Rescue image architecture must match the VM's."),
        )
        args = _parse_args(
            "rescue",
            extra=["--rescue-image", "projects/debian-cloud/global/images/family/debian-12-arm64"],
        )
        exit_code = cli.handle_rescue(args)
        assert exit_code == 1
        err = capsys.readouterr().err
        assert "architecture mismatch" in err
        assert "x86_64" in err and "arm64" in err


class TestHandleRestore:
    """Tests for handle_restore CLI handler."""

    def _setup_restore(self, monkeypatch, validate=True, execute=True):
        """Common setup for restore handler tests."""
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        mock_compute = _mock_auth(monkeypatch)

        monkeypatch.setattr(
            preflight, "_validate_vm_for_restore",
            lambda c, p, z, vm, user_agent=None: (True, {"disks": [], "metadata": {"items": [{"key": "rescue-mode", "value": "1"}]}}, None),
        )

        class FakeOrchestrator:
            def __init__(self, **kwargs):
                self.original_disk_name = "orig-disk"

            def validate(self):
                return validate

            def execute(self):
                return execute

        monkeypatch.setattr("gce_rescue_v2.cli.restore.RestoreOrchestrator", FakeOrchestrator)

        # Mock snapshot lookup after restore
        mock_compute.snapshots.return_value.list.return_value.execute.return_value = {"items": []}
        # Mock os detection after restore
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            "disks": [{"boot": True, "source": "disk", "deviceName": "sda"}],
            "networkInterfaces": [],
        }

    def test_handle_restore_success(self, monkeypatch):
        """Restore runs, shows confirmation, returns 0."""
        self._setup_restore(monkeypatch)
        args = _parse_args("restore")
        exit_code = cli.handle_restore(args)
        assert exit_code == 0

    def test_handle_restore_validation_fails(self, monkeypatch):
        """Validation failure returns 1."""
        self._setup_restore(monkeypatch, validate=False)
        args = _parse_args("restore")
        exit_code = cli.handle_restore(args)
        assert exit_code == 1

    def test_handle_restore_execute_fails(self, monkeypatch):
        """Orchestrator execute failure returns 1."""
        self._setup_restore(monkeypatch, execute=False)
        args = _parse_args("restore")
        exit_code = cli.handle_restore(args)
        assert exit_code == 1


class TestHandleRepair:
    """Tests for handle_repair CLI handler."""

    def _setup_repair_base(self, monkeypatch):
        """Base setup for repair: auth + preflight."""
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        mock_compute = _mock_auth(monkeypatch)
        monkeypatch.setattr(preflight, "_create_tracked_client", lambda c, label: mock_compute)

        # VM is Linux, RUNNING, not in rescue mode
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            "status": "RUNNING",
            "disks": [{"boot": True, "source": "disk", "deviceName": "sda"}],
            "metadata": {"items": []},
        }

        return mock_compute

    def _make_fake_repair_orch(self, boot_errors=None, fixable=None,
                              unfixable=None, fstab_targets=None,
                              execute_result=None):
        """Create a FakeRepairOrchestrator class with given behaviour."""
        errors = boot_errors or []
        fix_cats = fixable or []
        unfix_cats = unfixable or []
        targets = fstab_targets or []
        exec_res = execute_result or {
            "status": "success", "fixed_count": 1,
            "fix_lines": ["[FIXED] Commented out UUID=bad-uuid"],
            "error": None, "snapshot_name": "snap-123", "duration_seconds": 30,
        }

        class FakeRepairOrchestrator:
            _suppress_header = False

            def __init__(self, **kwargs):
                pass

            def validate(self):
                return True

            def diagnose(self):
                return {
                    "instance_name": "vm-1", "zone": "us-central1-a",
                    "boot_errors": errors,
                    "boot_status": "error" if errors else "healthy",
                }

            def get_fixable_categories(self, diag):
                return fix_cats

            def get_unfixable_categories(self, diag):
                return unfix_cats

            def _extract_fstab_targets(self, diag):
                return targets

            def execute(self, diagnosis):
                return exec_res

        return FakeRepairOrchestrator

    def test_handle_repair_success(self, monkeypatch):
        """Repair runs, shows results, returns 0."""
        self._setup_repair_base(monkeypatch)

        Fake = self._make_fake_repair_orch(
            boot_errors=[{"category": "fstab", "severity": "error",
                          "description": "Bad UUID in /etc/fstab",
                          "detected_pattern": "UUID=bad-uuid"}],
            fixable=["fstab"],
            fstab_targets=["UUID=bad-uuid"],
        )
        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", Fake
        )

        args = _parse_args("repair")
        exit_code = cli.handle_repair(args)
        assert exit_code == 0

    def test_handle_repair_no_issues(self, monkeypatch):
        """No boot errors exits cleanly with message."""
        self._setup_repair_base(monkeypatch)

        Fake = self._make_fake_repair_orch(boot_errors=[])
        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", Fake
        )

        args = _parse_args("repair")
        exit_code = cli.handle_repair(args)
        assert exit_code == 0

    def test_handle_repair_no_fix_available(self, monkeypatch, capsys):
        """Unfixable issues suggests rescue mode, returns 0."""
        self._setup_repair_base(monkeypatch)

        Fake = self._make_fake_repair_orch(
            boot_errors=[{"category": "kernel", "severity": "critical",
                          "description": "Missing kernel"}],
            unfixable=["kernel"],
        )
        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", Fake
        )

        args = _parse_args("repair")
        exit_code = cli.handle_repair(args)
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "rescue" in captured.out.lower()

    def test_handle_repair_auth_error(self, monkeypatch):
        """Auth failure returns 1."""
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")

        monkeypatch.setattr(
            "gce_rescue_v2.core.auth.AuthManager",
            lambda: Mock(get_client=Mock(side_effect=Exception("bad creds"))),
        )

        args = _parse_args("repair")
        exit_code = cli.handle_repair(args)
        assert exit_code == 1

    # --- --rescue-image pre-flight (issue #102) ---

    def test_handle_repair_rescue_image_invalid_blocks_pre_flight(
        self, monkeypatch, capsys,
    ):
        """Bad --rescue-image URL is blocked by shared pre-flight helper."""
        self._setup_repair_base(monkeypatch)
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(
            preflight, "validate_custom_rescue_image",
            lambda *a, **kw: (None, "Unrecognized rescue image URL format: oops"),
        )

        Fake = self._make_fake_repair_orch()
        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", Fake
        )

        args = _parse_args("repair", extra=["--rescue-image", "oops"])
        exit_code = cli.handle_repair(args)
        assert exit_code == 1
        assert "Unrecognized rescue image URL format" in capsys.readouterr().err

    def test_handle_repair_rescue_image_size_mutated_onto_orchestrator(
        self, monkeypatch,
    ):
        """Successful pre-flight mutates orchestrator.config.custom_rescue_image_size_gb."""
        self._setup_repair_base(monkeypatch)
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(
            preflight, "validate_custom_rescue_image",
            lambda *a, **kw: (50, None),  # resolved 50 GB, no error
        )

        captured = {}

        class FakeRepairOrch:
            _suppress_header = False
            def __init__(self, **kwargs):
                # Save instance so we can inspect config mutation
                self.config = kwargs.get('config')
                captured['orch'] = self
            def validate(self): return True
            def diagnose(self):
                return {"instance_name": "vm-1", "zone": "us-central1-a",
                        "boot_errors": [], "boot_status": "healthy"}
            def get_fixable_categories(self, d): return []
            def get_unfixable_categories(self, d): return []
            def _extract_fstab_targets(self, d): return []
            def execute(self, d):
                return {"status": "success", "fixed_count": 0,
                        "fix_lines": [], "error": None,
                        "snapshot_name": "s", "duration_seconds": 1}

        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", FakeRepairOrch
        )

        args = _parse_args(
            "repair",
            extra=["--rescue-image", "projects/debian-cloud/global/images/family/debian-12"],
        )
        exit_code = cli.handle_repair(args)
        assert exit_code == 0
        # Confirm the mutation happened on the orchestrator's config
        assert captured['orch'].config.custom_rescue_image_size_gb == 50


# ---------------------------------------------------------------------------
# TestRescueImageFlag
# ---------------------------------------------------------------------------

class TestRescueImageFlag:
    """Tests for the --rescue-image CLI flag."""

    def setup_method(self):
        self.parser = cli.create_parser()

    # --- Parsing ---

    def test_rescue_image_accepted_by_rescue_command(self):
        """--rescue-image is a valid flag for the rescue subcommand."""
        args = self.parser.parse_args([
            "rescue", "vm-1", "--zone", "us-central1-a",
            "--rescue-image", "projects/my-proj/global/images/my-image",
        ])
        assert args.rescue_image == "projects/my-proj/global/images/my-image"

    def test_rescue_image_accepted_by_repair_command(self):
        """--rescue-image is a valid flag for the repair subcommand."""
        args = self.parser.parse_args([
            "repair", "vm-1", "--zone", "us-central1-a",
            "--rescue-image", "projects/my-proj/global/images/family/debian-11",
        ])
        assert args.rescue_image == "projects/my-proj/global/images/family/debian-11"

    def test_rescue_image_defaults_to_none(self):
        """When --rescue-image is omitted, rescue_image is None."""
        args = self.parser.parse_args(["rescue", "vm-1", "--zone", "us-central1-a"])
        assert args.rescue_image is None

    # --- args_to_rescue_config wiring ---

    def test_rescue_image_populates_config(self):
        """args_to_rescue_config copies --rescue-image into RescueConfig.custom_rescue_image."""
        args = self.parser.parse_args([
            "rescue", "vm-1", "--zone", "us-central1-a",
            "--rescue-image", "projects/my-proj/global/images/my-image",
        ])
        config = cli.args_to_rescue_config(args)
        assert config.custom_rescue_image == "projects/my-proj/global/images/my-image"

    def test_no_rescue_image_leaves_config_none(self):
        """Without the flag, RescueConfig.custom_rescue_image stays None."""
        args = self.parser.parse_args(["rescue", "vm-1", "--zone", "us-central1-a"])
        config = cli.args_to_rescue_config(args)
        assert config.custom_rescue_image is None


class TestFixScriptFlag:
    """Tests for the --fix-script flag wiring on rescue and repair."""

    def setup_method(self):
        self.parser = cli.create_parser()

    def _write_script(self, tmp_path, content="echo fixing\n"):
        script = tmp_path / "fix.sh"
        script.write_text(content)
        return str(script)

    def test_fix_script_parsed_on_rescue(self, tmp_path):
        """rescue accepts --fix-script and stores the path on args."""
        path = self._write_script(tmp_path)
        args = self.parser.parse_args([
            "rescue", "vm-1", "--zone", "us-central1-a", "--fix-script", path,
        ])
        assert args.fix_script == path

    def test_fix_script_parsed_on_repair(self, tmp_path):
        """repair accepts --fix-script and stores the path on args."""
        path = self._write_script(tmp_path)
        args = self.parser.parse_args([
            "repair", "vm-1", "--zone", "us-central1-a", "--fix-script", path,
        ])
        assert args.fix_script == path

    def test_fix_script_content_populates_config(self, tmp_path):
        """args_to_rescue_config reads the file and stores its content on config."""
        path = self._write_script(tmp_path, content="echo hello\n")
        args = self.parser.parse_args([
            "repair", "vm-1", "--zone", "us-central1-a", "--fix-script", path,
        ])
        config = cli.args_to_rescue_config(args)
        assert config.fix_script == "echo hello\n"

    def test_no_fix_script_leaves_config_none(self):
        """Without the flag, RescueConfig.fix_script stays None."""
        args = self.parser.parse_args(["rescue", "vm-1", "--zone", "us-central1-a"])
        config = cli.args_to_rescue_config(args)
        assert config.fix_script is None

    def test_read_fix_script_missing_file_raises(self):
        """read_fix_script raises ValueError for a non-existent path."""
        with pytest.raises(ValueError, match="file not found"):
            cli.read_fix_script("/nonexistent/path/to/fix.sh")

    def test_read_fix_script_empty_file_raises(self, tmp_path):
        """read_fix_script raises ValueError for an empty file."""
        empty = tmp_path / "empty.sh"
        empty.write_text("   \n")
        with pytest.raises(ValueError, match="empty"):
            cli.read_fix_script(str(empty))


class TestFixScriptRepairPath:
    """--fix-script on repair: diagnosis bypass and confirmation flow."""

    SUCCESS_RESULT = {
        "status": "unknown", "fixed_count": 0, "fix_lines": [],
        "error": None, "snapshot_name": "snap-1", "duration_seconds": 10,
        "boot_verified": True, "boot_errors_after": [],
    }

    def _setup_base(self, monkeypatch):
        from gce_rescue_v2.cli import preflight
        monkeypatch.setattr(preflight, "get_gcloud_config", lambda key: "test-proj")
        mock_compute = _mock_auth(monkeypatch)
        monkeypatch.setattr(preflight, "_create_tracked_client",
                            lambda c, label: mock_compute)
        mock_compute.instances.return_value.get.return_value.execute.return_value = {
            "status": "RUNNING",
            "disks": [{"boot": True, "source": "disk", "deviceName": "sda"}],
            "metadata": {"items": []},
        }
        return mock_compute

    def _make_fake_orch(self, result=None):
        calls = {"execute_custom": 0, "diagnose": 0}
        outer_result = result or self.SUCCESS_RESULT

        class FakeOrch:
            _suppress_header = False
            call_log = calls

            def __init__(self, **kwargs):
                self.config = kwargs.get("config")

            def validate(self):
                return True

            def diagnose(self):
                calls["diagnose"] += 1
                raise AssertionError("diagnose must be bypassed with --fix-script")

            def execute_custom(self):
                calls["execute_custom"] += 1
                return outer_result

        return FakeOrch

    def test_repair_fix_script_bypasses_diagnosis(self, monkeypatch, tmp_path):
        """--fix-script runs execute_custom and never calls diagnose."""
        self._setup_base(monkeypatch)
        Fake = self._make_fake_orch()
        monkeypatch.setattr(
            "gce_rescue_v2.orchestration.repair.RepairOrchestrator", Fake
        )

        script = tmp_path / "fix.sh"
        script.write_text("echo custom-fix\n")
        args = _parse_args("repair", extra=["--fix-script", str(script)])

        exit_code = cli.handle_repair(args)
        assert exit_code == 0
        assert Fake.call_log["execute_custom"] == 1
        assert Fake.call_log["diagnose"] == 0

    def test_confirmation_abort_does_not_execute(self, monkeypatch, tmp_path):
        """Answering 'n' at the confirmation never touches the VM."""
        from gce_rescue_v2.cli.repair import _run_custom_fix_script

        orch = Mock()
        args = _parse_args("repair", extra=["--fix-script", "/tmp/fix.sh"])
        args.quiet = False
        monkeypatch.setattr("builtins.input", lambda *a: "n")

        exit_code = _run_custom_fix_script(args, orch, "test-proj",
                                           "echo fix\n")
        assert exit_code == 0
        orch.execute_custom.assert_not_called()

    def test_confirmation_yes_executes(self, monkeypatch, tmp_path):
        """Answering 'y' proceeds to execute_custom."""
        from gce_rescue_v2.cli.repair import _run_custom_fix_script

        orch = Mock()
        orch.execute_custom.return_value = dict(self.SUCCESS_RESULT)
        args = _parse_args("repair", extra=["--fix-script", "/tmp/fix.sh"])
        args.quiet = False
        monkeypatch.setattr("builtins.input", lambda *a: "y")

        exit_code = _run_custom_fix_script(args, orch, "test-proj",
                                           "echo fix\n")
        assert exit_code == 0
        orch.execute_custom.assert_called_once()
        assert orch._suppress_header is True
