"""Tests for DiagnoseOperation: serial console analysis, error handling, edge cases."""

import logging
from unittest.mock import Mock, patch, MagicMock

import pytest
from googleapiclient.errors import HttpError

from gce_rescue_v2.operations.diagnose import DiagnoseOperation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Exec:
    """Fake .execute() return wrapper."""
    def __init__(self, value=None):
        self._value = value

    def execute(self):
        return self._value


def _make_compute(vm_info=None, serial_output=''):
    """Create a minimal fake compute client."""
    compute = Mock()

    if vm_info is None:
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/boot-disk',
                'deviceName': 'boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': [], 'fingerprint': 'abc'},
        }

    compute.instances.return_value.get.return_value.execute.return_value = vm_info
    compute.instances.return_value.getSerialPortOutput.return_value.execute.return_value = {
        'contents': serial_output
    }
    return compute


def _make_logger():
    logger = logging.getLogger('test_diagnose')
    logger.setLevel(logging.DEBUG)
    return logger


def _make_http_error(status_code, message="error"):
    """Create a fake HttpError with given status code."""
    resp = Mock()
    resp.status = status_code
    return HttpError(resp, f'{{"error": {{"message": "{message}"}}}}'.encode())


def _diagnose(serial: str):
    """Run DiagnoseOperation against a serial excerpt, return rollback_data."""
    compute = _make_compute(serial_output=serial)
    op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())
    result = op.execute('test-vm')
    return result.rollback_data


# ---------------------------------------------------------------------------
# TestDiagnoseBasic
# ---------------------------------------------------------------------------

class TestDiagnoseBasic:
    """Basic execution and return values."""

    def test_healthy_vm_returns_success(self):
        """Healthy VM with no boot errors should return success."""
        serial = "Linux version 5.15.0-100-generic (builder@server)\n[    0.000000] Booting Linux on physical CPU\nlogin: "
        compute = _make_compute(serial_output=serial)
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is True
        assert result.rollback_data['diagnosis_status'] == 'healthy'
        assert result.rollback_data['boot_errors'] == []

    def test_fstab_error_detected(self):
        """VM with fstab error should return boot_errors_detected."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "emergency mode\n"
        )
        compute = _make_compute(serial_output=serial)
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is True
        assert result.rollback_data['diagnosis_status'] == 'boot_errors_detected'
        assert len(result.rollback_data['boot_errors']) > 0

    def test_empty_serial_output(self):
        """Empty serial output should return unable_to_diagnose."""
        compute = _make_compute(serial_output='')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is False
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'

    def test_short_serial_output(self):
        """Very short serial output should return unable_to_diagnose."""
        compute = _make_compute(serial_output='boot')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is False
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'

    def test_operation_name(self):
        """Operation should have correct name."""
        compute = _make_compute()
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())
        assert op.name == "Diagnose VM"

    def test_rollback_is_noop(self):
        """Rollback should always return True (read-only operation)."""
        compute = _make_compute()
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())
        assert op.rollback({}) is True


# ---------------------------------------------------------------------------
# TestDiagnoseOSDetection
# ---------------------------------------------------------------------------

class TestDiagnoseOSDetection:
    """OS type, flavor, architecture detection in diagnosis results."""

    def test_linux_vm_detected(self):
        """Linux VM should be detected from licenses."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': []},
        }
        compute = _make_compute(vm_info=vm_info, serial_output='Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: ')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.rollback_data['os_type'] == 'linux'

    def test_vm_status_included(self):
        """VM status should be included in diagnosis results."""
        compute = _make_compute(serial_output='Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: ')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.rollback_data['status'] == 'RUNNING'


# ---------------------------------------------------------------------------
# TestDiagnoseErrorHandling
# ---------------------------------------------------------------------------

