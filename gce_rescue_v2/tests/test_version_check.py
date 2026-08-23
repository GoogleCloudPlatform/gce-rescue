"""
Unit tests for the PyPI Version Check Utility.

Tests:
- Version comparison (newer available vs up to date)
- Cache hit and cache miss behavior with TTL
- Silent network failure handling
- Opt-out via environment variable, --quiet flag, and non-text --format flags
- Background VersionChecker integration and notice display
- CLI main integration
"""

import io
import json
import os
import time
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest

from gce_rescue_v2 import cli
from gce_rescue_v2.core.config import VERSION
from gce_rescue_v2.utils import version_check
from gce_rescue_v2.utils.version_check import (
    VersionChecker,
    fetch_pypi_version,
    get_cached_version,
    get_latest_version,
    is_newer_version,
    is_version_check_disabled,
    save_cached_version,
)


class TestVersionComparison:
    """Tests for semantic version comparison logic."""

    def test_newer_version_available(self):
        """Should return True when latest version is greater than current."""
        assert is_newer_version("99.99.99", "1.0.0") is True
        assert is_newer_version("2.2.0", "2.1.0") is True
        assert is_newer_version("2.1.1", "2.1.0") is True

    def test_up_to_date_same_version(self):
        """Should return False when latest version equals current."""
        assert is_newer_version("2.1.0", "2.1.0") is False
        assert is_newer_version(VERSION, VERSION) is False

    def test_older_version(self):
        """Should return False when latest version is older than current."""
        assert is_newer_version("1.0.0", "2.1.0") is False
        assert is_newer_version("2.0.9", "2.1.0") is False

    def test_invalid_or_empty_version_strings(self):
        """Should handle empty or invalid version strings gracefully without errors."""
        assert is_newer_version("", "2.1.0") is False
        assert is_newer_version(None, "2.1.0") is False
        assert is_newer_version("2.2.0", "") is False

    def test_fallback_semver(self, monkeypatch):
        """Test fallback comparison when packaging is not imported or fails."""
        with patch("builtins.__import__", side_effect=ImportError):
            assert is_newer_version("2.2.0", "2.1.0") is True
            assert is_newer_version("2.1.0", "2.1.0") is False
            assert is_newer_version("2.0.0", "2.1.0") is False


class TestOptOutAndSuppression:
    """Tests for suppressing version check via env var and CLI flags."""

    def test_opt_out_env_var_enabled(self, monkeypatch):
        """Setting GCE_RESCUE_DISABLE_VERSION_CHECK=1 should disable checks."""
        monkeypatch.setenv("GCE_RESCUE_DISABLE_VERSION_CHECK", "1")
        assert is_version_check_disabled() is True

        monkeypatch.setenv("GCE_RESCUE_DISABLE_VERSION_CHECK", "true")
        assert is_version_check_disabled() is True

    def test_opt_out_env_var_disabled_or_unset(self, monkeypatch):
        """When env var is unset or 0, check should not be disabled."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        assert is_version_check_disabled() is False

        monkeypatch.setenv("GCE_RESCUE_DISABLE_VERSION_CHECK", "0")
        assert is_version_check_disabled() is False

        monkeypatch.setenv("GCE_RESCUE_DISABLE_VERSION_CHECK", "false")
        assert is_version_check_disabled() is False

    def test_quiet_flag_suppresses_check(self, monkeypatch):
        """When --quiet flag is set, version check should be suppressed."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        args = Mock(quiet=True, format="disable")
        assert is_version_check_disabled(args) is True

    def test_format_flags_suppress_check(self, monkeypatch):
        """Non-text formats (json, yaml, none, value) should suppress notice."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        for fmt in ["json", "yaml", "none", "value(vmName)", "JSON", "YAML"]:
            args = Mock(quiet=False, format=fmt)
            assert is_version_check_disabled(args) is True

    def test_format_table_or_disable_allows_check(self, monkeypatch):
        """Standard text formats (table, disable) should allow check."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        for fmt in ["table", "disable", "TABLE"]:
            args = Mock(quiet=False, format=fmt)
            assert is_version_check_disabled(args) is False


