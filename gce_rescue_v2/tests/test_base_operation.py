"""
Unit tests for BaseOperation helpers.

Tests:
- _wait_for_operation() method
- _wait_for_status() method
- Timeout handling
- Error handling
"""

import time
from unittest.mock import Mock

import pytest

from gce_rescue_v2.operations.base import BaseOperation


class ConcreteOperation(BaseOperation):
    """Concrete implementation for testing abstract BaseOperation."""

    @property
    def name(self):
        return "Test Operation"

    def execute(self, **kwargs):
        return None

    def rollback(self, rollback_data):
        return True


class TestWaitForOperation:
    """Tests for _wait_for_operation method."""

    def setup_method(self):
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()
        self.op = ConcreteOperation(self.mock_compute, self.project, self.zone, self.logger)

    def test_operation_success(self):
        """Test successful operation completion."""
        zone_ops = self.mock_compute.zoneOperations.return_value
        zone_ops.get.return_value.execute.return_value = {"status": "DONE"}

        result = self.op._wait_for_operation({"name": "op-123"})
        assert result is True

    def test_operation_with_error(self):
        """Test operation that completes with error."""
        zone_ops = self.mock_compute.zoneOperations.return_value
        zone_ops.get.return_value.execute.return_value = {
            "status": "DONE",
            "error": {"errors": [{"message": "Something went wrong"}]},
        }

        result = self.op._wait_for_operation({"name": "op-123"})
        assert result is False

    def test_operation_timeout(self, monkeypatch):
        """Test operation timeout."""
        zone_ops = self.mock_compute.zoneOperations.return_value
        zone_ops.get.return_value.execute.return_value = {"status": "RUNNING"}

        real_time = time.time
        call_count = {"n": 0}

        def mock_time():
            call_count["n"] += 1
            return real_time() + (call_count["n"] * 200)

        monkeypatch.setattr(time, "time", mock_time)

        result = self.op._wait_for_operation({"name": "op-123"}, timeout=5)
        assert result is False

    def test_no_operation_name(self):
        """Test with missing operation name."""
        result = self.op._wait_for_operation({})
        assert result is True

    def test_polling_retries(self):
        """Test that polling retries on transient errors."""
        zone_ops = self.mock_compute.zoneOperations.return_value
        zone_ops.get.return_value.execute.side_effect = [Exception("Transient"), {"status": "DONE"}]

        result = self.op._wait_for_operation({"name": "op-123"})
        assert result is True
        assert zone_ops.get.return_value.execute.call_count == 2


class TestWaitForStatus:
    """Tests for _wait_for_status method."""

    def setup_method(self):
        self.mock_compute = Mock()
        self.op = ConcreteOperation(self.mock_compute, "test-project", "us-central1-a", Mock())

    def test_status_reached_immediately(self):
        """Test when target status is reached on first check."""
        check_func = Mock(return_value="STOPPED")
        result = self.op._wait_for_status(check_func, "STOPPED")
        assert result is True

    def test_status_reached_after_polling(self):
        """Test when status is reached after several polls."""
        check_func = Mock(side_effect=["RUNNING", "STOPPING", "STOPPED"])
        result = self.op._wait_for_status(check_func, "STOPPED")
        assert result is True

    def test_status_timeout(self, monkeypatch):
        """Test timeout when status never reached."""
        check_func = Mock(return_value="RUNNING")  # Never changes

        real_time = time.time
        call_count = {"n": 0}

        def mock_time():
            call_count["n"] += 1
            return real_time() + (call_count["n"] * 200)

        monkeypatch.setattr(time, "time", mock_time)

        result = self.op._wait_for_status(check_func, "STOPPED", timeout=5)
        assert result is False

