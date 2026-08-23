"""
Unit tests for VerifyStartupOperation.

Tests:
- test_marker_found_immediately: Marker found on first poll
- test_marker_found_after_delay: Marker found after several polls
- test_timeout_marker_not_found: Returns failure after timeout
- test_serial_console_disabled: Handles 403 error gracefully
- test_empty_serial_output: Handles empty contents field
- test_tracking_label_used: Verifies tracking label in User-Agent
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from gce_rescue_v2.operations.verify_startup import VerifyStartupOperation


class TestVerifyStartupOperation:

    def setup_method(self):
        """Setup test fixtures."""
        self.mock_compute = Mock()
        self.project = "test-project"
        self.zone = "us-central1-a"
        self.logger = Mock()

        self.operation = VerifyStartupOperation(
            self.mock_compute,
            self.project,
            self.zone,
            self.logger
        )

    def test_marker_found_immediately(self):
        """Test marker found on first poll."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'Boot messages...\nGCE-RESCUE-COMPLETE\nMore output...'
        }

        # Act
        result = self.operation.execute(vm_name='test-vm', timeout=10)

        # Assert
        assert result.success is True
        assert "Startup script completed" in result.message
        assert result.rollback_data is None  # Verification doesn't need rollback

    def test_marker_found_after_delay(self):
        """Test marker found after several polls."""
        # Arrange
        call_count = [0]

        def mock_execute():
            call_count[0] += 1
            if call_count[0] < 3:
                return {'contents': 'Booting...\nStarting services...'}
            else:
                return {'contents': 'Booting...\nStarting services...\nGCE-RESCUE-COMPLETE'}

        self.mock_compute.instances().getSerialPortOutput().execute.side_effect = mock_execute

        # Act
        with patch('time.sleep'):  # Speed up test by mocking sleep
            result = self.operation.execute(vm_name='test-vm', timeout=30)

        # Assert
        assert result.success is True
        assert call_count[0] == 3  # Called 3 times before finding marker
        assert "Startup script completed" in result.message

    def test_timeout_marker_not_found(self):
        """Test timeout when marker is not found."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'Boot messages without completion marker'
        }

        # Act
        with patch('time.sleep'):  # Speed up test
            with patch('time.time', side_effect=[0, 5, 10, 15, 20, 121]):  # Simulate timeout
                result = self.operation.execute(vm_name='test-vm', timeout=120)

        # Assert
        assert result.success is False
        assert "Timeout waiting for startup script" in result.message
        assert "did not complete within" in result.error

    def test_serial_console_disabled(self):
        """Test handling when serial console is disabled (403 error)."""
        # Arrange
        from googleapiclient.errors import HttpError
        resp = Mock(status=403, reason="Forbidden")
        error = HttpError(resp, b'{"error": {"message": "Serial port output is not enabled"}}')

        # Make _create_tracked_client raise the 403 error
        # This will be caught by the outer try-except which checks for 403
        with patch.object(self.operation, '_create_tracked_client') as mock_create:
            mock_create.side_effect = error

            # Act
            result = self.operation.execute(vm_name='test-vm', timeout=10, tracking_label='test')

        # Assert - should return SUCCESS with warning (not failure)
        assert result.success is True
        assert "manual verification required" in result.message.lower()
        assert "serial console disabled" in result.message.lower()

    def test_empty_serial_output(self):
        """Test handling empty serial output."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': ''
        }

        # Act
        with patch('time.sleep'):
            with patch('time.time', side_effect=[0, 5, 121]):  # Quick timeout
                result = self.operation.execute(vm_name='test-vm', timeout=120)

        # Assert
        assert result.success is False
        assert "Timeout" in result.message

    def test_timeout_returns_structured_details(self):
        """On timeout, result carries structured timeout details (issue #132)."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'booting...\nwaiting for disk\n'
        }
        with patch('time.sleep'):
            with patch('time.time', side_effect=[0, 5, 121]):
                result = self.operation.execute(vm_name='test-vm', timeout=120)
        assert result.success is False
        assert result.details is not None
        assert result.details['timed_out'] is True
        assert result.details['timeout_seconds'] == 120

    def test_timeout_captures_serial_tail(self):
        """The last serial output seen is captured in the timeout details."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'booting...\nwaiting for affected disk\n'
        }
        with patch('time.sleep'):
            with patch('time.time', side_effect=[0, 5, 121]):
                result = self.operation.execute(vm_name='test-vm', timeout=120)
        assert 'waiting for affected disk' in result.details['serial_tail']

    def test_timeout_empty_serial_has_empty_tail(self):
        """Empty serial output yields an empty (not missing) tail."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': ''
        }
        with patch('time.sleep'):
            with patch('time.time', side_effect=[0, 5, 121]):
                result = self.operation.execute(vm_name='test-vm', timeout=120)
        assert result.details['serial_tail'] == ''

    def test_success_has_no_timeout_details(self):
        """On success, details stays None (only set on timeout)."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'GCE-RESCUE-COMPLETE'
        }
        result = self.operation.execute(vm_name='test-vm', timeout=10)
        assert result.success is True
        assert result.details is None

    def test_tracking_label_used(self):
        """Test that tracking label creates custom User-Agent."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'GCE-RESCUE-COMPLETE'
        }

        # Mock _create_tracked_client to verify it's called
        mock_tracked_client = Mock()
        mock_tracked_client.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'GCE-RESCUE-COMPLETE'
        }

        with patch.object(self.operation, '_create_tracked_client', return_value=mock_tracked_client) as mock_create:
            # Act
            result = self.operation.execute(
                vm_name='test-vm',
                tracking_label='test-tracking-label',
                timeout=10
            )

            # Assert
            assert result.success is True
            mock_create.assert_called_once_with('test-tracking-label')
            # Verify tracked client was used (called at least once)
            assert mock_tracked_client.instances().getSerialPortOutput.call_count >= 1

    def test_guest_attr_complete_variable_value(self):
        """Guest attribute variableValue=COMPLETE is detected."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'COMPLETE'
        }
        assert self.operation._completion_guest_attribute_set(self.mock_compute, 'vm') is True

    def test_guest_attr_complete_items(self):
        """Guest attribute via queryValue.items is detected."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'queryValue': {'items': [
                {'namespace': 'gce-rescue', 'key': 'status', 'value': 'COMPLETE'}
            ]}
        }
        assert self.operation._completion_guest_attribute_set(self.mock_compute, 'vm') is True

    def test_guest_attr_not_complete(self):
        """A non-COMPLETE value is not treated as done."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'RUNNING'
        }
        assert self.operation._completion_guest_attribute_set(self.mock_compute, 'vm') is False

    def test_guest_attr_error_returns_false(self):
        """A 404/disabled error (attribute not set) returns False, not a crash."""
        self.mock_compute.instances().getGuestAttributes().execute.side_effect = Exception("404")
        assert self.operation._completion_guest_attribute_set(self.mock_compute, 'vm') is False

    def test_execute_succeeds_via_guest_attr_without_serial_marker(self):
        """Completion via guest attribute succeeds even if serial lacks the marker."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'booting... no marker here'
        }
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'COMPLETE'
        }
        result = self.operation.execute(vm_name='test-vm', timeout=10)
        assert result.success is True
        assert "completed" in result.message.lower()

    def test_guest_attr_session_token_accepted(self):
        """The session token value counts as completion when expected."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'COMPLETE-abc123def456'
        }
        assert self.operation._completion_guest_attribute_set(
            self.mock_compute, 'vm', expected_value='COMPLETE-abc123def456'
        ) is True

    def test_guest_attr_stale_complete_rejected_for_session_token(self):
        """A stale COMPLETE from a previous rescue must NOT satisfy a
        session-token verification (guest attributes persist across
        stop/start/restore and would short-circuit repeat repairs)."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'COMPLETE'
        }
        assert self.operation._completion_guest_attribute_set(
            self.mock_compute, 'vm', expected_value='COMPLETE-abc123def456'
        ) is False

    def test_execute_stale_guest_attr_does_not_short_circuit(self):
        """execute() with a session token ignores a stale COMPLETE and times
        out when neither the token nor the serial marker appears."""
        self.mock_compute.instances().getGuestAttributes().execute.return_value = {
            'variableValue': 'COMPLETE'  # from a previous rescue session
        }
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'booting... no marker'
        }
        with patch('time.sleep'), patch('time.time', side_effect=[0, 0, 20, 20]):
            result = self.operation.execute(
                vm_name='test-vm', timeout=10,
                expected_completion_value='COMPLETE-abc123def456'
            )
        assert result.success is False

    def test_startup_failure_marker_aborts_early(self):
        """A terminal failure line on serial fails verification immediately
        instead of polling out the timeout (a mount that already failed can
        never produce the completion marker)."""
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'mounting...\nERROR: All mount attempts failed\n'
        }
        result = self.operation.execute(vm_name='test-vm', timeout=600)
        assert result.success is False
        assert 'All mount attempts failed' in result.message
        # One poll was enough - no timeout wait
        assert self.mock_compute.instances().getSerialPortOutput().execute.call_count == 1

    def test_rollback_returns_true(self):
        """Test rollback always returns True (no-op for verification)."""
        # Act
        result = self.operation.rollback({})

        # Assert
        assert result is True

    def test_custom_completion_marker(self):
        """Test using custom completion marker."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {
            'contents': 'Boot messages...\nCUSTOM-MARKER-HERE\nMore output...'
        }

        # Act
        result = self.operation.execute(
            vm_name='test-vm',
            completion_marker='CUSTOM-MARKER-HERE',
            timeout=10
        )

        # Assert
        assert result.success is True

    def test_serial_output_missing_contents_field(self):
        """Test handling when serial output response doesn't have contents field."""
        # Arrange
        self.mock_compute.instances().getSerialPortOutput().execute.return_value = {}

        # Act
        with patch('time.sleep'):
            with patch('time.time', side_effect=[0, 5, 121]):
                result = self.operation.execute(vm_name='test-vm', timeout=120)

        # Assert
        assert result.success is False
        assert "Timeout" in result.message