class TestCachingAndPyPIFetch:
    """Tests for cache hits, cache misses, expiration, and network failures."""

    def test_cache_hit(self, tmp_path, monkeypatch):
        """Should return cached version if file exists and is within TTL."""
        cache_file = tmp_path / "version-check.json"
        save_cached_version("2.9.0", cache_path=cache_file)

        with patch("urllib.request.urlopen") as mock_urlopen:
            ver = get_latest_version(cache_path=cache_file)
            assert ver == "2.9.0"
            mock_urlopen.assert_not_called()

    def test_cache_miss_fetches_pypi_and_caches(self, tmp_path):
        """On cache miss, should hit PyPI and save the result in cache."""
        cache_file = tmp_path / "version-check.json"
        assert not cache_file.exists()

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"info": {"version": "3.1.0"}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            ver = get_latest_version(cache_path=cache_file)
            assert ver == "3.1.0"
            mock_urlopen.assert_called_once()
            assert cache_file.exists()

            # Verify saved content
            cached = get_cached_version(cache_file)
            assert cached == "3.1.0"

    def test_cache_expired_fetches_pypi(self, tmp_path):
        """Expired cache (>24h) should trigger a PyPI re-fetch."""
        cache_file = tmp_path / "version-check.json"
        # Write cache dated 25 hours ago
        expired_data = {
            "latest_version": "2.1.0",
            "last_checked": time.time() - (25 * 3600)
        }
        cache_file.write_text(json.dumps(expired_data), encoding="utf-8")

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"info": {"version": "2.5.0"}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            ver = get_latest_version(cache_path=cache_file)
            assert ver == "2.5.0"
            mock_urlopen.assert_called_once()

    def test_network_failure_is_silent(self, tmp_path):
        """Network failure on cache miss should silently return None."""
        cache_file = tmp_path / "version-check.json"

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Timeout")):
            ver = get_latest_version(cache_path=cache_file)
            assert ver is None
            assert not cache_file.exists()

    def test_corrupt_cache_is_handled_silently(self, tmp_path):
        """Corrupt JSON in cache file should be ignored silently."""
        cache_file = tmp_path / "version-check.json"
        cache_file.write_text("{not-valid-json", encoding="utf-8")

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Offline")):
            ver = get_cached_version(cache_file)
            assert ver is None
            latest = get_latest_version(cache_file)
            assert latest is None


class TestVersionChecker:
    """Tests for background thread runner and notification display."""

    def test_checker_newer_available(self, tmp_path, monkeypatch):
        """Should print upgrade notice when a newer version is found."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        cache_file = tmp_path / "version-check.json"
        save_cached_version("99.0.0", cache_path=cache_file)

        args = Mock(quiet=False, format="disable")
        checker = VersionChecker(args=args, cache_path=cache_file)
        checker.start()

        stream = io.StringIO()
        checker.display_notice(stream=stream)
        output = stream.getvalue()

        assert "A new version of gce-rescue (99.0.0) is available." in output
        assert f"You are running {VERSION}." in output
        assert "To upgrade: pip install --upgrade gce-rescue" in output

    def test_checker_up_to_date_no_notice(self, tmp_path, monkeypatch):
        """Should print nothing when running version is up to date."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        cache_file = tmp_path / "version-check.json"
        save_cached_version(VERSION, cache_path=cache_file)

        args = Mock(quiet=False, format="disable")
        checker = VersionChecker(args=args, cache_path=cache_file)
        checker.start()

        stream = io.StringIO()
        checker.display_notice(stream=stream)
        assert stream.getvalue() == ""

    def test_checker_network_failure_no_notice(self, tmp_path, monkeypatch):
        """Should print nothing when PyPI lookup fails on cache miss."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        cache_file = tmp_path / "version-check.json"

        args = Mock(quiet=False, format="disable")
        with patch("urllib.request.urlopen", side_effect=OSError("Offline")):
            checker = VersionChecker(args=args, cache_path=cache_file)
            checker.start()

            stream = io.StringIO()
            checker.display_notice(stream=stream)
            assert stream.getvalue() == ""

    def test_checker_opt_out_env(self, tmp_path, monkeypatch):
        """Should skip thread and notice when opt-out env var is set."""
        monkeypatch.setenv("GCE_RESCUE_DISABLE_VERSION_CHECK", "1")
        cache_file = tmp_path / "version-check.json"
        save_cached_version("99.0.0", cache_path=cache_file)

        args = Mock(quiet=False, format="disable")
        checker = VersionChecker(args=args, cache_path=cache_file)
        checker.start()

        assert checker.started is False
        stream = io.StringIO()
        checker.display_notice(stream=stream)
        assert stream.getvalue() == ""

    def test_checker_quiet_flag(self, tmp_path, monkeypatch):
        """Should skip thread and notice when --quiet is set."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        cache_file = tmp_path / "version-check.json"
        save_cached_version("99.0.0", cache_path=cache_file)

        args = Mock(quiet=True, format="disable")
        checker = VersionChecker(args=args, cache_path=cache_file)
        checker.start()

        assert checker.started is False

    def test_checker_format_flag(self, tmp_path, monkeypatch):
        """Should skip thread and notice when machine-parseable format is set."""
        monkeypatch.delenv("GCE_RESCUE_DISABLE_VERSION_CHECK", raising=False)
        cache_file = tmp_path / "version-check.json"
        save_cached_version("99.0.0", cache_path=cache_file)

        args = Mock(quiet=False, format="json")
        checker = VersionChecker(args=args, cache_path=cache_file)
        checker.start()

        assert checker.started is False