class TestDiagnoseErrorHandling:
    """Error handling for various API failure modes."""

    def test_vm_not_found_404(self):
        """404 from instances.get should return clear not-found message."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = (
            _make_http_error(404, "Instance not found")
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('nonexistent-vm')
        assert result.success is False
        assert 'not found' in result.message.lower()
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'

    def test_instances_get_403_graceful_degradation(self):
        """403 from instances.get should continue with serial console only."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = (
            _make_http_error(403, "Permission denied")
        )
        # Serial console should still work
        compute.instances.return_value.getSerialPortOutput.return_value.execute.return_value = {
            'contents': 'Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: '
        }
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        # Should succeed despite 403 on instances.get
        assert result.success is True
        assert result.rollback_data['os_type'] == 'unknown'
        assert result.rollback_data['status'] == 'UNKNOWN'

    def test_serial_console_disabled_403(self):
        """403 from getSerialPortOutput should return serial-disabled message."""
        compute = _make_compute()
        compute.instances.return_value.getSerialPortOutput.return_value.execute.side_effect = (
            _make_http_error(403, "Serial port output disabled")
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is False
        assert 'serial console' in result.message.lower()
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'
        # Should suggest enabling serial console
        recs = result.rollback_data['recommendations']
        assert any('serial-port-enable' in r for r in recs)

    def test_serial_console_other_error(self):
        """Non-403 error from getSerialPortOutput should return error message."""
        compute = _make_compute()
        compute.instances.return_value.getSerialPortOutput.return_value.execute.side_effect = (
            _make_http_error(500, "Internal error")
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is False
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'

    def test_unexpected_exception(self):
        """Unexpected exception should return error gracefully."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = (
            RuntimeError("Unexpected failure")
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is False
        assert 'Unexpected' in result.message
        assert result.rollback_data['diagnosis_status'] == 'unable_to_diagnose'


# ---------------------------------------------------------------------------
# TestDiagnoseTracking
# ---------------------------------------------------------------------------

class TestDiagnoseTracking:
    """User-Agent tracking label support."""

    def test_no_tracking_uses_base_compute(self):
        """Without tracking_label, should use base compute client."""
        compute = _make_compute(serial_output='Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: ')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        assert result.success is True
        # Verify base compute was used (instances().get() was called)
        compute.instances.return_value.get.assert_called()

    def test_tracking_label_creates_tracked_client(self):
        """With tracking_label, should call _create_tracked_client."""
        compute = _make_compute(serial_output='Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: ')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        # Patch _create_tracked_client to return the mock compute
        with patch.object(op, '_create_tracked_client', return_value=compute) as mock_create:
            result = op.execute('test-vm', tracking_label='diagnose')
            mock_create.assert_called_once_with('diagnose')
            assert result.success is True


# ---------------------------------------------------------------------------
# TestDiagnoseResultStructure
# ---------------------------------------------------------------------------

class TestDiagnoseResultStructure:
    """Verify result dict contains all required fields."""

    def test_success_result_has_all_fields(self):
        """Successful diagnosis should include all expected fields."""
        compute = _make_compute(serial_output='Linux version 5.15.0-100-generic\n[    0.000000] Booting Linux\nlogin: ')
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        data = result.rollback_data

        required_fields = [
            'vm_name', 'zone', 'status', 'os_type', 'os_flavor',
            'architecture', 'license_type', 'diagnosis_status',
            'boot_errors', 'recommendations'
        ]
        for field in required_fields:
            assert field in data, f"Missing field: {field}"

    def test_error_result_has_all_fields(self):
        """Failed diagnosis should still include all expected fields."""
        compute = Mock()
        compute.instances.return_value.get.return_value.execute.side_effect = (
            _make_http_error(404, "Not found")
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        data = result.rollback_data

        required_fields = [
            'vm_name', 'zone', 'status', 'os_type', 'os_flavor',
            'architecture', 'license_type', 'diagnosis_status',
            'boot_errors', 'recommendations'
        ]
        for field in required_fields:
            assert field in data, f"Missing field in error result: {field}"

    def test_boot_error_dict_structure(self):
        """Boot errors should have correct dict structure."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "emergency mode\n"
        )
        compute = _make_compute(serial_output=serial)
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        result = op.execute('test-vm')
        errors = result.rollback_data['boot_errors']
        assert len(errors) > 0

        error_fields = [
            'name', 'category', 'severity', 'description', 'detected_pattern',
            'suggested_fixes', 'context_lines', 'matched_line_index'
        ]
        for err in errors:
            for field in error_fields:
                assert field in err, f"Missing field in boot error: {field}"


# ---------------------------------------------------------------------------
# TestDiagnoseStabilization
# ---------------------------------------------------------------------------

class TestDiagnoseStabilization:
    """Stability-based polling for RUNNING VMs."""

    def test_stabilize_returns_immediately_for_terminated_vm(self):
        """TERMINATED VMs skip polling entirely (single pass)."""
        vm_info = {
            'status': 'TERMINATED',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': []},
        }
        serial = "Linux version 5.15.0-100-generic (builder@server)\n[    0.000000] Booting Linux on physical CPU\nlogin: "
        compute = _make_compute(vm_info=vm_info, serial_output=serial)
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        with patch.object(op, '_stabilize_diagnosis') as mock_stab:
            result = op.execute('test-vm', stabilize=True)
            # _stabilize_diagnosis should NOT be called for TERMINATED
            mock_stab.assert_not_called()
        assert result.success is True

    def test_stabilize_waits_for_stable_result(self):
        """Should poll until 2 consecutive identical results."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': []},
        }

        # Simulate serial output growing over time:
        # Poll 1: healthy (no errors)
        # Poll 2: fstab error appears
        # Poll 3: same fstab error (stable)
        serial_outputs = [
            "Linux version 5.15.0\nlogin: ",
            (
                "Linux version 5.15.0\n"
                "Timed out waiting for device "
                "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
                "emergency mode\n"
            ),
            (
                "Linux version 5.15.0\n"
                "Timed out waiting for device "
                "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
                "emergency mode\n"
            ),
        ]
        call_count = [0]

        def _side_effect(**kwargs):
            idx = min(call_count[0], len(serial_outputs) - 1)
            call_count[0] += 1
            result = Mock()
            result.execute.return_value = {'contents': serial_outputs[idx]}
            return result

        compute = _make_compute(vm_info=vm_info)
        compute.instances.return_value.getSerialPortOutput.side_effect = (
            _side_effect
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        with patch('gce_rescue_v2.operations.diagnose.time.sleep'):
            result = op.execute('test-vm', stabilize=True)

        assert result.success is True
        assert result.rollback_data['diagnosis_status'] == 'boot_errors_detected'
        # 3 polls: healthy, error, error (stable)
        assert call_count[0] == 3

    def test_stabilize_times_out_returns_last(self):
        """When diagnosis never stabilizes, return last result at timeout."""
        vm_info = {
            'status': 'RUNNING',
            'disks': [{
                'boot': True,
                'source': 'projects/p/zones/z/disks/boot-disk',
                'licenses': ['projects/debian-cloud/global/licenses/debian-12'],
            }],
            'machineType': 'zones/z/machineTypes/e2-micro',
            'metadata': {'items': []},
        }

        # Each poll returns different serial output (never stabilizes)
        call_count = [0]

        def _side_effect(**kwargs):
            call_count[0] += 1
            # Alternate between healthy and error to prevent stabilization
            if call_count[0] % 2 == 1:
                serial = "Linux version 5.15.0\nlogin: "
            else:
                serial = (
                    "Linux version 5.15.0\n"
                    "Timed out waiting for device "
                    "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
                    "emergency mode\n"
                )
            result = Mock()
            result.execute.return_value = {'contents': serial}
            return result

        compute = _make_compute(vm_info=vm_info)
        compute.instances.return_value.getSerialPortOutput.side_effect = (
            _side_effect
        )
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        # Use very short timeout to avoid slow test
        with patch('gce_rescue_v2.operations.diagnose.time.sleep'):
            with patch('gce_rescue_v2.operations.diagnose.time.monotonic') as mock_mono:
                # Simulate time progressing past the deadline
                # First call sets deadline, then each subsequent call is past it
                mock_mono.side_effect = [0.0, 0.0, 31.0]
                result = op.execute('test-vm', stabilize=True)

        # Should have returned a result (not hung)
        assert result.rollback_data['diagnosis_status'] in (
            'healthy', 'boot_errors_detected', 'unable_to_diagnose'
        )

    def test_stabilize_false_skips_polling(self):
        """stabilize=False (default) should do single pass, no polling."""
        serial = "Linux version 5.15.0-100-generic (builder@server)\n[    0.000000] Booting Linux on physical CPU\nlogin: "
        compute = _make_compute(serial_output=serial)
        op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())

        with patch.object(op, '_stabilize_diagnosis') as mock_stab:
            result = op.execute('test-vm', stabilize=False)
            mock_stab.assert_not_called()
        assert result.success is True


# ---------------------------------------------------------------------------
# TestDiagnoseDiskFull
# ---------------------------------------------------------------------------

class TestDiagnoseDiskFull:
    """Detection tests for disk_full category patterns."""

    def test_disk_full_no_space_detected(self):
        """Kernel/journald ENOSPC message should trigger disk_full_no_space."""
        serial = (
            "[  241.693421] EXT4-fs (sda1): mounted filesystem with ordered data mode\n"
            "[  242.104233] systemd-journald[312]: Failed to write entry "
            "(23 items, 812 bytes), ignoring: No space left on device\n"
            "[  242.512345] systemd[1]: Failed to start Rotate log files.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'disk_full_no_space' in names

    def test_disk_full_guest_agent_detected(self):
        """Guest agent temp-directory failure should trigger disk_full_guest_agent."""
        serial = (
            "[  120.001234] google_guest_agent[512]: ERROR instance_setup.go:160 "
            "Failed to generate SSH host keys: [Errno 2] "
            "No usable temporary directory found in ['/tmp', '/var/tmp', '/usr/tmp']\n"
            "[  120.442211] OSConfigAgent Error: unexpected end of JSON input\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'disk_full_guest_agent' in names

    def test_both_disk_full_patterns_detected_together(self):
        """A truly full disk usually shows both symptoms; both should be reported."""
        serial = (
            "[  310.104233] systemd-journald[312]: Failed to write entry, "
            "ignoring: No space left on device\n"
            "[  312.001234] google_guest_agent[512]: ERROR non_windows_accounts.go:144 "
            "Error updating SSH keys: [Errno 2] No usable temporary directory found "
            "in ['/tmp', '/var/tmp', '/usr/tmp']\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'disk_full_no_space' in names
        assert 'disk_full_guest_agent' in names

    def test_disk_full_severities(self):
        """disk_full_no_space is critical; disk_full_guest_agent is error."""
        serial = (
            "[  310.104233] systemd-journald[312]: Failed to write entry, "
            "ignoring: No space left on device\n"
            "[  312.001234] google_guest_agent[512]: [Errno 2] "
            "No usable temporary directory found in ['/tmp', '/var/tmp']\n"
        )
        data = _diagnose(serial)
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['disk_full_no_space'] == 'critical'
        assert severities['disk_full_guest_agent'] == 'error'

    def test_fstab_errors_do_not_trigger_disk_full_patterns(self):
        """fstab-only failures must not produce disk_full findings."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device "
            "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "You are in emergency mode\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'disk_full' not in categories

    def test_healthy_boot_matches_no_disk_full_patterns(self):
        """A normal boot with free space must not trigger disk_full."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel)\n"
            "[    2.104233] EXT4-fs (sda1): mounted filesystem with ordered data mode\n"
            "[    4.512345] systemd[1]: Reached target Local File Systems.\n"
            "login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_inotify_exhaustion_enospc_not_flagged(self):
        """inotify watch exhaustion returns ENOSPC with a healthy disk.

        These messages appear at RUNTIME (after boot success), so the
        boot-success suppression does not protect against them — the
        regex itself must exclude them.
        """
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel)\n"
            "[   12.000000] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "systemd[1]: Failed to add /run/systemd/ask-password to "
            "directory watch: No space left on device\n"
            "tail: inotify cannot be used, reverting to polling: "
            "no space left on device\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_cgroup_limit_enospc_not_flagged(self):
        """cgroup-limit ENOSPC on container hosts is not a full disk."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel)\n"
            "[   12.000000] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "kubelet[1543]: mkdir /sys/fs/cgroup/memory/kubepods/pod9f3: "
            "no space left on device\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_quoted_enospc_string_not_flagged(self):
        """A line merely quoting the phrase mid-sentence must not match."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel)\n"
            "[   12.000000] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            'startup-script[900]: INFO: monitor app logs for '
            '"No space left on device" and page oncall\n'
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_enospc_noise_does_not_mask_fstab_finding(self):
        """inotify ENOSPC noise must not dedupe away a real fstab failure.

        Regression for red-team D2: before the regex fix, the noise line
        counted as a disk_full "root cause" and the generic-symptom tier
        deleted the fstab dependency finding, hiding the actual problem.
        Uses the engine directly with TERMINATED status (no suppression).
        """
        from gce_rescue_v2.core.diagnosis import analyze_serial_output

        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel)\n"
            "Dependency failed for /data.\n"
            "tail: inotify cannot be used, reverting to polling: "
            "no space left on device\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'disk_full' not in categories
        assert 'fstab' in categories
