"""
Unit tests for rollback system.

Tests:
- RollbackHandler
- Dependency checking
- Critical failure handling
- State tracking
"""

from unittest.mock import Mock

from gce_rescue_v2.orchestration.rollback import (
    RollbackHandler,
    ROLLBACK_DEPENDENCIES,
    CRITICAL_ROLLBACK_OPS,
)
from gce_rescue_v2.orchestration.state import StateTracker, OperationState


class _FakeOperation:
    """Simple operation stub with configurable rollback outcome."""

    def __init__(self, name, success=True):
        self._name = name
        self.success = success
        self.rollback_called = False

    @property
    def name(self):
        return self._name

    def rollback(self, rollback_data):
        self.rollback_called = True
        return self.success


class TestRollbackHandler:
    """Tests for RollbackHandler."""

    def setup_method(self):
        self.logger = Mock()
        self.handler = RollbackHandler(self.logger)

    def test_rollback_empty(self):
        """Test rollback with no operations to rollback."""
        state_tracker = StateTracker()
        result = self.handler.rollback(state_tracker, {})
        assert result is True

    def test_rollback_single_operation(self):
        """Test rollback of single successful operation."""
        state_tracker = StateTracker()
        state_tracker.add_operation("Op1", success=True, message="ok", rollback_data={"a": 1})

        op = _FakeOperation("Op1", success=True)
        operations_map = {"Op1": op}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is True
        assert op.rollback_called is True

    def test_rollback_multiple_operations_lifo(self):
        """Test rollback in reverse order (LIFO)."""
        call_order = []

        class OrderedOp(_FakeOperation):
            def rollback(self, rollback_data):
                call_order.append(self.name)
                return True

        state_tracker = StateTracker()
        state_tracker.add_operation("First", success=True, message="ok", rollback_data={"a": 1})
        state_tracker.add_operation("Second", success=True, message="ok", rollback_data={"b": 2})

        operations_map = {
            "First": OrderedOp("First"),
            "Second": OrderedOp("Second"),
        }

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is True
        assert call_order == ["Second", "First"]

    def test_rollback_skips_failed_operations(self):
        """Test that failed operations are not rolled back."""
        state_tracker = StateTracker()
        state_tracker.add_operation("SuccessOp", success=True, message="ok", rollback_data={"a": 1})
        state_tracker.add_operation("FailedOp", success=False, message="fail", rollback_data={})

        op = _FakeOperation("SuccessOp", success=True)
        operations_map = {"SuccessOp": op}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is True
        assert op.rollback_called is True


class TestRollbackDependencies:
    """Tests for rollback dependency checking."""

    def setup_method(self):
        self.logger = Mock()
        self.handler = RollbackHandler(self.logger)

    def test_dependency_check_passes(self):
        """Test when dependencies are satisfied."""
        state_tracker = StateTracker()
        state_tracker.add_operation("Detach Boot Disk", True, "ok", rollback_data={"x": 1})
        state_tracker.add_operation("Stop VM", True, "ok", rollback_data={"y": 2})

        op_detach = _FakeOperation("Detach Boot Disk", success=True)
        op_stop = _FakeOperation("Stop VM", success=True)

        operations_map = {"Detach Boot Disk": op_detach, "Stop VM": op_stop}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is True
        assert op_detach.rollback_called is True
        assert op_stop.rollback_called is True

    def test_skip_when_dependency_failed(self):
        """Test skipping rollback when dependency failed."""
        state_tracker = StateTracker()
        # Execution order: Stop VM, Detach Boot Disk (rollback reversed)
        state_tracker.add_operation("Stop VM", True, "ok", rollback_data={"y": 2})
        state_tracker.add_operation("Detach Boot Disk", True, "ok", rollback_data={"x": 1})

        op_detach = _FakeOperation("Detach Boot Disk", success=False)  # rollback will fail
        op_stop = _FakeOperation("Stop VM", success=True)

        operations_map = {"Detach Boot Disk": op_detach, "Stop VM": op_stop}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is False
        assert op_detach.rollback_called is True
        # Stop VM should be skipped because dependency failed
        assert op_stop.rollback_called is False

    def test_critical_failure_handling(self):
        """Test handling of critical rollback failures."""
        state_tracker = StateTracker()
        state_tracker.add_operation("Detach Boot Disk", True, "ok", rollback_data={"x": 1})

        op_detach = _FakeOperation("Detach Boot Disk", success=False)
        operations_map = {"Detach Boot Disk": op_detach}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is False
        assert op_detach.rollback_called is True
        assert "Detach Boot Disk" in self.handler.failed_rollbacks
        assert "Detach Boot Disk" in CRITICAL_ROLLBACK_OPS

    def test_non_critical_failure(self):
        """Test non-critical rollback failure continues processing."""
        state_tracker = StateTracker()
        state_tracker.add_operation("Create Disk", True, "ok", rollback_data={"a": 1})
        state_tracker.add_operation("Stop VM", True, "ok", rollback_data={"b": 2})

        op_create = _FakeOperation("Create Disk", success=False)
        op_stop = _FakeOperation("Stop VM", success=True)

        operations_map = {"Create Disk": op_create, "Stop VM": op_stop}

        result = self.handler.rollback(state_tracker, operations_map)

        assert result is False
        assert op_create.rollback_called is True
        # Stop VM still rolls back even if previous non-critical failed
        assert op_stop.rollback_called is True


class TestStateTracker:
    """Tests for StateTracker."""

    def test_add_operation(self):
        """Test adding operations to tracker."""
        tracker = StateTracker()
        tracker.add_operation("Op1", True, "msg1", rollback_data={"k": "v"})

        assert len(tracker.operations) == 1
        op = tracker.operations[0]
        assert op.operation_name == "Op1"
        assert op.success is True
        assert op.rollback_data == {"k": "v"}

    def test_get_rollback_operations(self):
        """Test getting operations for rollback."""
        tracker = StateTracker()
        tracker.add_operation("Op1", True, "msg1", rollback_data={})
        tracker.add_operation("Op2", False, "fail", rollback_data={})
        tracker.add_operation("Op3", True, "msg3", rollback_data={})

        ops = tracker.get_rollback_operations()
        assert [op.operation_name for op in ops] == ["Op3", "Op1"]

    def test_operation_state_tracking(self):
        """Test OperationState captures correct data."""
        state = OperationState("X", True, "ok", rollback_data={"a": 1}, timestamp="ts")
        assert state.operation_name == "X"
        assert state.success is True
        assert state.message == "ok"
        assert state.rollback_data == {"a": 1}
        assert state.timestamp == "ts"