class TestCLIIntegration:
    """Tests for integration into main CLI command lifecycle."""

    def test_cli_main_runs_version_check(self, monkeypatch):
        """VersionChecker should be initialized and invoked in CLI main."""
        mock_checker_instance = MagicMock()
        mock_checker_class = MagicMock(return_value=mock_checker_instance)
        monkeypatch.setattr(version_check, "VersionChecker", mock_checker_class)
        monkeypatch.setattr(cli, "VersionChecker", mock_checker_class)

        # Mock parse_args and command handler
        parser_mock = MagicMock()
        args_mock = Mock(command="diagnose", quiet=False, format="disable",
                         project="test-proj", verbosity="info")
        parser_mock.parse_args.return_value = args_mock
        monkeypatch.setattr(cli, "create_parser", lambda: parser_mock)
        monkeypatch.setattr(cli, "validate_args", lambda args: True)
        monkeypatch.setattr(cli, "handle_diagnose", lambda args: 0)

        # Suppress footer output during test
        monkeypatch.setattr(cli, "_print_support_footer", lambda exit_code: None)

        exit_code = cli.main()
        assert exit_code == 0
        mock_checker_class.assert_called_once_with(args_mock)
        mock_checker_instance.start.assert_called_once()
        mock_checker_instance.display_notice.assert_called_once()

    @pytest.mark.parametrize("command", ["rescue", "restore", "diagnose", "repair"])
    def test_cli_main_covers_all_commands(self, command, monkeypatch):
        """Version check should run at the end of rescue, restore, diagnose, and repair."""
        mock_checker_instance = MagicMock()
        mock_checker_class = MagicMock(return_value=mock_checker_instance)
        monkeypatch.setattr(version_check, "VersionChecker", mock_checker_class)
        monkeypatch.setattr(cli, "VersionChecker", mock_checker_class)

        parser_mock = MagicMock()
        args_mock = Mock(command=command, quiet=False, format="disable",
                         project="test-proj", verbosity="info")
        parser_mock.parse_args.return_value = args_mock
        monkeypatch.setattr(cli, "create_parser", lambda: parser_mock)
        monkeypatch.setattr(cli, "validate_args", lambda args: True)
        monkeypatch.setattr(cli, f"handle_{command}", lambda args: 0)
        monkeypatch.setattr(cli, "_print_support_footer", lambda exit_code: None)

        exit_code = cli.main()
        assert exit_code == 0
        mock_checker_class.assert_called_once_with(args_mock)
        mock_checker_instance.start.assert_called_once()
        mock_checker_instance.display_notice.assert_called_once()
