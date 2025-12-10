"""
Unit tests for CLI module.

Tests:
- Argument parsing
- Input validation
- Output formatting
"""

from unittest.mock import Mock

import pytest

import cli


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

        monkeypatch.setattr(cli, "get_gcloud_config", lambda key: "cfg-project")
        monkeypatch.setattr(cli, "rescue_vm", lambda **kwargs: True)

        exit_code = cli.handle_rescue(args)

        assert exit_code == 0

    def test_handle_restore_failure(self, monkeypatch):
        """Handle restore returning failure sets non-zero exit code."""
        parser = cli.create_parser()
        args = parser.parse_args(["restore", "vm-1", "--zone", "us-central1-a", "--quiet", "--format", "disable"])

        monkeypatch.setattr(cli, "get_gcloud_config", lambda key: "cfg-project")
        monkeypatch.setattr(cli, "restore_vm", lambda **kwargs: False)

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

