"""Tests for DiagnoseOperation: serial console analysis, error handling, edge cases."""

import logging
from unittest.mock import Mock, patch, MagicMock

import pytest
from googleapiclient.errors import HttpError

from gce_rescue_v2.core.diagnosis import analyze_serial_output
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
# TestKernelPanicDetection
# ---------------------------------------------------------------------------

class TestKernelPanicDetection:
    """Detection tests for kernel.yaml panic patterns."""

    def test_hung_task_panic_detected(self):
        """Hung-task panic should report kernel_panic_hung_task, not generic."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64 (debian-kernel@lists.debian.org)\n"
            "[  241.693421] INFO: task jbd2/sda1-8:512 blocked for more than 120 seconds.\n"
            "[  241.699830] \"echo 0 > /proc/sys/kernel/hung_task_timeout_secs\" disables this message.\n"
            "[  362.812554] Kernel panic - not syncing: hung_task: blocked tasks\n"
            "[  362.818992] CPU: 0 PID: 42 Comm: khungtaskd Not tainted 5.10.0-28-cloud-amd64 #1\n"
            "[  362.825101] Call Trace:\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_hung_task' in names
        assert 'kernel_panic_generic' not in names

    def test_oom_panic_detected(self):
        """OOM panic should report kernel_panic_oom, not generic.

        CONTRACT CHANGE (Wave 4): the 'Out of memory: Killed process' line
        used to match nothing; since oom.yaml landed it deliberately also
        produces an oom finding (warning). Both are reported: the kill and
        the panic are different, both-true findings, and the warning can
        never suppress the critical (tier-1 requires CRITICAL severity and
        _is_boot_root_cause excludes warnings/detect-only)."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[  512.103311] Out of memory: Killed process 1234 (java) total-vm:8388608kB\n"
            "[  512.209972] Kernel panic - not syncing: out of memory. panic_on_oom is selected\n"
            "[  512.216104] CPU: 1 PID: 55 Comm: oom_reaper Not tainted 5.10.0-28-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_oom' in names
        assert 'kernel_panic_generic' not in names
        assert 'oom_killed_process' in names
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['kernel_panic_oom'] == 'critical'
        assert severities['oom_killed_process'] == 'warning'

    def test_machine_check_panic_detected(self):
        """Fatal machine check panic should report kernel_panic_machine_check."""
        serial = (
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64\n"
            "[  100.001234] mce: [Hardware Error]: CPU 0: Machine Check Exception: 5 Bank 0\n"
            "[  100.103421] Kernel panic - not syncing: Fatal Machine check\n"
            "[  100.109553] Kernel Offset: disabled\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_machine_check' in names
        assert 'kernel_panic_generic' not in names

    def test_nmi_panic_detected(self):
        """NMI panic should report kernel_panic_nmi, not generic."""
        serial = (
            "[    0.000000] Linux version 5.15.0-100-generic\n"
            "[   88.004521] Uhhuh. NMI received for unknown reason 31 on CPU 0.\n"
            "[   88.101233] Kernel panic - not syncing: NMI: Not continuing\n"
            "[   88.107455] CPU: 0 PID: 0 Comm: swapper/0 Not tainted 5.15.0-100-generic #1\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_nmi' in names
        assert 'kernel_panic_generic' not in names

    def test_unrecognized_panic_reports_generic(self):
        """A panic cause not covered by specific patterns falls to generic.

        'Attempted to kill the idle task!' deliberately sits one word away
        from the kernel_attempted_kill_init exclusion ('Attempted to kill
        init') - it must fall through to generic, proving the exclusion
        term is exactly as tight as its positive.
        """
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[    5.002311] Kernel panic - not syncing: Attempted to kill the idle task!\n"
            "[    5.008442] CPU: 0 PID: 0 Comm: swapper/0 Not tainted 5.10.0-28-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_generic' in names
        assert 'kernel_attempted_kill_init' not in names

    def test_modern_oom_panic_detected(self):
        """Modern (>=4.x) OOM panic wording must be detected, not report healthy.

        Regression: kernels since ~4.x panic with 'Out of memory: ...
        panic_on_oom is enabled' (not the pre-4.x 'is selected'). The old
        regex missed it AND the generic lookahead excluded it -> the engine
        reported a genuinely panicked VM as healthy.
        """
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  512.103311] Out of memory: Killed process 1234 (java) total-vm:8388608kB\n"
            "[  512.209972] Kernel panic - not syncing: Out of memory: system-wide panic_on_oom is enabled\n"
            "[  512.216104] CPU: 1 PID: 55 Comm: oom_reaper Not tainted 6.1.0-18-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_oom' in names
        assert 'kernel_panic_generic' not in names
        # Wave 4 contract change: the kill line now also reports oom
        # (warning) alongside the critical panic - see oom.yaml header.
        assert 'oom_killed_process' in names

    def test_compulsory_oom_panic_detected(self):
        """sysctl vm.panic_on_oom=2 variant ('compulsory ... is enabled')."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  600.001122] Kernel panic - not syncing: Out of memory: compulsory panic_on_oom is enabled\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_oom' in names
        assert 'kernel_panic_generic' not in names

    def test_deadlocked_on_memory_panic_classified_as_oom(self):
        """Red-team C4: mm/oom_kill.c panics 'System is deadlocked on
        memory' when no killable process remains - it must classify as
        kernel_panic_oom (resize/panic_on_oom fixes), not generic
        (reset/previous-kernel fixes), and the generic lookahead must
        mirror the new exclusion."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  712.103311] Kernel panic - not syncing: System is "
            "deadlocked on memory\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_oom' in names
        assert 'kernel_panic_generic' not in names

    def test_evidence_anchors_on_actual_match_not_first_occurrence(self):
        """Evidence must quote the line the regex matched, not an earlier
        line containing the same text.

        Regression (live Test 3): buffer held an old OOM panic and a fresh
        sysrq panic. kernel_panic_generic matched the sysrq line (lookahead
        rejects the OOM line), but context extraction re-searched for the
        matched text 'Kernel panic - not syncing' and anchored the evidence
        on the FIRST occurrence — the OOM line. Context must be derived
        from the match offset instead.
        """
        serial = (
            "[    0.000000] Linux version 6.1.0-49-cloud-amd64\n"
            "[   85.365705] Kernel panic - not syncing: Out of memory: system-wide panic_on_oom is enabled\n"
            "[   85.374092] CPU: 1 PID: 1138 Comm: tail Not tainted 6.1.0-49-cloud-amd64 #1\n"
            "[    0.000000] Linux version 6.1.0-49-cloud-amd64 (second boot)\n"
            "[   18.500000] sysrq: Trigger a crash\n"
            "[   18.600000] Kernel panic - not syncing: sysrq triggered crash\n"
            "[   18.830725] ---[ end Kernel panic - not syncing: sysrq triggered crash ]---\n"
        )
        data = _diagnose(serial)
        generic = [e for e in data['boot_errors']
                   if e['name'] == 'kernel_panic_generic']
        assert generic, "kernel_panic_generic should fire on the sysrq line"
        err = generic[0]
        matched_line = err['context_lines'][err['matched_line_index']]
        assert 'sysrq triggered crash' in matched_line
        assert 'panic_on_oom' not in matched_line

    def test_vfs_panic_variant_falls_to_generic(self):
        """A VFS root-fs panic that is NOT unknown-block must not vanish.

        Regression: the generic lookahead excluded any 'VFS: Unable to mount
        root fs' while initramfs only matches the 'on unknown-block' form ->
        e.g. an NFS-root panic matched nothing and reported healthy. Exclusion
        terms must mirror the positive patterns exactly.
        """
        serial = (
            "[    0.000000] Linux version 5.15.0-100-generic\n"
            "[    9.912345] Kernel panic - not syncing: VFS: Unable to mount root fs via NFS.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_generic' in names
        assert 'initramfs_no_root_fs' not in names

    def test_fstab_errors_do_not_trigger_kernel_patterns(self):
        """fstab failure lines should not produce kernel-category findings."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "You are in emergency mode\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' not in categories

    def test_healthy_boot_matches_no_kernel_patterns(self):
        """A clean boot log should produce no kernel findings."""
        serial = (
            "[    0.000000] Linux version 5.15.0-100-generic (builder@server)\n"
            "[    0.000000] Booting Linux on physical CPU 0x0\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered data mode\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestInitramfsDetection
# ---------------------------------------------------------------------------

class TestInitramfsDetection:
    """Detection tests for initramfs.yaml patterns, including kernel overlap."""

    def test_no_root_fs_panic_detected_as_initramfs(self):
        """VFS root-mount panic reports initramfs, NOT generic kernel panic.

        The panic line itself contains 'Kernel panic - not syncing', so
        kernel_panic_generic must exclude it via negative lookahead.
        """
        serial = (
            "[    0.000000] Linux version 4.18.0-425.el8.x86_64\n"
            "[    1.523311] md: Waiting for all devices to be available before autodetect\n"
            "[    1.612345] VFS: Cannot open root device \"sda1\" or unknown-block(0,0): error -6\n"
            "[    1.702211] Please append a correct \"root=\" boot option; here are the available partitions:\n"
            "[    1.803992] Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)\n"
            "[    1.810221] CPU: 0 PID: 1 Comm: swapper/0 Not tainted 4.18.0-425.el8.x86_64 #1\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_no_root_fs' in names
        assert 'kernel_panic_generic' not in names

    def test_unpacking_failure_detected(self):
        """Corrupt initramfs (junk in compressed archive) should be detected."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[    0.812345] Trying to unpack rootfs image as initramfs...\n"
            "[    0.905566] Initramfs unpacking failed: junk in compressed archive\n"
            "[    1.002211] Freeing initrd memory: 24576K\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_load_failure' in names

    def test_unpacking_failure_is_error_not_critical(self):
        """'junk ... compressed archive' is benign on some kernels (microcode
        cpio padding, ~5.4-5.15) where boot proceeds normally. Without a
        following VFS panic it is ambiguous, so it must report severity
        'error', not 'critical' (the unambiguous case is initramfs_no_root_fs).
        """
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[    0.905566] Initramfs unpacking failed: junk in compressed archive\n"
        )
        data = _diagnose(serial)
        findings = [e for e in data['boot_errors']
                    if e['name'] == 'initramfs_load_failure']
        assert findings, "initramfs_load_failure should be detected"
        assert all(f['severity'] == 'error' for f in findings)

    def test_load_failure_variant_detected(self):
        """'Failed to load initramfs' variant should also be detected."""
        serial = (
            "[    0.000000] Linux version 5.15.0-100-generic\n"
            "[    0.712345] Failed to load initramfs image from /boot/initrd.img-5.15.0-100-generic\n"
            "[    0.809982] Kernel command line: BOOT_IMAGE=/boot/vmlinuz-5.15.0-100-generic root=UUID=abc\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_load_failure' in names

    def test_benign_decoding_message_not_matched(self):
        """'Initramfs unpacking failed: Decoding failed' is benign on some
        kernels (LZ4 fallback) and must NOT trigger initramfs_load_failure."""
        serial = (
            "[    0.000000] Linux version 5.4.0-100-generic (buildd@lcy02)\n"
            "[    0.905566] Initramfs unpacking failed: Decoding failed\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered data mode\n"
            "ubuntu login: \n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'initramfs' not in categories

    def test_fstab_errors_do_not_trigger_initramfs_patterns(self):
        """fstab failure lines should not produce initramfs-category findings."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "You are in emergency mode\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'initramfs' not in categories


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


# ---------------------------------------------------------------------------
# TestDiagnoseSshPatterns
# ---------------------------------------------------------------------------

class TestDiagnoseSshPatterns:
    """Detection tests for the ssh diagnose category."""

    def test_sshd_config_bad_option_detected(self):
        """Invalid sshd_config directive should match ssh_sshd_config_error."""
        serial = (
            "[   10.312478] cloud-init[498]: Cloud-init v. 22.4.2 running 'modules:final'\n"
            "[   10.812345] sshd[512]: /etc/ssh/sshd_config: line 122: "
            "Bad configuration option: ThisIsNotAValidDirective\n"
            "[   10.812999] systemd[1]: ssh.service: Control process exited, "
            "code=exited, status=255/EXCEPTION\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_sshd_config_terminating_detected(self):
        """sshd 'terminating, N bad configuration options' should match."""
        serial = (
            "[   11.102938] sshd[512]: /etc/ssh/sshd_config: terminating, "
            "1 bad configuration options\n"
            "[   11.104001] systemd[1]: ssh.service: Main process exited, "
            "code=exited, status=255/EXCEPTION\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_sshd_service_failed_debian_detected(self):
        """Debian-style ssh.service failure line should match."""
        serial = (
            "[   12.001234] systemd[1]: ssh.service: Start request repeated too quickly.\n"
            "[FAILED] Failed to start ssh.service - OpenBSD Secure Shell server.\n"
            "See 'systemctl status ssh.service' for details.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_sshd_service_failed_rhel_detected(self):
        """RHEL-style sshd.service failure line should match."""
        serial = (
            "[   13.442210] systemd[1]: sshd.service: Start request repeated too quickly.\n"
            "[FAILED] Failed to start sshd.service - OpenSSH server daemon.\n"
            "See 'systemctl status sshd.service' for details.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_guest_agent_failed_to_start_detected(self):
        """systemd failure of google-guest-agent should match ssh_guest_agent_failed."""
        serial = (
            "[   12.481000] systemd[1]: google-guest-agent.service: "
            "Start request repeated too quickly.\n"
            "[FAILED] Failed to start google-guest-agent.service - "
            "Google Compute Engine Guest Agent.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_guest_agent_failed' in names

    def test_guest_agent_fatal_log_detected(self):
        """Guest agent fatal log line should match ssh_guest_agent_failed."""
        serial = (
            "[   13.001111] systemd[1]: Starting google-guest-agent.service...\n"
            "[   13.123456] google_guest_agent[655]: FATAL main.go:118 "
            "error creating instance config: invalid config\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_guest_agent_failed' in names

    def test_guest_agent_benign_log_not_matched(self):
        """Normal guest agent startup logs should not trigger any ssh pattern."""
        serial = (
            "[    9.881234] google_guest_agent[652]: GCE Agent Started (version 20230601.00)\n"
            "[    9.991234] google_guest_agent[652]: Adding existing user gokull to google-sudoers group.\n"
        )
        data = _diagnose(serial)
        assert 'ssh' not in {e['category'] for e in data['boot_errors']}

    def test_auth_permissions_detected(self):
        """sshd 'bad ownership or modes' should match ssh_auth_permissions."""
        serial = (
            "[  241.601000] sshd[1042]: Connection from 192.168.1.7 port 51522\n"
            "[  241.693421] sshd[1042]: Authentication refused: "
            "bad ownership or modes for directory /home/gokull/.ssh\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_auth_permissions' in names

    def test_fstab_errors_do_not_trigger_ssh_patterns(self):
        """fstab root-cause lines must not produce ssh findings."""
        serial = (
            "[    5.102938] systemd[1]: Timed out waiting for device "
            "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "[    5.104001] systemd[1]: Dependency failed for /mnt/data\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        assert 'ssh' not in {e['category'] for e in data['boot_errors']}

    def test_healthy_boot_matches_no_ssh_patterns(self):
        """A clean boot with normal ssh/guest-agent lines should stay healthy."""
        serial = (
            "[    8.812345] systemd[1]: Started ssh.service - OpenBSD Secure Shell server.\n"
            "[    9.881234] google_guest_agent[652]: GCE Agent Started (version 20230601.00)\n"
            "[   10.101234] systemd[1]: Startup finished in 4.102s (kernel) + 6.204s (userspace) = 10.306s.\n"
            "login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestDiagnoseSshRedTeamRegressions
# ---------------------------------------------------------------------------

class TestDiagnoseSshRedTeamRegressions:
    """Regressions from red-team review of the ssh diagnose category.

    Each test reproduces a confirmed failing serial-console scenario; serial
    buffers accumulate across reboots, so several tests mix stale lines from
    old boots with the failure of the latest boot.
    """

    def test_stale_ssh_noise_does_not_mask_fstab_emergency(self):
        """C1: a stale ssh auth line must not erase fstab boot-blockers.

        The stale 'Authentication refused' line is followed by a success
        marker, then the VM reboots into an fstab emergency-mode failure.
        Cross-category dedupe previously deleted the fstab criticals (and
        in this ordering even reported the VM as healthy).
        """
        serial = (
            "sshd[900]: Authentication refused: bad ownership or modes "
            "for directory /home/bob/.ssh\n"
            "systemd[1]: Startup finished in 1.2s (kernel) + 9.0s "
            "(userspace) = 10.2s.\n"
            "-- reboot --\n"
            "systemd[1]: Dependency failed for /data.\n"
            "systemd[1]: data.mount: Job data.mount/start failed with "
            "result 'dependency'.\n"
            "You are in emergency mode. After logging in, type "
            '"journalctl -xb" to view\n'
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        categories = {e['category'] for e in data['boot_errors']}
        # fstab findings must survive so `repair` can still auto-fix them
        assert 'fstab' in categories
        assert any(
            e['category'] == 'fstab' and e['severity'] == 'critical'
            for e in data['boot_errors']
        )

    def test_guest_agent_failure_survives_completed_boot(self):
        """C2: guest agent failure never blocks boot, so a 'Startup
        finished' marker must not suppress the finding on a RUNNING VM."""
        serial = (
            "systemd[1]: Starting google-guest-agent.service - "
            "Google Compute Engine Guest Agent...\n"
            "google_guest_agent[412]: panic: runtime error: invalid memory "
            "address or nil pointer dereference\n"
            "systemd[1]: google-guest-agent.service: Start request repeated "
            "too quickly.\n"
            "systemd[1]: Failed to start google-guest-agent.service - "
            "Google Compute Engine Guest Agent.\n"
            "systemd[1]: Reached target multi-user.target - Multi-User System.\n"
            "systemd[1]: Startup finished in 1.226s (kernel) + 15.379s "
            "(userspace) = 16.605s.\n"
        )
        data = _diagnose(serial)  # vm_status is RUNNING in this helper
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_guest_agent_failed' in names

    def test_sshd_failure_survives_completed_boot(self):
        """C2: sshd StartLimit exhausted before 'Startup finished' must
        still be reported on a RUNNING VM (boot completes without sshd)."""
        serial = (
            "sshd[836]: /etc/ssh/sshd_config: line 124: Bad configuration "
            "option: ThisIsNotAValidDirective\n"
            "systemd[1]: ssh.service: Start request repeated too quickly.\n"
            "systemd[1]: Failed to start ssh.service - OpenBSD Secure "
            "Shell server.\n"
            "systemd[1]: Startup finished in 1.226s (kernel) + 15.379s "
            "(userspace) = 16.605s.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_auth_error_survives_later_sshd_restart(self):
        """C2: an sshd restart (e.g. unattended-upgrades) after the auth
        error must not re-suppress the ssh_auth_permissions finding."""
        serial = (
            "systemd[1]: Started ssh.service - OpenBSD Secure Shell server.\n"
            "systemd[1]: Startup finished in 1.2s (kernel) + 9.0s "
            "(userspace) = 10.2s.\n"
            "sshd[900]: Authentication refused: bad ownership or modes "
            "for directory /home/bob/.ssh\n"
            "systemd[1]: Stopping ssh.service - OpenBSD Secure Shell server...\n"
            "systemd[1]: Started ssh.service - OpenBSD Secure Shell server.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_auth_permissions' in names

    def test_sshd_config_error_without_colon_detected(self):
        """C3: OpenSSH variants print 'sshd_config line N' without a colon."""
        serial = (
            "sshd[512]: /etc/ssh/sshd_config line 12: "
            "Bad configuration option: FooBar\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_sles_openssh_daemon_failure_detected(self):
        """C4: SLES unit description is 'OpenSSH Daemon' (no unit name on
        older systemd)."""
        serial = (
            "systemd[1]: Reached target Basic System.\n"
            "systemd[1]: Failed to start OpenSSH Daemon.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_sshd_config_error' in names

    def test_non_fatal_agent_log_not_matched(self):
        """C5: 'non-fatal' agent lines must not match the fatal/panic regex."""
        serial = (
            "google_guest_agent[583]: retrying non-fatal metadata error\n"
        )
        data = _diagnose(serial)
        assert 'ssh' not in {e['category'] for e in data['boot_errors']}
        assert data['diagnosis_status'] == 'healthy'

    def test_agent_shutdown_transient_not_matched(self):
        """C6: 'Failed with result' during shutdown is a transient, not a
        boot-time agent failure."""
        serial = (
            "systemd[1]: google-guest-agent.service: "
            "Failed with result 'exit-code'.\n"
        )
        data = _diagnose(serial)
        assert 'ssh' not in {e['category'] for e in data['boot_errors']}
        assert data['diagnosis_status'] == 'healthy'


# ---------------------------------------------------------------------------
# TestDiagnoseFilesystemPatterns
# ---------------------------------------------------------------------------

class TestDiagnoseFilesystemPatterns:
    """Detection tests for the filesystem diagnose category."""

    def test_bad_magic_number_detected(self):
        """e2fsck bad superblock magic should fire filesystem_bad_superblock."""
        serial = (
            "[  OK  ] Reached target local-fs-pre.target - Preparation for Local File Systems.\n"
            "         Starting systemd-fsck@dev-sdb.service - File System Check on /dev/sdb...\n"
            "systemd-fsck[412]: fsck.ext4: Bad magic number in super-block while trying to open /dev/sdb\n"
            "systemd-fsck[412]: fsck failed with exit status 8.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_bad_superblock' in names

    def test_superblock_invalid_backup_blocks_detected(self):
        """fsck falling back to backup superblocks should fire filesystem_bad_superblock."""
        serial = (
            "         Starting systemd-fsck@dev-sdb.service - File System Check on /dev/sdb...\n"
            "systemd-fsck[388]: fsck.ext4: Superblock invalid, trying backup blocks...\n"
            "systemd-fsck[388]: /dev/sdb was not cleanly unmounted, check forced.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_bad_superblock' in names

    def test_superblock_could_not_be_read_detected(self):
        """e2fsck superblock-unreadable guidance should fire filesystem_bad_superblock."""
        serial = (
            "systemd-fsck[395]: The superblock could not be read or does not describe a valid ext2/ext3/ext4\n"
            "systemd-fsck[395]: filesystem.  If the device is valid and it really contains an ext2/ext3/ext4\n"
            "systemd-fsck[395]: filesystem (and not swap or ufs or something else), then the superblock\n"
            "systemd-fsck[395]: is corrupt, and you might try running e2fsck with an alternate superblock:\n"
            "systemd-fsck[395]:     e2fsck -b 8193 <device>\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_bad_superblock' in names

    def test_vfs_cant_find_ext4_detected(self):
        """Kernel mount error for a wiped superblock (observed live on GCE
        Debian 12 serial console) should fire filesystem_bad_superblock."""
        serial = (
            "         Mounting mnt-data.mount - /mnt/data...\n"
            "[    4.444497] EXT4-fs (sda): VFS: Can't find ext4 filesystem\n"
            "[FAILED] Failed to mount mnt-data.mount - /mnt/data.\n"
            "[DEPEND] Dependency failed for local-fs.target - Local File Systems.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_bad_superblock' in names
        # Tier-2 dedupe is category-scoped: a root cause only demotes generic
        # symptoms of its OWN category, so the fstab-level mount symptom is
        # reported alongside the filesystem root cause rather than hidden by
        # a finding from a different category.
        assert 'fstab_mount_failed' in names

    def test_superblock_partition_table_corrupt_detected(self):
        """e2fsck size-mismatch corruption verdict should fire filesystem_corruption."""
        serial = (
            "systemd-fsck[401]: The filesystem size (according to the superblock) is 2621440 blocks\n"
            "systemd-fsck[401]: The physical size of the device is 2359296 blocks\n"
            "systemd-fsck[401]: Either the superblock or the partition table is likely to be corrupt!\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names

    def test_xfs_in_memory_corruption_detected(self):
        """XFS in-memory corruption shutdown should fire filesystem_corruption."""
        serial = (
            "[  241.693421] XFS (sdb1): Corruption of in-memory data detected.  Shutting down filesystem\n"
            "[  241.695832] XFS (sdb1): Please unmount the filesystem and rectify the problem(s)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names

    def test_xfs_metadata_corruption_detected(self):
        """XFS metadata verifier failure should fire filesystem_corruption."""
        serial = (
            "[   88.114532] XFS (sdb1): Metadata corruption detected at xfs_inode_buf_verify+0x15e/0x180 [xfs], xfs_inode block 0x80\n"
            "[   88.117201] XFS (sdb1): Unmount and run xfs_repair\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names

    def test_ext4_fs_error_detected(self):
        """Kernel EXT4-fs error report should fire filesystem_corruption."""
        serial = (
            "[  152.208814] EXT4-fs error (device sdb1): ext4_find_entry:1455: inode #2: comm systemd: reading directory lblock 0\n"
            "[  152.211903] Aborting journal on device sdb1-8.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names

    def test_benign_ext4_mount_message_not_matched(self):
        """Normal EXT4-fs mount messages must not fire filesystem patterns."""
        serial = (
            "[    1.812345] EXT4-fs (sda1): mounted filesystem with ordered data mode. Quota mode: none.\n"
            "[    2.104211] EXT4-fs (sdb): recovery complete\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'

    def test_fstab_errors_do_not_trigger_filesystem_patterns(self):
        """Plain fstab config errors must not fire filesystem patterns."""
        serial = (
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "You are in emergency mode. After logging in, type journalctl -xb\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        categories = {e['category'] for e in data['boot_errors']}
        assert 'filesystem' not in categories

    def test_healthy_boot_matches_no_filesystem_patterns(self):
        """A clean boot log should produce a healthy diagnosis."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel@lists.debian.org)\n"
            "[    2.410394] EXT4-fs (sda1): mounted filesystem with ordered data mode.\n"
            "[  OK  ] Reached target multi-user.target - Multi-User System.\n"
            "Debian GNU/Linux 12 test-vm ttyS0\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestDiagnoseCpuLockup
# ---------------------------------------------------------------------------

class TestDiagnoseCpuLockup:
    """Detection tests for the cpu_lockup category (soft/hard lockups, bus
    locks, RCU stalls)."""

    def test_soft_lockup_detected(self):
        """Canonical soft-lockup watchdog line should be detected as error."""
        serial = (
            "[  241.693421] watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [stress-ng-cpu:1523]\n"
            "[  241.702118] Modules linked in: nft_ct nf_tables binfmt_misc virtio_net\n"
            "[  241.710233] CPU: 0 PID: 1523 Comm: stress-ng-cpu Not tainted 6.1.0-18-cloud-amd64 #1\n"
            "[  241.719544] Hardware name: Google Google Compute Engine/Google Compute Engine\n"
            "[  241.728901] RIP: 0010:queued_spin_lock_slowpath+0x5b/0x1d0\n"
            "[  241.737010] Call Trace:\n"
            "[  241.741232]  <IRQ>\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        errors = data['boot_errors']
        names = [e['name'] for e in errors]
        assert 'cpu_lockup_soft_lockup' in names
        err = next(e for e in errors if e['name'] == 'cpu_lockup_soft_lockup')
        assert err['category'] == 'cpu_lockup'
        assert err['severity'] == 'error'
        assert len(err['suggested_fixes']) > 0

    def test_hard_lockup_detected(self):
        """NMI watchdog hard lockup should be detected as critical."""
        serial = (
            "[  312.099873] Uhhuh. NMI received for unknown reason 3d on CPU 2.\n"
            "[  312.104501] NMI watchdog: Watchdog detected hard LOCKUP on cpu 2\n"
            "[  312.110276] Modules linked in: virtio_scsi virtio_pci virtio_ring\n"
            "[  312.118440] CPU: 2 PID: 887 Comm: kworker/2:1 Tainted: G L 5.15.0-91-generic\n"
            "[  312.127655] Hardware name: Google Google Compute Engine/Google Compute Engine\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        err = next(
            e for e in data['boot_errors']
            if e['name'] == 'cpu_lockup_hard_lockup'
        )
        assert err['severity'] == 'critical'

    def test_hard_lockup_detected_kernel_6_5_plus_prefix(self):
        """Red-team C2: kernel >= 6.5 (Ubuntu 24.04 = 6.8) logs the hard
        lockup with a 'watchdog:' prefix, not 'NMI watchdog:'. On GCE the
        buddy detector (6.5+ prefix) is the realistic reporter since guests
        have no PMU."""
        serial = (
            "[ 3600.200000] watchdog: Watchdog detected hard LOCKUP on cpu 2\n"
            "[ 3600.208113] Modules linked in: virtio_scsi virtio_pci\n"
        )
        data = _diagnose(serial)
        err = next(
            e for e in data['boot_errors']
            if e['name'] == 'cpu_lockup_hard_lockup'
        )
        assert err['severity'] == 'critical'

    def test_benign_watchdog_boot_lines_not_matched(self):
        """Healthy watchdog boot lines (present on every GCE boot) must not
        trigger the hard lockup pattern."""
        serial = (
            "[    0.512331] NMI watchdog: Perf NMI watchdog permanently disabled\n"
            "[    0.520114] watchdog: Delayed init of the lockup detector failed: -19\n"
            "[    0.527448] watchdog: Hard watchdog permanently disabled\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'cpu_lockup' not in categories

    def test_bus_lock_trap_detected(self):
        """Kernel split-lock-detection trap line should be detected as warning."""
        serial = (
            "[  102.328812] x86/split lock detection: warning about user-space bus_locks\n"
            "[  102.334455] x86/split lock detection: #DB: myapp/2211 took a bus_lock trap at address: 0x7f3c2a1b4d20\n"
            "[  102.341102] perf: interrupt took too long (2503 > 2500), lowering kernel.perf_event_max_sample_rate\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        err = next(
            e for e in data['boot_errors']
            if e['name'] == 'cpu_lockup_bus_lock'
        )
        assert err['severity'] == 'warning'

    def test_split_lock_phrase_detected(self):
        """Plain 'split lock detected' phrasing should also match."""
        serial = (
            "[   88.120933] core: split lock detected in workload process sampler/1877\n"
            "[   88.127544] core: this access severely degrades memory bus performance\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_bus_lock' in names

    def test_rcu_stall_detected(self):
        """rcu_sched stall report should be detected as error."""
        serial = (
            "[  455.220133] rcu: INFO: rcu_sched detected stalls on CPUs/tasks:\n"
            "[  455.226801] rcu: \t3-...0: (1 GPs behind) idle=8a2/1/0x4000000000000000 softirq=8412/8413 fqs=2626\n"
            "[  455.235477] rcu: \t(detected by 0, t=5252 jiffies, g=24829, q=1290)\n"
            "[  455.244103] Sending NMI from CPU 0 to CPUs 3:\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        err = next(
            e for e in data['boot_errors']
            if e['name'] == 'cpu_lockup_rcu_stall'
        )
        assert err['severity'] == 'error'

    def test_rcu_self_detected_stall_variant(self):
        """Older self-detected stall phrasing should match the same pattern."""
        serial = (
            "[  978.301220] INFO: rcu_sched self-detected stall on CPU { 1}  (t=5250 jiffies g=4294 c=4293 q=880)\n"
            "[  978.309912] Task dump for CPU 1:\n"
            "[  978.316733] cruncher        R  running task        0  2231      1 0x00000008\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_rcu_stall' in names

    def test_rcu_preempt_stall_detected(self):
        """Red-team C3: rcu_preempt is the default RCU flavor on Ubuntu
        22.04/24.04 (PREEMPT_DYNAMIC) — the most common modern GCE case."""
        serial = (
            "[  512.882314] rcu: INFO: rcu_preempt detected stalls on CPUs/tasks:\n"
            "[  512.890120] rcu: \t1-...!: (0 ticks this GP) idle=b6ac/0/0x0 softirq=9241/9241 fqs=0\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_rcu_stall' in names

    def test_rcu_stall_old_kernel_format_detected(self):
        """Red-team C3: kernels < 4.19 / RHEL 7 log without the 'rcu:'
        prefix."""
        serial = (
            "[  455.220133] INFO: rcu_sched detected stalls on CPUs/tasks: { 1} (detected by 0, t=60002 jiffies)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_rcu_stall' in names

    def test_rcu_expedited_stall_detected(self):
        """Red-team C3: expedited grace-period stalls should also match."""
        serial = (
            "[  600.101220] rcu: INFO: rcu_sched detected expedited stalls on CPUs/tasks: { 2-... } 6620 jiffies s: 141\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_rcu_stall' in names

    def test_rcu_preempt_self_detected_stall_detected(self):
        """Red-team C3: self-detected stall with the rcu_preempt flavor."""
        serial = (
            "[  711.400913] rcu: INFO: rcu_preempt self-detected stall on CPU\n"
            "[  711.408122] rcu: \t2-....: (5249 ticks this GP) idle=e3e/1/0x4000000000000002\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_rcu_stall' in names

    def test_healthy_rcu_boot_banners_not_matched(self):
        """Healthy RCU boot banners must not trigger the stall pattern."""
        serial = (
            "[    0.010220] rcu: Hierarchical RCU implementation.\n"
            "[    0.014331] rcu: \tRCU restricting CPUs from NR_CPUS=8192 to nr_cpu_ids=2.\n"
            "[    0.020144] rcu: Hierarchical SRCU implementation.\n"
            "[    0.031228] rcu: RCU calculated value of scheduler-enlistment delay is 25 jiffies.\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'cpu_lockup' not in categories

    def test_healthy_boot_matches_no_cpu_lockup_patterns(self):
        """A clean boot must not produce any cpu_lockup findings."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 (debian-kernel@lists.debian.org)\n"
            "[    1.204551] EXT4-fs (sda1): mounted filesystem with ordered data mode\n"
            "[    2.883104] systemd[1]: Detected virtualization google.\n"
            "login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestDiagnoseFilesystemRedTeamRegressions
# ---------------------------------------------------------------------------

class TestDiagnoseFilesystemRedTeamRegressions:
    """Regression tests for red-team findings against the filesystem category.

    Each test uses the exact failing serial line from the red-team report.
    """

    # Healthy "try mount, else mkfs" startup-script probe on a blank disk.
    # The VFS line lands AFTER the boot-success markers (the unit STARTS
    # before the script output), which defeated ordering-based suppression.
    _MKFS_PROBE_SERIAL = (
        "[  OK  ] Reached target multi-user.target - Multi-User System.\n"
        "[  OK  ] Started google-startup-scripts.service - Google Compute Engine Startup Scripts.\n"
        "[   15.221133] EXT4-fs (sdb): VFS: Can't find ext4 filesystem\n"
        "mke2fs 1.47.0 (5-Feb-2023)\n"
        "Creating filesystem with 2621440 4k blocks and 655360 inodes\n"
    )

    def test_mkfs_probe_on_running_vm_is_healthy(self):
        """C1: mount-probe-then-mkfs on a blank disk must not fire
        filesystem_bad_superblock, even when the VFS line lands after the
        last boot-success marker (RUNNING)."""
        result = analyze_serial_output(
            self._MKFS_PROBE_SERIAL, 'test-vm', 'zone-a', 'RUNNING'
        )
        assert result.diagnosis_status == 'healthy'
        assert result.boot_errors == []

    def test_mkfs_probe_on_terminated_vm_is_healthy(self):
        """C1: same buffer on a TERMINATED VM (boot-success suppression is
        skipped entirely) must also stay healthy — the anchored regex, not
        suppression, has to reject the probe."""
        result = analyze_serial_output(
            self._MKFS_PROBE_SERIAL, 'test-vm', 'zone-a', 'TERMINATED'
        )
        assert result.diagnosis_status == 'healthy'
        assert result.boot_errors == []

    def test_vfs_cant_find_ext4_anchored_match_is_single_line(self):
        """C1: the anchored regex must still fire on a real mount failure and
        keep detected_pattern single-line (lookahead, no multi-line blob)."""
        serial = (
            "         Mounting mnt-data.mount - /mnt/data...\n"
            "[    4.444497] EXT4-fs (sda): VFS: Can't find ext4 filesystem\n"
            "[FAILED] Failed to mount mnt-data.mount - /mnt/data.\n"
            "[DEPEND] Dependency failed for local-fs.target - Local File Systems.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'TERMINATED')
        errors = {e.name: e for e in result.boot_errors}
        assert 'filesystem_bad_superblock' in errors
        assert '\n' not in errors['filesystem_bad_superblock'].detected_pattern

    def test_xfs_metadata_corruption_on_device_mapper_detected(self):
        """C2: XFS corruption on device-mapper devices (dm-0, LVM on
        RHEL/SAP images) must fire filesystem_corruption; \\w+ missed the
        hyphen in the device name."""
        serial = (
            "[  102.5] XFS (dm-0): Metadata corruption detected at xfs_inode_buf_verify+0x15a/0x180 [xfs]\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'filesystem_corruption' in names

    def test_xfs_metadata_crc_error_detected(self):
        """C3: modern-kernel XFS 'Metadata CRC error detected' wording must
        fire filesystem_corruption."""
        serial = (
            "[   88.1] XFS (sda1): Metadata CRC error detected at xfs_agi_read_verify+0xd0/0xf0 [xfs], xfs_agi block 0x2\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'filesystem_corruption' in names

    def test_xfs_unmount_and_run_xfs_repair_detected(self):
        """C3: the canonical companion line emitted for both XFS corruption
        wordings must fire filesystem_corruption on its own."""
        serial = (
            "[   88.1] XFS (sdb1): Mounting V5 Filesystem\n"
            "[   88.2] XFS (sdb1): Unmount and run xfs_repair\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'filesystem_corruption' in names

    def test_corrupt_nofail_secondary_disk_survives_boot_success(self):
        """C4: a corrupt nofail secondary disk on a VM that boots fine must
        still be reported on RUNNING — boot-success suppression must not
        clear filesystem-category findings."""
        serial = (
            "[    5.1] EXT4-fs error (device sdb1): ext4_find_entry:1455: inode #2: comm systemd: reading directory lblock 0\n"
            "[  OK  ] Reached target multi-user.target - Multi-User System.\n"
            "[  OK  ] Startup finished in 5.2s.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'RUNNING')
        assert result.diagnosis_status == 'boot_errors_detected'
        names = [e.name for e in result.boot_errors]
        assert 'filesystem_corruption' in names

    def test_boot_success_still_clears_fstab_noise(self):
        """C4 guard: the exemption is filesystem-only — fstab timeout noise
        on a successfully booted RUNNING VM must still be cleared."""
        serial = (
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "[  OK  ] Reached target multi-user.target - Multi-User System.\n"
            "[  OK  ] Startup finished in 6.1s.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a', 'RUNNING')
        assert result.diagnosis_status == 'healthy'
        assert result.boot_errors == []
    def test_benign_nmi_line_not_matched(self):
        """'NMI received for unknown reason' alone is not a hard lockup."""
        serial = (
            "[  120.401220] Uhhuh. NMI received for unknown reason 31 on CPU 0.\n"
            "[  120.407733] Do you have a strange power saving mode enabled?\n"
            "[  120.414092] Dazed and confused, but trying to continue\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'cpu_lockup' not in categories

    def test_fstab_errors_do_not_trigger_cpu_lockup_patterns(self):
        """fstab failure lines must not produce cpu_lockup findings."""
        serial = (
            "[  241.693421] systemd[1]: Timed out waiting for device "
            "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "[  241.702118] systemd[1]: Dependency failed for /mnt/data\n"
            "[  241.710233] You are in emergency mode\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'cpu_lockup' not in categories
        assert 'fstab' in categories

    def test_cpu_lockup_lines_do_not_trigger_fstab_patterns(self):
        """Lockup lines must not produce fstab findings."""
        serial = (
            "[  241.693421] watchdog: BUG: soft lockup - CPU#3 stuck for 26s! [dd:2210]\n"
            "[  241.702118] CPU: 3 PID: 2210 Comm: dd Not tainted 6.1.0-18-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert categories == {'cpu_lockup'}

    def test_lockup_finding_does_not_suppress_emergency_mode(self):
        """cpu_lockup findings are runtime conditions, never a boot-config
        root cause, so they must NOT suppress the emergency-mode catch-all
        (tier-1 dedupe must ignore them)."""
        serial = (
            "[  312.104501] NMI watchdog: Watchdog detected hard LOCKUP on cpu 1\n"
            "[  320.220118] You are in emergency mode\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_hard_lockup' in names
        assert 'fstab_emergency_mode' in names

    def test_bus_lock_warning_does_not_suppress_emergency_mode(self):
        """Red-team C1: a warning-severity bus_lock finding must not hide a
        CRITICAL fstab emergency-mode finding."""
        serial = (
            "[  102.334455] x86/split lock detection: #DB: myapp/2211 took a bus_lock trap at address: 0x7f3c2a1b4d20\n"
            "[  180.220118] You are in emergency mode\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cpu_lockup_bus_lock' in names
        assert 'fstab_emergency_mode' in names
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['fstab_emergency_mode'] == 'critical'

    def test_bus_lock_warning_does_not_suppress_dependency_failures(self):
        """Red-team C1: a warning-severity bus_lock finding must not strip
        generic-symptom fstab criticals (tier-2 dedupe)."""
        serial = (
            "[  102.334455] x86/split lock detection: #DB: myapp/2211 took a bus_lock trap at address: 0x7f3c2a1b4d20\n"
            "[  150.101332] systemd[1]: Dependency failed for /mnt/disks/data.\n"
            "[  150.109221] systemd[1]: Dependency failed for File System Check on /dev/sdb1.\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'cpu_lockup' in categories
        assert 'fstab' in categories

    def test_recovered_lockup_before_boot_success_is_suppressed(self):
        """A lockup that happened BEFORE the last successful boot marker on a
        RUNNING VM is history: boot-success suppression clears it and the VM
        reports healthy. This is intentional — a recovered soft lockup needs
        no rescue action."""
        serial = (
            "[  241.693421] watchdog: BUG: soft lockup - CPU#0 stuck for 22s! [stress-ng-cpu:1523]\n"
            "[  241.702118] CPU: 0 PID: 1523 Comm: stress-ng-cpu Not tainted 6.1.0-18-cloud-amd64 #1\n"
            "[  400.101332] systemd[1]: Startup finished in 4.512s (kernel) + 11.204s (userspace) = 15.716s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestDiagnoseUnifiedEngineRedTeamRegressions
# ---------------------------------------------------------------------------

class TestDiagnoseUnifiedEngineRedTeamRegressions:
    """Regressions from the adversarial review of the unified diagnose
    engine (2.4.0 coverage integration). Each test reproduces a confirmed
    failing serial buffer from the red-team report. The _diagnose helper
    runs with vm_status RUNNING."""

    def test_full_disk_garbled_fsckd_line_not_flagged_as_fsck_failed(self):
        """LIVE (t-diskfull): on a full disk, journald interleaves two writes
        onto one physical serial line, e.g. a healthy 'systemd-fsckd.service:
        Deactivated succe...' fragment concatenated with journald's 'Failed
        to open ...: No space left on device'. The old greedy 'fsck.*failed'
        matched 'fsckd...Failed' across the garble -> false CRITICAL
        fstab_fsck_failed. disk_full must be the only fstab-family finding
        here (no fsck_failed)."""
        serial = (
            "systemd-journald[206]: Failed to open system journal: "
            "No space left on device\n"
            "syst[   33.575608] systemd-fsckd.service: Deactivated succe"
            "[   33.581290] systemd-journald[206]: Failed to open: "
            "No space left on device\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'disk_full_no_space' in names
        assert 'fstab_fsck_failed' not in names

    def test_healthy_fsckd_daemon_lines_not_flagged(self):
        """LIVE (t-diskfull): the fsck-to-fsckd socket/daemon startup lines are
        normal on every boot and must never match fstab_fsck_failed."""
        serial = (
            "[    3.302480] systemd[1]: Listening on systemd-fsckd.socket - "
            "fsck to fsckd communication Socket.\n"
            "systemd[1]: Started systemd-fsckd.service - File System Check "
            "Daemon to report status.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_fsck_failed' not in names

    def test_real_fsck_failure_still_detected(self):
        """Guard: tightening the regex must not lose true fsck failures."""
        for line in (
            "systemd-fsck[512]: fsck failed with exit status 4\n",
            "systemd-fsck[512]: fsck exited with status code 8\n",
            "[   12.3] EXT4-fs (sdb1): FILE SYSTEM CHECK FAILED\n",
        ):
            data = _diagnose("Linux version 6.1\n" + line)
            names = [e['name'] for e in data['boot_errors']]
            assert 'fstab_fsck_failed' in names, line

    def test_device_cannot_open_blockdev_detected_as_filesystem(self):
        """LIVE (t-fs): severe superblock+partition corruption makes the
        device unopenable, so serial shows '/dev/sda: Can't open blockdev'
        rather than 'Bad magic number'. That must surface as a filesystem
        finding (alongside the fstab mount failure)."""
        serial = (
            "[    4.129800] /dev/sda: Can't open blockdev\n"
            "[FAILED] Failed to mount mnt-data.mount - /mnt/data.\n"
        )
        data = _diagnose(serial)
        cats = {e['category'] for e in data['boot_errors']}
        assert 'filesystem' in cats
        assert 'fstab' in cats  # mount failure still reported too

    def test_lone_ssh_error_does_not_erase_emergency_mode(self):
        """C1: a single ssh auth error (survives-boot category, can never
        explain emergency mode) must not qualify as a Tier-1 root cause and
        delete the CRITICAL fstab_emergency_mode finding."""
        serial = (
            "You are in emergency mode. After logging in, type "
            '"journalctl -xb" to view system logs.\n'
            "sshd[401]: Authentication refused: bad ownership or modes "
            "for directory /home/bob/.ssh\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_emergency_mode' in names
        assert 'ssh_auth_permissions' in names
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['fstab_emergency_mode'] == 'critical'

    def test_lone_filesystem_error_does_not_erase_emergency_mode(self):
        """C1 side effect (safe direction): a filesystem finding alone also
        stops suppressing emergency mode — both findings are shown."""
        serial = (
            "[  152.208814] EXT4-fs error (device sdb1): "
            "ext4_find_entry:1455: inode #2: comm systemd: "
            "reading directory lblock 0\n"
            "You are in emergency mode. After logging in, type "
            '"journalctl -xb" to view system logs.\n'
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names
        assert 'fstab_emergency_mode' in names

    def test_post_marker_filesystem_error_does_not_retain_stale_fstab(self):
        """C2: a filesystem error AFTER the boot-success marker is exempt
        from suppression anyway, so its position must not veto the clearing
        of stale fstab noise from the previous failed boot."""
        serial = (
            "systemd[1]: Timed out waiting for device "
            "dev-disk-by\\x2duuid-6c78e5d3.device.\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
            "[FAILED] EXT4-fs error (device sdb1): ext4_find_entry:1463: "
            "inode #2: comm ls: reading directory lblock 0\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'filesystem' in categories
        assert 'fstab' not in categories

    def test_post_marker_ssh_failure_does_not_retain_stale_fstab(self):
        """C2: same veto bug via a post-marker sshd failure — the stale
        fstab device timeout from the old boot must be cleared."""
        serial = (
            "systemd[1]: Timed out waiting for device "
            "dev-disk-by\\x2duuid-6c78e5d3.device.\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
            "systemd[1]: Failed to start ssh.service - OpenBSD Secure "
            "Shell server.\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'ssh' in categories
        assert 'fstab' not in categories

    def test_kernel_panic_does_not_suppress_emergency_mode(self):
        """C3 (engine side): kernel is detect-only — a panic must not act
        as a Tier-1 root cause and hide a stale emergency-mode finding from
        another boot; both findings are shown."""
        serial = (
            "You are in emergency mode. After logging in, type "
            '"journalctl -xb" to view system logs.\n'
            "-- reboot --\n"
            "[   12.310221] Kernel panic - not syncing: Attempted to kill "
            "the idle task!\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_generic' in names
        assert 'fstab_emergency_mode' in names

    def test_recovered_panic_before_boot_success_is_suppressed(self):
        """C3 side effect: a panic BEFORE the last successful boot marker on
        a RUNNING VM is history and is cleared — same intentional precedent
        as the recovered-lockup suppression."""
        serial = (
            "[   30.104501] Kernel panic - not syncing: Attempted to kill "
            "init! exitcode=0x00007f00\n"
            "-- reboot --\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_disk_full_survives_completed_boot(self):
        """C4: a boot-success marker does not empty a full disk — the
        disk_full finding must survive suppression on a RUNNING VM instead
        of reporting healthy."""
        serial = (
            "systemd-journald[300]: Failed to write entry: "
            "No space left on device\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'disk_full_no_space' in names

    def test_post_marker_disk_full_does_not_retain_stale_fstab(self):
        """C4 secondary effect: disk_full lines AFTER the marker must not
        keep stale fstab noise alive (they are exempt from suppression, so
        their position must not veto the clearing)."""
        serial = (
            "systemd[1]: Timed out waiting for device "
            "dev-disk-by\\x2duuid-6c78e5d3.device.\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
            "systemd-journald[300]: Failed to write entry: "
            "No space left on device\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'disk_full' in categories
        assert 'fstab' not in categories

    def test_disk_full_detected_pattern_has_no_trailing_newline(self):
        """Minor: the anchored ENOSPC regex must stop at end-of-line so the
        newline never leaks into detected_pattern / JSON output."""
        serial = (
            "systemd-journald[300]: Failed to write entry: "
            "No space left on device\n"
        )
        data = _diagnose(serial)
        patterns = {
            e['name']: e['detected_pattern'] for e in data['boot_errors']
        }
        assert 'disk_full_no_space' in patterns
        assert '\n' not in patterns['disk_full_no_space']


# ---------------------------------------------------------------------------
# TestGrubDetection
# ---------------------------------------------------------------------------

class TestGrubDetection:
    """Detection tests for grub.yaml patterns (bootloader stage).

    GRUB serial output carries no kernel timestamps and no systemd
    prefixes — fixtures reproduce the real bare-line formatting.
    """

    def test_grub_rescue_prompt_detected(self):
        """Drop to 'grub rescue>' reports the rescue prompt and its cause."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: no such partition.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_rescue_prompt' in names
        assert 'grub_no_such_partition' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert categories == {'grub'}
        assert all(e['severity'] == 'critical' for e in data['boot_errors'])

    def test_grub_normal_shell_detected(self):
        """Minimal-BASH banner means boot stopped at the grub> shell."""
        serial = (
            "GNU GRUB  version 2.06-13+deb12u1\n"
            "\n"
            "Minimal BASH-like line editing is supported. For the first "
            "word, TAB\n"
            "lists possible command completions. Anywhere else TAB lists "
            "possible\n"
            "device or file completions.\n"
            "\n"
            "grub> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_normal_shell' in names

    def test_grub_config_not_found_detected(self):
        """Missing grub.cfg (Debian /boot/grub path)."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: file `/boot/grub/grub.cfg' not found.\n"
            "grub> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_file_not_found' in names

    def test_grub2_config_path_variant_detected(self):
        """RHEL/SLES use /boot/grub2/grub.cfg — the regex must accept both."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: file `/boot/grub2/grub.cfg' not found.\n"
            "grub> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_file_not_found' in names

    def test_rhel_prefixed_grub_errors_detected(self):
        """RHEL/Fedora grub2 prefixes every error with its source location
        (error: ../../grub-core/<file>.c:<line>:<message>). Red-team C1:
        these exact lines produced ZERO findings before the optional
        (?:\\S+\\.c:\\d+:)? prefix was added to every message-bound regex."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: ../../grub-core/fs/fshelp.c:258:file "
            "`/vmlinuz-4.18.0-513.el8_9.x86_64' not found.\n"
            "error: ../../grub-core/loader/i386/efi/linux.c:94:you need to "
            "load the kernel first.\n"
            "\n"
            "Press any key to continue...\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_kernel_not_found' in names
        assert 'grub_load_kernel_first' in names

    def test_rhel_prefixed_filesystem_and_disk_errors_detected(self):
        """Remaining RHEL-prefixed forms from the red-team C1 corpus."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: ../../grub-core/kern/fs.c:120:unknown filesystem.\n"
            "error: ../../grub-core/kern/disk.c:236:disk `hd0,gpt2' "
            "not found.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_unknown_filesystem' in names
        assert 'grub_disk_not_found' in names
        assert 'grub_rescue_prompt' in names

    def test_benign_rhel_grubenv_line_not_flagged(self):
        """The well-known BENIGN RHEL-on-XFS line (grubenv unwritable, boot
        continues fine) must NOT fire grub_file_not_found — without the
        (?!grubenv) exclusion this is a critical FP on healthy RHEL VMs."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "error: ../../grub-core/fs/fshelp.c:257:file "
            "`/boot/grub2/grubenv' not found.\n"
            "[    0.000000] Linux version 4.18.0-513.el8_9.x86_64\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_unprefixed_grubenv_line_not_flagged(self):
        """Same grubenv exclusion for the unprefixed (Debian-style) form,
        with no completed boot in the buffer — proves the lookahead alone
        (not boot-success suppression) rejects the line."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "error: file `/boot/grub2/grubenv' not found.\n"
        )
        data = _diagnose(serial)
        assert data['boot_errors'] == []

    def test_broken_grub2_module_still_flagged_despite_grubenv_exclusion(self):
        """The (?!grubenv) lookahead must not swallow real /boot/grub2/*
        failures (e.g. a missing module)."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: ../../grub-core/fs/fshelp.c:258:file "
            "`/boot/grub2/i386-pc/normal.mod' not found.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_file_not_found' in names

    def test_grub_kernel_not_found_detected(self):
        """Missing vmlinuz referenced by grub.cfg, plus the follow-up
        'you need to load the kernel first' error."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: file `/boot/vmlinuz-6.1.0-18-cloud-amd64' not found.\n"
            "error: you need to load the kernel first.\n"
            "\n"
            "Press any key to continue...\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_kernel_not_found' in names
        assert 'grub_load_kernel_first' in names

    def test_grub_initrd_not_found_detected(self):
        """Missing initrd referenced by grub.cfg."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: file `/boot/initrd.img-6.1.0-18-cloud-amd64' not found.\n"
            "\n"
            "Press any key to continue...\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_kernel_not_found' in names

    def test_grub_symbol_not_found_detected(self):
        """Core/module version skew (e.g. grub_calloc after an upgrade)."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: symbol `grub_calloc' not found.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_symbol_not_found' in names

    def test_grub_disk_not_found_detected(self):
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: disk `hd0,gpt2' not found.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_disk_not_found' in names

    def test_grub_unknown_filesystem_detected(self):
        """GRUB 'error: unknown filesystem' is grub-category, and must not
        cross-fire into the filesystem category."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: unknown filesystem.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_unknown_filesystem' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'filesystem' not in categories

    def test_grub_out_of_memory_detected(self):
        """GRUB OOM is bound to the 'error:' prefix and must never be
        confused with kernel OOM (kernel/disk_full/oom categories)."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "\n"
            "error: out of memory.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_out_of_memory' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' not in categories
        assert 'disk_full' not in categories
        # Wave 4: the oom category's regexes are bound to kernel OOM-killer
        # wording (PID / gfp_mask) - the GRUB 'error: out of memory.' line
        # must not fire it either.
        assert 'oom' not in categories

    def test_grub_invalid_magic_detected(self):
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: invalid magic number.\n"
            "error: you need to load the kernel first.\n"
            "\n"
            "Press any key to continue...\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'grub_invalid_magic' in names

    def test_healthy_boot_with_grub_banner_no_findings(self):
        """The normal 'Welcome to GRUB!' banner on every healthy boot must
        not fire any grub pattern (FP guard from the master plan)."""
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Booting from Hard Disk 0...\n"
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64 "
            "(debian-kernel@lists.debian.org)\n"
            "[    0.512345] Trying to unpack rootfs image as initramfs...\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode. Quota mode: none.\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_kernel_oom_lines_do_not_trigger_grub(self):
        """Kernel OOM output (timestamp-prefixed 'Out of memory') must not
        match grub_out_of_memory — proves the 'error:' line-start binding.

        (Wave 4: the same buffer now legitimately reports the oom
        category — the guard here is only that grub must not fire.)"""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  512.103311] Out of memory: Killed process 1234 (java) "
            "total-vm:8388608kB\n"
            "[  512.209972] Kernel panic - not syncing: Out of memory: "
            "system-wide panic_on_oom is enabled\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'grub' not in categories
        assert 'oom' in categories

    def test_mount_unknown_filesystem_type_does_not_trigger_grub(self):
        """The kernel/mount 'unknown filesystem type' wording never starts a
        serial line with 'error:', so grub_unknown_filesystem must not fire."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    3.204333] mount[456]: mount: /data: unknown filesystem "
            "type 'xfs'.\n"
            "[    3.304333] systemd[1]: data.mount: Mount process exited, "
            "code=exited, status=32/n/a\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'grub' not in categories

    def test_fstab_errors_do_not_trigger_grub_or_firmware(self):
        """fstab failure lines should not produce grub/firmware findings."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678\n"
            "Dependency failed for /mnt/data\n"
            "You are in emergency mode\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'grub' not in categories
        assert 'firmware' not in categories

    def test_stale_grub_error_cleared_by_later_successful_boot(self):
        """The serial ring buffer keeps old boots: a grub failure followed
        by a later completed boot on a RUNNING VM is stale noise and must
        be suppressed (grub does not declare survives_boot_success)."""
        serial = (
            "GRUB loading.\n"
            "Welcome to GRUB!\n"
            "error: no such partition.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Booting from Hard Disk 0...\n"
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestFirmwareDetection
# ---------------------------------------------------------------------------

class TestFirmwareDetection:
    """Detection tests for firmware.yaml patterns (BIOS/UEFI stage)."""

    def test_bios_no_bootable_device_detected(self):
        """SeaBIOS wiped-MBR failure reports firmware_no_bootable_device."""
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Machine UUID 12345678-1234-1234-1234-123456789abc\n"
            "Booting from Hard Disk 0...\n"
            "Boot failed: not a bootable disk\n"
            "\n"
            "No bootable device.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_no_bootable_device' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert categories == {'firmware'}

    def test_bios_could_not_read_boot_disk_detected(self):
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Machine UUID 12345678-1234-1234-1234-123456789abc\n"
            "Booting from Hard Disk 0...\n"
            "Boot failed: could not read the boot disk\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_no_bootable_device' in names

    def test_uefi_bds_load_failure_detected(self):
        """OVMF BdsDxe failure (missing/unformatted ESP or lost NVRAM entry)."""
        serial = (
            "UEFI: Attempting to start image.\n"
            "Description: UEFI Google PersistentDisk\n"
            "BdsDxe: failed to load Boot0001 \"UEFI Google PersistentDisk\" "
            "from PciRoot(0x0)/Pci(0x3,0x0)/Scsi(0x1,0x0): Not Found\n"
            "BdsDxe: No bootable option or device was found.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_uefi_boot_load_failed' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert categories == {'firmware'}

    def test_secure_boot_violation_detected(self):
        """Shielded VM Secure Boot rejection — the OVMF verification line.
        A 'loading Boot0000' line must not fire the failed-to-load pattern."""
        serial = (
            "BdsDxe: loading Boot0000 \"UEFI Google PersistentDisk\" "
            "from PciRoot(0x0)/Pci(0x3,0x0)/Scsi(0x1,0x0)\n"
            "Verification failed: (0x1A) Security Violation\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_secure_boot_violation' in names
        assert 'firmware_uefi_boot_load_failed' not in names

    def test_bad_shim_signature_detected(self):
        """shim/GRUB-emitted Secure Boot rejection, with the realistic
        follow-up grub error on the same screen (cross-category pairing)."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: bad shim signature.\n"
            "error: you need to load the kernel first.\n"
            "\n"
            "Press any key to continue...\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_secure_boot_violation' in names
        assert 'grub_load_kernel_first' in names

    def test_rhel_prefixed_bad_shim_signature_detected(self):
        """RHEL grub2 source-location prefix on the shim rejection line
        (red-team C1 companion fix in firmware.yaml)."""
        serial = (
            "GNU GRUB  version 2.06\n"
            "\n"
            "error: ../../grub-core/kern/verifiers.c:119:bad shim "
            "signature.\n"
            "error: ../../grub-core/loader/i386/efi/linux.c:94:you need to "
            "load the kernel first.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_secure_boot_violation' in names
        assert 'grub_load_kernel_first' in names

    def test_prose_mention_of_no_bootable_device_not_flagged(self):
        """Red-team C4: a startup-script echo merely MENTIONING the phrase
        mid-line must not fire firmware_no_bootable_device (start anchor)."""
        serial = (
            "[   12.345678] startup-script: INFO Watching for the "
            "'No bootable device' marker in guest logs\n"
            "[   13.000000] startup-script: INFO handler installed\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'firmware' not in categories

    def test_glued_seabios_no_bootable_device_still_detected(self):
        """Live S4 capture: SeaBIOS glues its banner onto the failure line
        without a newline ('No bootable device.SeaBIOS ...'). The phrase
        still starts the line, so the start anchor must keep matching."""
        serial = (
            "Booting from Hard Disk 0...\n"
            "Boot failed: not a bootable disk\n"
            "\n"
            "No bootable device.SeaBIOS (version 1.8.2-google)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_no_bootable_device' in names

    def test_gpt_corrupt_detected(self):
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Machine UUID 12345678-1234-1234-1234-123456789abc\n"
            "Booting from Hard Disk 0...\n"
            "Invalid partition table!\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'firmware_gpt_corrupt' in names

    def test_healthy_seabios_boot_no_firmware_findings(self):
        """'Booting from Hard Disk...' prints on EVERY healthy SeaBIOS boot
        and must never fire firmware patterns (critic FP correction)."""
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Machine UUID 12345678-1234-1234-1234-123456789abc\n"
            "Booting from Hard Disk 0...\n"
            "Welcome to GRUB!\n"
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode. Quota mode: none.\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_resized_disk_gpt_warning_not_flagged(self):
        """The kernel's 'GPT: Primary header thinks Alt. header is not at
        the end of the disk' warning fires on every resized-but-unexpanded
        PD while the VM boots fine — it must NOT be reported (critic C1)."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    1.234567] GPT:Primary header thinks Alt. header is not at "
            "the end of the disk.\n"
            "[    1.234789] GPT:41943039 != 62914559\n"
            "[    1.234901] GPT: Use GNU Parted to correct GPT errors.\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode. Quota mode: none.\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_stale_firmware_error_cleared_by_later_successful_boot(self):
        """A firmware failure from an older boot followed by a completed
        boot must be suppressed on a RUNNING VM (stale ring-buffer noise)."""
        serial = (
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Booting from Hard Disk 0...\n"
            "No bootable device.\n"
            "SeaBIOS (version 1.8.2-20231011_165638-google)\n"
            "Booting from Hard Disk 0...\n"
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestDracutInitramfsDetection (Wave 2: RHEL/Rocky/Alma/SLES dialect)
# ---------------------------------------------------------------------------

class TestDracutInitramfsDetection:
    """Detection tests for the dracut initramfs dialect (RHEL-family).

    Fixtures reproduce real Rocky 9 serial formatting: kernel timestamps
    on dracut-initqueue lines, bare lines for the emergency shell.
    """

    ROCKY_DRACUT_TIMEOUT_SERIAL = (
        "[  OK  ] Reached target Basic System.\n"
        "[  135.209316] dracut-initqueue[550]: Warning: dracut-initqueue "
        "timeout - starting timeout scripts\n"
        "[  135.719813] dracut-initqueue[550]: Warning: dracut-initqueue "
        "timeout - starting timeout scripts\n"
        "[  191.964618] dracut-initqueue[550]: Warning: Could not boot.\n"
        "[  191.976595] dracut-initqueue[550]: Warning: /dev/disk/by-uuid/"
        "00000000-0000-0000-0000-000000000bad does not exist\n"
        "         Starting Dracut Emergency Shell...\n"
        "Warning: /dev/disk/by-uuid/00000000-0000-0000-0000-000000000bad "
        "does not exist\n"
        "\n"
        "Generating \"/run/initramfs/rdsosreport.txt\"\n"
        "\n"
        "Entering emergency mode. Exit the shell to continue.\n"
        "Type \"journalctl\" to view system logs.\n"
        "You might want to save \"/run/initramfs/rdsosreport.txt\" to a "
        "USB stick or /boot\n"
        "after mounting them and attach it to a bug report.\n"
        "\n"
        "dracut:/# \n"
    )

    def test_rocky_dracut_timeout_and_emergency_detected(self):
        """Full Rocky 9 bad-root-UUID buffer reports timeout + emergency."""
        data = _diagnose(self.ROCKY_DRACUT_TIMEOUT_SERIAL)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_dracut_timeout' in names
        assert 'initramfs_dracut_emergency' in names

    def test_dracut_emergency_dedupes_fstab_emergency_mode(self):
        """Overlap guard: the 'Entering emergency mode' line on a dracut
        buffer fires fstab_emergency_mode (catch-all), but Tier-1 dedupe
        must demote it in favor of the dracut root-cause findings --
        never two findings for the same emergency state."""
        data = _diagnose(self.ROCKY_DRACUT_TIMEOUT_SERIAL)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_emergency_mode' not in names

    def test_dracut_emergency_alone_still_dedupes_fstab_emergency(self):
        """Truncated buffer with only the emergency-shell tail: the dracut
        emergency finding is the root cause; fstab_emergency_mode stays
        deduped (no double-fire on 'Entering emergency mode')."""
        serial = (
            "Generating \"/run/initramfs/rdsosreport.txt\"\n"
            "\n"
            "Entering emergency mode. Exit the shell to continue.\n"
            "Type \"journalctl\" to view system logs.\n"
            "dracut:/# \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_dracut_emergency' in names
        assert 'fstab_emergency_mode' not in names

    def test_dracut_lines_do_not_fire_kernel_category(self):
        """No panic string in the dracut buffer -- kernel must stay silent."""
        data = _diagnose(self.ROCKY_DRACUT_TIMEOUT_SERIAL)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' not in categories

    def test_dracut_fatal_detected(self):
        serial = (
            "[    2.113305] dracut: FATAL: FIPS integrity test failed\n"
            "[    2.113400] dracut: Refusing to continue\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_dracut_fatal' in names

    def test_sysroot_mount_failure_detected_as_initramfs_not_fstab(self):
        """RHEL '/sysroot' failure is the initrd stage: it must report
        initramfs_sysroot_mount_failed and must NOT fire the fstab
        mount/dependency patterns (the (?!sysroot) carve-outs)."""
        serial = (
            "[    4.523310] XFS (sda4): Corruption warning: Metadata has "
            "LSN ahead of current LSN\n"
            "[FAILED] Failed to mount /sysroot.\n"
            "See 'systemctl status sysroot.mount' for details.\n"
            "[DEPEND] Dependency failed for Initrd Root File System.\n"
            "[DEPEND] Dependency failed for Reload Configuration from the "
            "Real Root.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_sysroot_mount_failed' in names
        assert 'fstab_mount_failed' not in names
        assert 'fstab_dependency_failed' not in names

    def test_nvme_rename_sd_device_reports_guidance(self):
        """dracut waiting for a legacy /dev/sdX device on an NVMe machine
        family reports the rename guidance pattern (UUID advice)."""
        serial = (
            "[  138.210044] dracut-initqueue[544]: Warning: dracut-initqueue "
            "timeout - starting timeout scripts\n"
            "[  138.220000] dracut-initqueue[544]: Warning: /dev/sda1 does "
            "not exist\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_nvme_device_rename' in names
        finding = next(e for e in data['boot_errors']
                       if e['name'] == 'initramfs_nvme_device_rename')
        fixes = ' '.join(finding['suggested_fixes']).lower()
        assert 'nvme' in fixes
        assert 'uuid' in fixes

    def test_healthy_rocky_boot_no_initramfs_findings(self):
        """Healthy Rocky 9 boot (normal dracut hooks) must report healthy."""
        serial = (
            "[    0.000000] Linux version 5.14.0-427.13.1.el9_4.x86_64 "
            "(mockbuild@x86-64-01.stream)\n"
            "[    1.612345] dracut-cmdline[214]: dracut-057-53.git20240104."
            "el9_4 dracut\n"
            "[    2.412345] XFS (sda4): Mounting V5 Filesystem\n"
            "[    2.512345] XFS (sda4): Ending clean mount\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
            "rocky9 login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestBusyBoxInitramfsDetection (Wave 2: Debian/Ubuntu dialect)
# ---------------------------------------------------------------------------

class TestBusyBoxInitramfsDetection:
    """Detection tests for the BusyBox initramfs dialect (Debian-family).

    BusyBox output is bare lines with no timestamps or unit prefixes.
    """

    def test_busybox_alert_and_prompt_detected(self):
        serial = (
            "Begin: Waiting for root file system ... Begin: Running "
            "/scripts/local-block ... done.\n"
            "done.\n"
            "Gave up waiting for root file system device.  Common problems:\n"
            " - Boot args (cat /proc/cmdline)\n"
            "   - Check rootdelay= (did the system wait long enough?)\n"
            " - Missing modules (cat /proc/modules; ls /dev)\n"
            "ALERT!  /dev/disk/by-uuid/deadbeef-cafe-4bad-8bad-2bad2bad2bad "
            "does not exist.  Dropping to a shell!\n"
            "\n"
            "BusyBox v1.35.0 (Debian 1:1.35.0-4+b3) built-in shell (ash)\n"
            "Enter 'help' for a list of built-in commands.\n"
            "\n"
            "(initramfs) \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_busybox_shell' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' not in categories

    def test_bare_initramfs_prompt_detected(self):
        """A truncated buffer ending at the bare '(initramfs)' prompt line
        must still be detected (line-start anchored, parens literal)."""
        serial = (
            "BusyBox v1.30.1 (Ubuntu 1:1.30.1-7ubuntu3) built-in shell "
            "(ash)\n"
            "Enter 'help' for a list of built-in commands.\n"
            "\n"
            "(initramfs) \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_busybox_shell' in names

    def test_busybox_prompt_match_has_no_trailing_newline(self):
        """The prompt regex uses [ \\t]*$ so the newline never leaks into
        detected_pattern / JSON output."""
        serial = (
            "BusyBox v1.35.0 (Debian 1:1.35.0-4+b3) built-in shell (ash)\n"
            "Enter 'help' for a list of built-in commands.\n"
            "(initramfs) \n"
        )
        data = _diagnose(serial)
        patterns = {e['name']: e['detected_pattern']
                    for e in data['boot_errors']}
        assert 'initramfs_busybox_shell' in patterns
        assert '\n' not in patterns['initramfs_busybox_shell']

    def test_gave_up_waiting_old_wording_detected(self):
        """Older initramfs-tools prints 'Gave up waiting for root device.'"""
        serial = (
            "Begin: Running /scripts/local-premount ... done.\n"
            "Gave up waiting for root device.  Common problems:\n"
            " - Boot args (cat /proc/cmdline)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_busybox_shell' in names

    def test_healthy_debian_initramfs_lines_not_flagged(self):
        """Normal initramfs-tools Begin/done chatter plus a completed boot
        must not fire any initramfs pattern."""
        serial = (
            "Begin: Loading essential drivers ... done.\n"
            "Begin: Running /scripts/init-premount ... done.\n"
            "Begin: Mounting root file system ... done.\n"
            "Begin: Running /scripts/init-bottom ... done.\n"
            "[    2.412345] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode. Quota mode: none.\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestInitramfsUnpackAndRootDevice (Wave 2: unpack variants + VFS form)
# ---------------------------------------------------------------------------

class TestInitramfsUnpackAndRootDevice:
    """Unpack-failure variants and the non-panic root-device form."""

    def test_invalid_magic_detected(self):
        serial = (
            "[    0.000000] Linux version 5.14.0-427.13.1.el9_4.x86_64\n"
            "[    0.905566] Initramfs unpacking failed: invalid magic at "
            "start of compressed archive\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_load_failure' in names

    def test_failed_to_decompress_bound_to_initramfs(self):
        serial = (
            "[    0.000000] Linux version 5.14.0-427.13.1.el9_4.x86_64\n"
            "[    0.905566] Failed to decompress external initrd "
            "(/boot/initramfs-5.14.0.img)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_load_failure' in names

    def test_unrelated_decompress_failure_not_flagged(self):
        """A 'Failed to decompress' line without initrd/initramfs context
        (e.g. a firmware blob) must not fire initramfs_load_failure."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    3.101010] amdgpu: Failed to decompress firmware blob\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'initramfs' not in categories

    def test_vfs_cannot_open_root_device_nonpanic_detected(self):
        """The non-panic 'VFS: Cannot open root device' form (kernel keeps
        retrying or panics much later) must report initramfs, and the
        kernel catch-all must stay silent (no panic line present)."""
        serial = (
            "[    1.612345] VFS: Cannot open root device "
            "\"PARTUUID=abcd1234-01\" or unknown-block(0,0): error -6\n"
            "[    1.702211] Please append a correct \"root=\" boot option; "
            "here are the available partitions:\n"
            "[    1.750000] 0800     10485760 sda driver: sd\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_no_root_fs' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' not in categories

    def test_vfs_panic_form_still_single_initramfs_finding(self):
        """When both the Cannot-open line and the Unable-to-mount panic are
        present, initramfs_no_root_fs reports once and kernel_panic_generic
        stays excluded (lookahead unchanged by the new regex)."""
        serial = (
            "[    1.612345] VFS: Cannot open root device \"sda1\" or "
            "unknown-block(0,0): error -6\n"
            "[    1.803992] Kernel panic - not syncing: VFS: Unable to "
            "mount root fs on unknown-block(0,0)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert names.count('initramfs_no_root_fs') == 1
        assert 'kernel_panic_generic' not in names


# ---------------------------------------------------------------------------
# TestLvmDetection (Wave 2 new category)
# ---------------------------------------------------------------------------

class TestLvmDetection:
    """Detection tests for lvm.yaml patterns."""

    def test_vg_not_found_detected(self):
        serial = (
            "[    2.113305] dracut-cmdline[214]: dracut-057-53.git20240104"
            ".el9_4\n"
            "Volume group \"vg_root\" not found\n"
            "Cannot process volume group vg_root\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'lvm_vg_not_found' in names

    def test_escaped_mapper_timeout_is_lvm_not_fstab(self):
        """dev-mapper device timeout (systemd \\x2d escaping) must report
        lvm_device_timeout and must NOT fire fstab_device_timeout (the
        (?!mapper) carve-outs)."""
        serial = (
            "systemd[1]: dev-mapper-vg\\x2droot.device: Job dev-mapper-"
            "vg\\x2droot.device/start timed out.\n"
            "systemd[1]: Timed out waiting for device dev-mapper-"
            "vg\\x2droot.device - /dev/mapper/vg-root.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'lvm_device_timeout' in names
        assert 'fstab_device_timeout' not in names

    def test_plain_mapper_path_timeout_is_lvm_not_fstab(self):
        serial = (
            "systemd[1]: Timed out waiting for device /dev/mapper/"
            "vgdata-lvdata.\n"
            "[DEPEND] Dependency failed for /srv/data.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'lvm_device_timeout' in names
        assert 'fstab_device_timeout' not in names

    def test_dracut_mapper_wait_is_lvm_not_initramfs(self):
        """dracut waiting on /dev/mapper/* is an LVM activation failure:
        lvm.yaml owns it; the initramfs dracut regex excludes mapper."""
        serial = (
            "[  138.220000] dracut-initqueue[544]: Warning: "
            "/dev/mapper/rhel-root does not exist\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'lvm_dracut_device_missing' in names
        assert 'initramfs_dracut_timeout' not in names
        assert 'initramfs_nvme_device_rename' not in names

    def test_healthy_lvm_boot_not_flagged(self):
        serial = (
            "[    2.104501] lvm[321]: 1 logical volume(s) in volume group "
            "\"vg00\" now active\n"
            "Found volume group \"vg00\" using metadata type lvm2\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestCryptDetection (Wave 2 new category, detect-only)
# ---------------------------------------------------------------------------

class TestCryptDetection:
    """Detection tests for crypt.yaml patterns (LUKS hangs)."""

    def test_passphrase_prompt_detected(self):
        """The prompt itself is the failure evidence (interactive hang)."""
        serial = (
            "[    4.104501] systemd[1]: Starting Cryptography Setup for "
            "luks-2f4c...\n"
            "Please enter passphrase for disk luks-2f4c8e11 on /: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'crypt_passphrase_prompt' in names

    def test_please_unlock_disk_wording_detected(self):
        serial = (
            "systemd[1]: Starting Cryptography Setup for cr_root...\n"
            "Please unlock disk cr_root: \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'crypt_passphrase_prompt' in names

    def test_crypt_start_job_detected(self):
        serial = (
            "[ ***  ] A start job is running for Cryptography Setup for "
            "luks-2f4c8e11 (1min 30s / no limit)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'crypt_unlock_wait' in names

    def test_benign_start_job_not_flagged(self):
        """FP guard: 'A start job is running for' is a benign transient on
        every boot -- it must ONLY match when bound to crypt/LUKS wording,
        even with no boot-success marker in the buffer."""
        serial = (
            "[  *** ] A start job is running for Wait for Network to be "
            "Configured (9s / 2min 30s)\n"
            "[ ***  ] A start job is running for dev-sdb1.device "
            "(5s / 1min 30s)\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'crypt' not in categories

    def test_cryptsetup_failure_bound_wording_detected(self):
        serial = (
            "[FAILED] Failed to start Cryptography Setup for luks-2f4c.\n"
            "See 'systemctl status systemd-cryptsetup@luks.service'.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'crypt_setup_failed' in names

    def test_healthy_cryptsetup_start_not_flagged(self):
        """Bare systemd-cryptsetup unit chatter on a healthy encrypted-disk
        boot must not fire (patterns are bound to failure wording)."""
        serial = (
            "systemd[1]: Started systemd-cryptsetup@luks-2f4c.service - "
            "Cryptography Setup for luks-2f4c.\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestRaidDetection (Wave 2 new category)
# ---------------------------------------------------------------------------

class TestRaidDetection:
    """Detection tests for raid.yaml patterns (mdadm)."""

    def test_dirty_degraded_array_detected(self):
        serial = (
            "[    3.104501] md/raid:md0: not enough operational devices "
            "(2/4 failed)\n"
            "[    3.104999] md: pers->run() failed ...\n"
            "[    3.105501] md0: Cannot start dirty degraded array.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_dirty_degraded' in names

    def test_raid1_mirrors_wording_detected(self):
        serial = (
            "[    3.104501] md/raid1:md127: not enough operational mirrors.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_dirty_degraded' in names

    def test_mdadm_unable_to_start_detected(self):
        serial = (
            "mdadm: Unable to start array /dev/md0: Input/output error\n"
            "[DEPEND] Dependency failed for /srv/raid.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_start_failed' in names

    def test_healthy_raid_boot_not_flagged(self):
        serial = (
            "[    3.104501] md/raid1:md0: active with 2 out of 2 mirrors\n"
            "[    3.204501] md0: detected capacity change from 0 to "
            "1073741824\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestMachineIdDetection (Wave 2 new category)
# ---------------------------------------------------------------------------

class TestMachineIdDetection:
    """Detection tests for machine_id.yaml (badly cloned images)."""

    def test_machine_id_unreadable_detected(self):
        serial = (
            "systemd[1]: Failed to read /etc/machine-id: No such file or "
            "directory\n"
            "dbus-daemon[512]: Failed to open \"/var/lib/dbus/machine-id\": "
            "No such file or directory\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'machine_id_missing' in names

    def test_could_not_get_machine_id_detected(self):
        serial = (
            "Linux version 6.1.0-18-cloud-amd64 (debian-kernel@lists)\n"
            "dbus[401]: Could not get machine ID: unable to load "
            "/var/lib/dbus/machine-id\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'machine_id_missing' in names

    def test_machine_id_cleared_by_completed_boot(self):
        """Boot continues with a transient ID, so a completed later boot
        clears the finding (no survives_boot_success)."""
        serial = (
            "systemd[1]: Failed to read /etc/machine-id: No such file or "
            "directory\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'


# ---------------------------------------------------------------------------
# TestFilesystemDistroBroadening (Wave 2: Btrfs, XFS log, ext4 features)
# ---------------------------------------------------------------------------

class TestFilesystemDistroBroadening:
    """Detection tests for the Wave 2 filesystem.yaml additions."""

    def test_btrfs_open_ctree_failed_detected(self):
        """openSUSE Btrfs root corruption (open_ctree) reports filesystem."""
        serial = (
            "[    4.121004] BTRFS error (device sda2): bad tree block "
            "start, want 268435456 have 0\n"
            "[    4.122500] BTRFS error (device sda2): failed to read "
            "chunk root\n"
            "[    4.124904] BTRFS error (device sda2): open_ctree failed\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_btrfs_corruption' in names

    def test_btrfs_chunk_tree_and_transid_detected(self):
        serial = (
            "[    4.121004] BTRFS: failed to read chunk tree on sda2\n"
            "[    4.122500] parent transid verify failed on 30408704 "
            "wanted 4096 found 4098\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_btrfs_corruption' in names

    def test_healthy_btrfs_info_lines_not_flagged(self):
        """BTRFS info chatter on healthy SUSE boots must not fire the
        corruption pattern."""
        serial = (
            "[    2.121004] BTRFS info (device sda2): using crc32c "
            "(crc32c-intel) checksum algorithm\n"
            "[    2.122500] BTRFS info (device sda2): enabling ssd "
            "optimizations\n"
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_xfs_log_recovery_failure_detected(self):
        serial = (
            "[    3.812345] XFS (sda4): Starting recovery (logdev: "
            "internal)\n"
            "[    3.912345] XFS (sda4): log mount/recovery failed: "
            "error -117\n"
            "[    3.913345] XFS (sda4): log mount failed\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_xfs_log_failure' in names

    def test_xfs_corruption_detected_unmount_wording(self):
        """Modern combined-line XFS wording joins filesystem_corruption."""
        serial = (
            "[    3.912345] XFS (sda4): Corruption detected. Unmount and "
            "run xfs_repair\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names

    def test_ext4_unsupported_features_detected(self):
        serial = (
            "[    4.412345] EXT4-fs (sdb1): couldn't mount because of "
            "unsupported optional features (4000)\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_ext4_feature_mismatch' in names

    def test_xfs_duplicate_uuid_detected_with_rescue_guidance(self):
        """The self-inflicted rescue-mode failure: original XFS disk
        attached as secondary shares the rescue root's UUID. Fixes must
        point at -o nouuid / xfs_admin -U generate."""
        serial = (
            "[  212.412345] XFS (sdb2): Filesystem has duplicate UUID "
            "290cb251-77f1-46f5-9fc1-4bb2c767b0de - can't mount\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_xfs_duplicate_uuid' in names
        finding = next(e for e in data['boot_errors']
                       if e['name'] == 'filesystem_xfs_duplicate_uuid')
        fixes = ' '.join(finding['suggested_fixes'])
        assert 'nouuid' in fixes
        assert 'xfs_admin -U generate' in fixes

    def test_xfs_duplicate_uuid_survives_completed_boot(self):
        """filesystem declares survives_boot_success -- the rescue VM boots
        fine while the secondary mount keeps failing, so the finding must
        not be cleared by the boot-success marker."""
        serial = (
            "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 8.102s (userspace) = 12.306s.\n"
            "[  212.412345] XFS (sdb2): Filesystem has duplicate UUID "
            "290cb251-77f1-46f5-9fc1-4bb2c767b0de - can't mount\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_xfs_duplicate_uuid' in names


# ---------------------------------------------------------------------------
# TestFstabDistroBroadening (Wave 2: \x2d units + maintenance prompt)
# ---------------------------------------------------------------------------

class TestFstabDistroBroadening:
    """Wave 2 fstab.yaml additions: escaped device units, sulogin prompt."""

    def test_escaped_uuid_timeout_matches_full_unit_name(self):
        """RHEL/SLES escaped unit form: the explicit \\x2d regex must win
        over the loose dev-\\w+ fallback so the FULL unit name (with the
        UUID) lands in detected_pattern for identifier extraction."""
        serial = (
            "systemd[1]: Timed out waiting for device dev-disk-by\\x2duuid-"
            "6c78e5d3\\x2d3672\\x2d4c05\\x2d8f65\\x2dfe4b9c1233a7.device.\n"
            "[DEPEND] Dependency failed for /data.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_device_timeout' in names
        finding = next(e for e in data['boot_errors']
                       if e['name'] == 'fstab_device_timeout')
        assert 'by\\x2duuid' in finding['detected_pattern']
        assert '.device' in finding['detected_pattern']

    def test_escaped_uuid_identifier_extracted(self):
        """End-to-end: the formatter must decode the \\x2d escapes and
        surface the real UUID as the finding identifier."""
        from gce_rescue_v2.utils.report_formatter import _extract_identifier
        serial = (
            "systemd[1]: Timed out waiting for device dev-disk-by\\x2duuid-"
            "6c78e5d3\\x2d3672\\x2d4c05\\x2d8f65\\x2dfe4b9c1233a7.device.\n"
        )
        data = _diagnose(serial)
        finding = next(e for e in data['boot_errors']
                       if e['name'] == 'fstab_device_timeout')
        identifier = _extract_identifier(finding['detected_pattern'])
        assert identifier == 'UUID=6c78e5d3-3672-4c05-8f65-fe4b9c1233a7'

    def test_give_root_password_maintenance_prompt_detected(self):
        """RHEL/SLES sulogin wording joins fstab_emergency_mode, and must
        be detectable when the buffer holds nothing but the prompt."""
        serial = (
            "Give root password for maintenance\n"
            "(or press Control-D to continue): \n"
            "padding line so the buffer exceeds the minimum length .....\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_emergency_mode' in names

    def test_live_rocky9_dracut_capture_detected_without_crypt_fp(self):
        """LIVE (w2-rocky, Rocky 9.6, broken root=UUID): exact serial
        wording differs from the canonical form ('dracut-initqueue:
        timeout, still waiting for following initqueue hooks:'), and the
        dumped devexists hook SCRIPT TEXT contains
        'systemd-cryptsetup@*.service' - which must NOT fire the crypt
        category (its patterns are bound to failure/prompt wording)."""
        serial = (
            "[  198.255076] dracut-initqueue[378]: Warning: "
            "dracut-initqueue: timeout, still waiting for following "
            "initqueue hooks:\n"
            "[  198.269189] dracut-initqueue[378]: Warning: /lib/dracut/"
            "hooks/initqueue/finished/devexists-\x2fdev\x2fdisk\x2fby-"
            "uuid\x2f00000000-0000-0000-0000-000000000bad.sh: \"if ! "
            "grep -q After=remote-fs-pre.target /run/systemd/generator/"
            "systemd-cryptsetup@*.service 2>/dev/null; then\n"
            "[  198.296147] dracut-initqueue[378]:     [ -e \"/dev/disk/"
            "by-uuid/00000000-0000-0000-0000-000000000bad\" ]\n"
            "[  198.839486] dracut-initqueue[378]: fi\"\n"
            "[  198.839530] dracut-initqueue[378]: Warning: "
            "dracut-initqueue: starting timeout scripts\n"
            "[  198.839573] dracut-initqueue[378]: Warning: Could not "
            "boot.\n"
            "Warning: /dev/disk/by-uuid/00000000-0000-0000-0000-"
            "000000000bad does not exist\n"
            "\n"
            "Generating \"/run/initramfs/rdsosreport.txt\"\n"
            "\n"
            "Entering emergency mode. Exit the shell to continue.\n"
            "Type \"journalctl\" to view system logs.\n"
            "dracut:/# \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_dracut_timeout' in names
        assert 'initramfs_dracut_emergency' in names
        assert 'fstab_emergency_mode' not in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'crypt' not in categories
        assert 'kernel' not in categories

    def test_live_opensuse_fstab_timeout_console_truncated_detected(self):
        """LIVE (w2-suse, openSUSE Leap 16, bogus fstab UUID): the console
        renders the device path ellipsized ('/dev/...000000bad'), and the
        engine must still report the device timeout + UUID root cause."""
        serial = (
            "[ TIME ] Timed out waiting for device "
            "/dev/…000000-0000-0000-0000-000000000bad.\n"
            "[DEPEND] Dependency failed for File System "
            "…000000-0000-0000-0000-000000000bad.\n"
            "[DEPEND] Dependency failed for /data2.\n"
            "[DEPEND] Dependency failed for Local File Systems.\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_device_timeout' in names
        assert 'fstab_emergency_mode' not in names

    def test_live_opensuse_plain_failed_to_mount_detected(self):
        """LIVE (t-w2-suse, openSUSE Leap 16, wiped-superblock secondary
        disk): the console prints the PLAIN form '[FAILED] Failed to mount
        /mnt/data.' - no .mount suffix, no systemd[1]: prefix - and the
        only DEPEND lines name non-path targets ('Local File Systems'),
        so before the plain-form regex this buffer produced nothing but
        the emergency-mode catch-all."""
        serial = (
            "[  OK  ] Mounted /var.\n"
            "[FAILED] Failed to mount /mnt/data.\n"
            "See 'systemctl status mnt-data.mount' for details.\n"
            "[DEPEND] Dependency failed for Local File Systems.\n"
            "[DEPEND] Dependency failed for Early Kernel Boot Messages.\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_mount_failed' in names
        # Root cause wins; the generic emergency catch-all stays demoted.
        assert 'fstab_emergency_mode' not in names


# ---------------------------------------------------------------------------
# TestDistroBroadeningRedTeamRegressions
# ---------------------------------------------------------------------------

class TestDistroBroadeningRedTeamRegressions:
    """Regressions from the adversarial review of the distro-broadening
    branch (dracut/BusyBox/LVM/LUKS/RAID). Each test reproduces a confirmed
    failing serial buffer from the red-team report."""

    # C1: an S2-shaped dracut failure ALWAYS prints 'Entering emergency
    # mode.' — once the incident is resolved and the VM boots, the stale
    # emergency line must not veto boot-success suppression forever.
    _RESOLVED_DRACUT_EMERGENCY = (
        "[  135.209316] dracut-initqueue[550]: Warning: dracut-initqueue "
        "timeout - starting timeout scripts\n"
        "[  191.964618] dracut-initqueue[550]: Warning: Could not boot.\n"
        "[  191.976595] dracut-initqueue[550]: Warning: /dev/disk/by-uuid/"
        "00000000-0000-0000-0000-000000000bad does not exist\n"
        "Generating \"/run/initramfs/rdsosreport.txt\"\n"
        "Entering emergency mode. Exit the shell to continue.\n"
        "dracut:/# \n"
        "-- reboot --\n"
        "[    2.412345] XFS (sda4): Ending clean mount\n"
        "[    5.204333] systemd[1]: Startup finished in 4.204s (kernel) "
        "+ 8.102s (userspace) = 12.306s.\n"
        "rocky9 login: \n"
    )

    def test_resolved_dracut_emergency_reports_healthy(self):
        """C1: a resolved dracut emergency incident followed by a clean
        boot on a RUNNING VM must report healthy — the stale 'Entering
        emergency mode' line must not permanently block suppression."""
        data = _diagnose(self._RESOLVED_DRACUT_EMERGENCY)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_resolved_fstab_emergency_reports_healthy(self):
        """C1: same veto bug via the fstab wording ('You are in emergency
        mode') — resolved incident + clean boot must report healthy."""
        serial = (
            "systemd[1]: Timed out waiting for device /dev/disk/by-uuid/"
            "deadbeef-1234-5678-9abc-def012345678\n"
            "systemd[1]: Dependency failed for /data.\n"
            "You are in emergency mode. After logging in, type "
            '"journalctl -xb" to view\n'
            "-- reboot --\n"
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_unresolved_dracut_emergency_still_reported(self):
        """C1 guard: when the emergency shell is the LATEST evidence (stale
        success marker from an older boot first), the findings must be
        retained — suppression stays position-based, not presence-based."""
        serial = (
            "systemd[1]: Startup finished in 4.2s (kernel) + 8.1s "
            "(userspace) = 12.3s.\n"
            "-- reboot --\n"
            "[  135.209316] dracut-initqueue[550]: Warning: dracut-initqueue "
            "timeout - starting timeout scripts\n"
            "Entering emergency mode. Exit the shell to continue.\n"
            "dracut:/# \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_dracut_timeout' in names
        assert 'initramfs_dracut_emergency' in names

    def test_mdadm_failed_to_run_array_detected(self):
        """C2: the actual mdadm wording for a refused array start
        (Assemble.c: 'failed to RUN_ARRAY') must fire raid_start_failed."""
        serial = (
            "mdadm: failed to RUN_ARRAY /dev/md0: Input/output error\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_start_failed' in names

    def test_mdadm_not_enough_to_start_assemble_wording_detected(self):
        """C2: degraded raid1 refusal via mdadm --assemble ('- not enough
        to start the array.') must fire raid_start_failed."""
        serial = (
            "mdadm: /dev/md0 assembled from 1 drive - not enough to "
            "start the array.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_start_failed' in names

    def test_mdadm_not_enough_to_start_incremental_wording_detected(self):
        """C2: the udev incremental-assembly path modern boots use
        (Incremental.c: 'attached to ..., not enough to start (1).') must
        fire raid_start_failed."""
        serial = (
            "mdadm: /dev/sdb1 attached to /dev/md/0, not enough to "
            "start (1).\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'raid_start_failed' in names

    def test_completed_cryptswap_job_on_stopped_vm_not_flagged(self):
        """C3: a transient cryptsetup start-job spinner that COMPLETES must
        not fire crypt_unlock_wait on a TERMINATED VM (boot-success
        suppression is skipped there — the regex itself must reject it)."""
        serial = (
            "[ ***  ] A start job is running for Cryptography Setup for "
            "cryptswap1 (11s / no limit)\n"
            "[  OK  ] Started Cryptography Setup for cryptswap1.\n"
            "[    9.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 5.102s (userspace) = 9.306s.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        assert result.diagnosis_status == 'healthy'
        assert result.boot_errors == []

    def test_completed_crypt_device_unit_job_on_stopped_vm_not_flagged(self):
        """C3: Ubuntu device-unit spinner form, resolved by 'Found device'
        — must also stay silent on a TERMINATED VM."""
        serial = (
            "[ ***  ] A start job is running for "
            "dev-mapper-cryptswap1.device (7s / 1min 30s)\n"
            "[  OK  ] Found device /dev/mapper/cryptswap1.\n"
            "[    9.204333] systemd[1]: Startup finished in 4.204s (kernel) "
            "+ 5.102s (userspace) = 9.306s.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        assert result.diagnosis_status == 'healthy'
        assert result.boot_errors == []

    def test_hung_crypt_job_still_detected_on_stopped_vm(self):
        """C3 guard: a real hang repeats the spinner with no completion
        line — crypt_unlock_wait must still fire (TERMINATED)."""
        serial = (
            "[ ***  ] A start job is running for Cryptography Setup for "
            "luks-2f4c8e11 (30s / no limit)\n"
            "[***   ] A start job is running for Cryptography Setup for "
            "luks-2f4c8e11 (1min 30s / no limit)\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'crypt_unlock_wait' in names

    def test_busybox_alert_uuid_line_does_not_fire_fstab(self):
        """C4: the BusyBox 'ALERT!  UUID=... does not exist' line names the
        root= kernel parameter — initramfs_busybox_shell owns it; the
        fstab auto-repair guidance (wrong file) must NOT co-fire."""
        serial = (
            "Gave up waiting for root file system device.  Common "
            "problems:\n"
            " - Boot args (cat /proc/cmdline)\n"
            "ALERT!  UUID=11111111-2222-3333-4444-555555555555 does not "
            "exist.  Dropping to a shell!\n"
            "BusyBox v1.30.1 (Ubuntu 1:1.30.1-7ubuntu3) built-in shell "
            "(ash)\n"
            "(initramfs) \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'initramfs_busybox_shell' in names
        assert 'fstab_uuid_not_found' not in names

    def test_non_alert_uuid_not_found_still_fires_fstab(self):
        """C4 guard: the ALERT carve-out must not lose genuine fstab UUID
        failures on ordinary systemd/mount lines."""
        serial = (
            "Linux version 6.1.0-18-cloud-amd64\n"
            "mount: UUID=deadbeef-1234-5678-9abc-def012345678 does not "
            "exist\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'fstab_uuid_not_found' in names


# ---------------------------------------------------------------------------
# TestSwitchrootDetection (Wave 3 - boot stage 6)
# ---------------------------------------------------------------------------

class TestSwitchrootDetection:
    """Detection tests for switchroot.yaml (switch_root / init handover)."""

    def test_systemd_switch_root_failure_detected(self):
        """systemd's 'Failed to switch root' (not an OS tree) is detected."""
        serial = (
            "[    3.412345] systemd[1]: Starting Switch Root...\n"
            "[    3.512345] systemd[1]: Failed to switch root: Specified "
            "switch root path '/sysroot' does not seem to be an OS tree. "
            "os-release file is missing.\n"
            "[    3.612345] systemd[1]: Failed to start Switch Root.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_failed' in names

    def test_switch_root_mount_moving_detected(self):
        """util-linux switch_root 'failed to mount moving' is detected."""
        serial = (
            "[    2.812345] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode\n"
            "switch_root: failed to mount moving /dev to /sysroot/dev: "
            "Invalid argument\n"
            "switch_root: forcing unmount of /dev\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_failed' in names

    def test_initramfs_tools_run_init_failure_detected(self):
        """Debian initramfs-tools no-init sequence (run-init wording)."""
        serial = (
            "Begin: Running /scripts/init-bottom ... done.\n"
            "run-init: can't execute '/sbin/init': No such file or "
            "directory\n"
            "Target filesystem doesn't have requested /sbin/init.\n"
            "run-init: can't execute '/etc/init': No such file or directory\n"
            "No init found. Try passing init= bootarg.\n"
            "BusyBox v1.30.1 (Debian 1:1.30.1-6+b3) built-in shell (ash)\n"
            "(initramfs) \n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_no_init' in names
        # The BusyBox prompt is a real second finding (different stage
        # ownership), not an overlap bug.
        assert 'initramfs_busybox_shell' in names

    def test_run_init_path_first_form_detected(self):
        """Debian 12 klibc run-init prints the path-first form
        'run-init: /sbin/init: No such file or directory' (no
        "can't execute" wording) - exact sequence captured live on GCE
        (2026-07). Fixture omits the 'Target filesystem' line so this
        test proves the run-init regex alone carries detection."""
        serial = (
            "Begin: Running /scripts/init-bottom ... done.\n"
            "run-init: /sbin/init: No such file or directory\n"
            "run-init: /etc/init: No such file or directory\n"
            "run-init: /bin/init: No such file or directory\n"
            "/bin/sh: 0: can't access tty; job control turned off\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_no_init' in names

    def test_no_working_init_panic_fires_switchroot_not_generic(self):
        """Modern kernels panic 'No working init found.' - switchroot owns
        that panic line, and the kernel_panic_generic lookahead must
        mirror the exclusion (INVARIANT drift guard)."""
        serial = (
            "[    2.123456] Run /sbin/init as init process\n"
            "[    2.124512] Failed to execute /sbin/init (error -2)\n"
            "[    2.125624] Kernel panic - not syncing: No working init "
            "found. Try passing init= option to kernel. See Linux "
            "Documentation/admin-guide/init.rst for guidance.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'switchroot_no_init' in names
        assert 'kernel_panic_generic' not in names

    def test_old_kernel_no_init_panic_fires_switchroot_not_generic(self):
        """Old-kernel wording 'No init found. Try passing init=' - same
        exclusion-mirror guard as the modern form."""
        serial = (
            "[    1.912345] VFS: Mounted root (ext4 filesystem) readonly "
            "on device 8:1.\n"
            "[    2.025624] Kernel panic - not syncing: No init found.  "
            "Try passing init= option to kernel. See Linux "
            "Documentation/init.txt for guidance.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'switchroot_no_init' in names
        assert 'kernel_panic_generic' not in names

    def test_init_shared_library_failure_detected(self):
        """init failing on a missing shared library is a switchroot finding."""
        serial = (
            "[    2.612345] Run /sbin/init as init process\n"
            "/sbin/init: error while loading shared libraries: libc.so.6: "
            "cannot open shared object file: No such file or directory\n"
            "[    2.812345] Kernel panic - not syncing: Attempted to kill "
            "init! exitcode=0x00007f00\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'switchroot_init_libs' in names

    def test_userspace_shared_library_failure_not_flagged(self):
        """'error while loading shared libraries' from an ordinary daemon
        on a RUNNING vm must NOT fire switchroot_init_libs - unbound, the
        string is a guaranteed false positive (any broken userspace
        dependency prints it after a perfectly healthy boot)."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  OK  ] Started OpenBSD Secure Shell server.\n"
            "[   11.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 8.4s (userspace) = 11.5s.\n"
            "debian login: \n"
            "myapp[1421]: /usr/local/bin/myapp: error while loading shared "
            "libraries: libfoo.so.1: cannot open shared object file\n"
            "cloud-init[892]: /usr/bin/cloud-init: error while loading "
            "shared libraries: libpython3.11.so.1.0: cannot open shared "
            "object file\n"
            "systemd-networkd[315]: error while loading shared libraries: "
            "libsystemd-shared-252.so: cannot open shared object file\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'switchroot' not in categories

    def test_busybox_root_device_drop_does_not_fire_switchroot(self):
        """A BusyBox drop caused by a MISSING ROOT DEVICE (stage 4) has no
        init strings - it must stay an initramfs finding only."""
        serial = (
            "Gave up waiting for root file system device.  Common "
            "problems:\n"
            "ALERT!  UUID=11111111-2222-3333-4444-555555555555 does not "
            "exist.  Dropping to a shell!\n"
            "BusyBox v1.30.1 (Ubuntu 1:1.30.1-7ubuntu3) built-in shell "
            "(ash)\n"
            "(initramfs) \n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'initramfs' in categories
        assert 'switchroot' not in categories

    def test_healthy_boot_no_switchroot_findings(self):
        """A clean boot (including the healthy switch-root transition line)
        must produce zero switchroot findings."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    2.912345] systemd[1]: Starting Switch Root...\n"
            "[    3.012345] systemd[1]: Switching root.\n"
            "[    5.412345] EXT4-fs (sda1): re-mounted. Quota mode: none.\n"
            "[   10.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 7.4s (userspace) = 10.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestSystemdEarlyDetection (Wave 3 - boot stage 7)
# ---------------------------------------------------------------------------

class TestSystemdEarlyDetection:
    """Detection tests for systemd_early.yaml (PID1 / early services)."""

    def test_ordering_cycle_detected_with_unit_names(self):
        """Realistic ordering-cycle block (systemd <=v249 wording)."""
        serial = (
            "[    4.112345] systemd[1]: local-fs.target: Found ordering "
            "cycle on local-fs.target/start\n"
            "[    4.112400] systemd[1]: local-fs.target: Found dependency "
            "on mnt-data.mount/start\n"
            "[    4.112500] systemd[1]: local-fs.target: Job "
            "mnt-data.mount/start deleted to break ordering cycle starting "
            "with local-fs.target/start\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'systemd_ordering_cycle']
        assert findings, "systemd_ordering_cycle should be detected"
        assert all(f.severity == 'error' for f in findings)

    def test_ordering_cycle_modern_wording_detected(self):
        """systemd v250+ wording: 'Breaking ordering cycle by deleting job'."""
        serial = (
            "[    3.912345] systemd[1]: sysinit.target: Found ordering "
            "cycle on sysinit.target/start\n"
            "[    3.912400] systemd[1]: sysinit.target: Breaking ordering "
            "cycle by deleting job cloud-init.service/start\n"
            "[    3.912500] systemd[1]: cloud-init.service: Job "
            "cloud-init.service/start deleted to break ordering cycle\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'systemd_ordering_cycle' in names

    def test_ordering_cycle_job_deleted_wording_alone_detected(self):
        """systemd 252 journal wording captured live on GCE (2026-07):
        'Job X deleted to break ordering cycle starting with Y'. Must
        fire even if the 'Found ordering cycle on' line is lost to
        serial buffer truncation."""
        serial = (
            "[    2.661909] systemd[1]: cycleb.service: Job "
            "cyclea.service/start deleted to break ordering cycle "
            "starting with cycleb.service/start\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'systemd_ordering_cycle' in names

    def test_ordering_cycle_console_skip_line_detected(self):
        """Console status line captured live on GCE (2026-07):
        '[ SKIP ] Ordering cycle found, skipping cyclea.service' -
        printed even when journal-to-console forwarding is off, so it
        must fire on its own."""
        serial = (
            "[ SKIP ] Ordering cycle found, skipping cyclea.service\n"
            "[  OK  ] Started cron.service - Regular background program "
            "processing daemon.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'systemd_ordering_cycle' in names

    def test_ordering_cycle_suppressed_after_successful_boot(self):
        """When systemd breaks the cycle and boot completes on a RUNNING
        vm, boot-success suppression clears the finding - by design
        (severity error, no survives_boot_success: diagnose targets boot
        failures, and this boot succeeded)."""
        serial = (
            "[    4.112345] systemd[1]: local-fs.target: Found ordering "
            "cycle on local-fs.target/start\n"
            "[    4.112500] systemd[1]: local-fs.target: Job "
            "mnt-data.mount/start deleted to break ordering cycle starting "
            "with local-fs.target/start\n"
            "[   12.512345] systemd[1]: Startup finished in 4.1s (kernel) "
            "+ 8.4s (userspace) = 12.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'

    def test_pid1_freeze_detected_as_critical(self):
        """systemd[1] freezing execution is a hard boot stop."""
        serial = (
            "[    3.212345] systemd[1]: Caught <SEGV>, dumped core as pid "
            "412.\n"
            "[    3.312345] systemd[1]: Freezing execution.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'systemd_freeze']
        assert findings, "systemd_freeze should be detected"
        assert all(f.severity == 'critical' for f in findings)

    def test_root_locked_console_detected_alongside_root_cause(self):
        """The sulogin lockout line (root account locked) must be reported
        next to the fstab root cause - it explains why emergency mode is
        a dead end on cloud images."""
        serial = (
            "[  TIME ] Timed out waiting for device "
            "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678.\n"
            "[DEPEND] Dependency failed for /mnt/data.\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
            "Cannot open access to console, the root account is locked. "
            "See sulogin(8) man page for more details.\n"
            "Press Enter to continue.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'systemd_no_console' in names
        assert 'fstab_device_timeout' in names

    def test_benign_unit_failures_do_not_fire_systemd_early(self):
        """Scoping guard: ordinary '[FAILED] Failed to start ...' unit
        failures on an otherwise healthy boot must produce ZERO
        systemd_early findings (the category deliberately ships no
        generic Failed-to-start pattern)."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[FAILED] Failed to start Update APT News.\n"
            "[    8.412345] systemd[1]: apt-news.service: Main process "
            "exited, code=exited, status=1/FAILURE\n"
            "[FAILED] Failed to start Download data for packages that "
            "failed at package install time.\n"
            "[   11.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 8.4s (userspace) = 11.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestKernelEarlyFaultDetection (Wave 3 - kernel.yaml broadening)
# ---------------------------------------------------------------------------

class TestKernelEarlyFaultDetection:
    """Detection tests for kernel_attempted_kill_init, kernel_bug_oops and
    kernel_decompress_fail, including catch-all and grub overlap guards."""

    def test_attempted_kill_init_fires_specific_not_generic(self):
        """The Attempted-to-kill-init panic must report the specific
        pattern; the generic lookahead mirrors it (INVARIANT guard)."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[    5.002311] Kernel panic - not syncing: Attempted to kill "
            "init! exitcode=0x0000000b\n"
            "[    5.008442] CPU: 0 PID: 1 Comm: systemd Not tainted "
            "5.10.0-28-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_attempted_kill_init' in names
        assert 'kernel_panic_generic' not in names

    def test_run_init_failure_reports_switchroot_and_kill_init(self):
        """run-init exec failure + resulting panic: the switchroot category
        names the on-disk cause, kernel names the panic - both fire."""
        serial = (
            "run-init: can't execute '/sbin/init': No such file or "
            "directory\n"
            "[    2.512345] Kernel panic - not syncing: Attempted to kill "
            "init! exitcode=0x00000100\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'switchroot_no_init' in names
        assert 'kernel_attempted_kill_init' in names
        assert 'kernel_panic_generic' not in names

    def test_bug_unable_to_handle_and_oops_detected(self):
        """Classic NULL-dereference Oops block reports kernel_bug_oops."""
        serial = (
            "[  184.123456] BUG: unable to handle kernel NULL pointer "
            "dereference at 0000000000000000\n"
            "[  184.130211] #PF: supervisor read access in kernel mode\n"
            "[  184.136122] Oops: 0002 [#1] SMP PTI\n"
            "[  184.141233] RIP: 0010:broken_driver_fn+0x12/0x40 "
            "[broken_driver]\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'kernel_bug_oops']
        assert findings, "kernel_bug_oops should be detected"
        assert all(f.severity == 'error' for f in findings)

    def test_kernel_bug_at_file_line_detected(self):
        """'kernel BUG at <file>:<line>!' form reports kernel_bug_oops."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   92.512345] kernel BUG at fs/ext4/inode.c:1731!\n"
            "[   92.518442] invalid opcode: 0000 [#1] PREEMPT SMP NOPTI\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'kernel_bug_oops' in names

    def test_oops_in_prose_not_flagged(self):
        """Bare 'Oops'/'oops' in ordinary log prose must never match - the
        pattern requires the Oops header with a 4-digit error code."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "myapp[1421]: Oops! Upload failed, retrying in 5s\n"
            "installer: we hit an oops in the parser, continuing\n"
            "There was a kernel bug at startup last week, now fixed\n"
            "[   11.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 8.4s (userspace) = 11.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_decompressor_corruption_detected(self):
        """Corrupt vmlinuz: decompressor error + '-- System halted' stub."""
        serial = (
            "Loading Linux 6.1.0-18-cloud-amd64 ...\n"
            "Loading initial ramdisk ...\n"
            "Decompressing Linux... \n"
            "\n"
            "XZ-compressed data is corrupt\n"
            "\n"
            " -- System halted\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'kernel_decompress_fail']
        assert findings, "kernel_decompress_fail should be detected"
        assert all(f.severity == 'critical' for f in findings)

    def test_healthy_decompress_line_not_flagged(self):
        """The healthy 'Decompressing Linux... Parsing ELF... done.' line
        must never match (same-line failed/error binding)."""
        serial = (
            "Loading Linux 6.1.0-18-cloud-amd64 ...\n"
            "Decompressing Linux... Parsing ELF... Performing "
            "relocations... done.\n"
            "Booting the kernel.\n"
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   11.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 8.4s (userspace) = 11.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_reboot_system_halted_not_flagged(self):
        """The kernel's ordinary shutdown line 'reboot: System halted' has
        no '-- ' stub prefix and must NOT fire kernel_decompress_fail."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  831.512345] systemd-shutdown[1]: Syncing filesystems and "
            "block devices.\n"
            "[  832.612345] reboot: System halted\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'kernel_decompress_fail' not in names

    def test_decompress_fail_and_grub_invalid_magic_do_not_cross_fire(self):
        """Overlap guard: GRUB's 'error: invalid magic number' stays a grub
        finding; the decompressor stub stays a kernel finding."""
        grub_serial = (
            "GRUB loading.\n"
            "error: invalid magic number.\n"
            "Entering rescue mode...\n"
            "grub rescue> \n"
        )
        result = analyze_serial_output(grub_serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'grub_invalid_magic' in names
        assert 'kernel_decompress_fail' not in names

        stub_serial = (
            "Loading Linux 6.1.0-18-cloud-amd64 ...\n"
            "Decompressing Linux... \n"
            "LZ4-compressed data is corrupt\n"
            " -- System halted\n"
        )
        result = analyze_serial_output(stub_serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        names = [e.name for e in result.boot_errors]
        assert 'kernel_decompress_fail' in names
        assert 'grub' not in categories


# ---------------------------------------------------------------------------
# TestWave3RedTeamRegressions
# ---------------------------------------------------------------------------

class TestWave3RedTeamRegressions:
    """Regressions from the adversarial review of the Wave 3 branch
    (switchroot / systemd_early / kernel early faults). Each test
    reproduces a confirmed failing serial buffer from the red-team report."""

    # C1: root is locked on every GCP image, so sulogin prints the
    # locked-console line on EVERY emergency-mode entry. The error-level
    # systemd_no_console companion must never suppress the CRITICAL
    # emergency-mode catch-all via Tier-1 dedupe: only a finding that names
    # a CRITICAL root cause may replace it.
    def test_locked_console_does_not_erase_emergency_mode_running(self):
        serial = (
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
            "Cannot open access to console, the root account is locked. "
            "See sulogin(8) man page for more details.\n"
            "Press Enter to continue.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_emergency_mode' in names
        assert 'systemd_no_console' in names
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['fstab_emergency_mode'] == 'critical'

    def test_locked_console_does_not_erase_emergency_mode_terminated(self):
        serial = (
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
            "Cannot open access to console, the root account is locked. "
            "See sulogin(8) man page for more details.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'fstab_emergency_mode' in names
        assert 'systemd_no_console' in names

    def test_ordering_cycle_does_not_erase_emergency_mode(self):
        """An error-level ordering-cycle report is context, not a root
        cause: systemd_early.yaml documents that a cycle blocking boot is
        'reported by the pattern that put the VM in emergency mode' - so
        the emergency catch-all must survive alongside it."""
        serial = (
            "[    4.112345] systemd[1]: local-fs.target: Job "
            "mnt-data.mount/start deleted to break ordering cycle starting "
            "with local-fs.target/start\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_emergency_mode' in names
        assert 'systemd_ordering_cycle' in names

    def test_critical_root_cause_still_demotes_emergency_catchall(self):
        """Control (X3c): a CRITICAL fstab root cause still suppresses the
        emergency catch-all under the tightened Tier-1 gate, and the
        no-console companion keeps being reported next to it."""
        serial = (
            "[  TIME ] Timed out waiting for device "
            "/dev/disk/by-uuid/deadbeef-1234-5678-9abc-def012345678.\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
            "Cannot open access to console, the root account is locked. "
            "See sulogin(8) man page for more details.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'fstab_device_timeout' in names
        assert 'systemd_no_console' in names
        assert 'fstab_emergency_mode' not in names

    # C2: when journal-to-console forwarding is off, the '[FAILED]' unit
    # status line is the ONLY switch-root output on the serial console -
    # the journal 'Failed to switch root:' line never appears.
    def test_rhel9_switch_root_unit_failed_line_detected(self):
        serial = (
            "[FAILED] Failed to start initrd-switch-root.service - "
            "Switch Root.\n"
            "See 'systemctl status initrd-switch-root.service' for "
            "details.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_failed' in names

    def test_rhel8_switch_root_unit_failed_line_detected(self):
        serial = (
            "[  OK  ] Reached target Initrd File Systems.\n"
            "         Starting Switch Root...\n"
            "[FAILED] Failed to start Switch Root.\n"
            "See 'systemctl status initrd-switch-root.service' for "
            "details.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'switchroot_failed' in names

    # C3: arm64 (T2A) fault headers differ from x86 - 'Internal error:
    # Oops: <code>' and an unprefixed 'Unable to handle kernel ...' line.
    # Modern x86 (>= 5.1) reports 'BUG: unable to handle page fault for
    # address:'. All three were complete misses.
    def test_arm64_oops_detected(self):
        serial = (
            "[  184.123456] Unable to handle kernel NULL pointer "
            "dereference at virtual address 0000000000000010\n"
            "[  184.130211] Mem abort info:\n"
            "[  184.136122] Internal error: Oops: 96000004 [#1] PREEMPT "
            "SMP\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'kernel_bug_oops' in names

    def test_arm64_paging_request_header_alone_detected(self):
        serial = (
            "[   92.512345] Unable to handle kernel paging request at "
            "virtual address ffff8000122d4000\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'kernel_bug_oops' in names

    def test_modern_x86_page_fault_header_alone_detected(self):
        """The >= 5.1 wording dropped 'kernel' from the BUG header; must be
        caught even when the Oops line is truncated off the buffer."""
        serial = (
            "[  184.123456] BUG: unable to handle page fault for address: "
            "ffffffffc0a01000\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'kernel_bug_oops' in names

    def test_warn_on_trace_still_not_flagged_by_new_oops_regexes(self):
        """A WARN_ON trace and userspace 'Unable to handle' prose must not
        match the broadened kernel_bug_oops regexes."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   42.123456] WARNING: CPU: 0 PID: 42 at "
            "kernel/sched/core.c:9999 finish_task_switch+0x1a/0x2b0\n"
            "[   42.130211] Call Trace:\n"
            "myapp[1421]: Unable to handle request, retrying\n"
            "[   11.512345] systemd[1]: Startup finished in 3.1s (kernel) "
            "+ 8.4s (userspace) = 11.5s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestReadonlyDetection (Wave 4 - runtime read-only remount / disk I/O)
# ---------------------------------------------------------------------------

class TestReadonlyDetection:
    """Detection tests for readonly.yaml (errors=remount-ro, I/O errors)."""

    def test_ext4_remount_readonly_detected(self):
        """The device-bound errors=remount-ro line is the critical signal."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[ 8123.412345] EXT4-fs error (device sda1): "
            "ext4_journal_check_start:83: comm rsyslogd: Detected aborted "
            "journal\n"
            "[ 8123.512345] EXT4-fs (sda1): Remounting filesystem read-only\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'readonly_remount' in names
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities['readonly_remount'] == 'critical'

    def test_bare_remount_readonly_detected(self):
        """ext2/jbd2 print the remount line without the EXT4-fs prefix."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[ 4451.209871] Aborting journal on device sdb1-8.\n"
            "[ 4451.312345] Remounting filesystem read-only\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'readonly_remount' in names

    def test_block_io_errors_detected_as_error_severity(self):
        """blk_update_request and the 5.18+ prefixless form both match;
        severity stays error (degradation signal, not proof of fs death)."""
        serial_old = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[ 7211.101234] blk_update_request: I/O error, dev sdb, "
            "sector 409600 op 0x0:(READ) flags 0x80700 phys_seg 1 prio "
            "class 0\n"
        )
        serial_new = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[ 7211.101234] I/O error, dev sda, sector 2048 op 0x1:(WRITE) "
            "flags 0x800 phys_seg 8 prio class 2\n"
            "[ 7211.202345] Buffer I/O error on dev sda1, logical block 0, "
            "lost async page write\n"
        )
        for serial in (serial_old, serial_new):
            data = _diagnose(serial)
            names = [e['name'] for e in data['boot_errors']]
            assert 'readonly_io_error' in names
            severities = {e['name']: e['severity']
                          for e in data['boot_errors']}
            assert severities['readonly_io_error'] == 'error'

    def test_readonly_and_filesystem_both_fire_on_same_incident(self):
        """Overlap guard: the 'EXT4-fs error (device ...)' line belongs to
        filesystem_corruption and the remount-ro line to readonly_remount.
        Both categories firing on the same buffer is expected and correct
        (different, both-true findings); neither may dedupe the other."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[ 8123.412345] EXT4-fs error (device sda1): "
            "ext4_find_entry:1683: inode #2: comm cron: reading directory "
            "lblock 0\n"
            "[ 8123.512345] EXT4-fs (sda1): Remounting filesystem read-only\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'filesystem' in categories
        assert 'readonly' in categories
        names = [e['name'] for e in data['boot_errors']]
        assert 'filesystem_corruption' in names
        assert 'readonly_remount' in names

    def test_readonly_survives_boot_success(self):
        """A remount-ro BEFORE the boot-success marker must still be
        reported on a RUNNING VM - the VM keeps 'running' while every
        write fails, which is the entire point of this category."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    8.412345] EXT4-fs (sda1): Remounting filesystem read-only\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'readonly_remount' in names

    def test_loop_device_io_errors_not_flagged(self):
        """Red-team C1: loop-device I/O errors are routine snapd
        refresh/remove noise on every Ubuntu GCE image - with
        survives_boot_success they stuck an ERROR to healthy VMs until
        the buffer rotated. The device lookahead must exclude them."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "ubuntu login: \n"
            "[ 8123.101234] blk_update_request: I/O error, dev loop3, "
            "sector 0\n"
            "[ 8123.202345] Buffer I/O error on dev loop3, logical "
            "block 0, async page read\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_removable_and_ram_device_io_errors_not_flagged(self):
        """Red-team C1: phantom CD-ROM (sr0), floppy (fd0 - the most
        famous benign I/O-error line in VM history) and ram-disk probes
        must not fire readonly_io_error."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "[ 1201.101234] I/O error, dev sr0, sector 0 op 0x0:(READ) "
            "flags 0x80700 phys_seg 1 prio class 0\n"
            "[ 1202.202345] end_request: I/O error, dev fd0, sector 0\n"
            "[ 1203.303456] Buffer I/O error on dev ram0, logical block 0\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_real_disk_io_errors_still_flagged_after_device_binding(self):
        """The lookahead must not eat real disks: sda (end_request form)
        and dm-0 (device-mapper/LVM) still fire readonly_io_error."""
        serial_endreq = (
            "[    0.000000] Linux version 3.10.0-1160.el7.x86_64\n"
            "[ 7211.101234] end_request: I/O error, dev sda, sector "
            "409600\n"
        )
        serial_dm = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[ 7211.101234] I/O error, dev dm-0, sector 2048 op "
            "0x1:(WRITE) flags 0x800 phys_seg 8 prio class 2\n"
        )
        for serial in (serial_endreq, serial_dm):
            data = _diagnose(serial)
            names = [e['name'] for e in data['boot_errors']]
            assert 'readonly_io_error' in names

    def test_healthy_mount_lines_not_flagged(self):
        """Healthy 'mounted filesystem' / 're-mounted' kernel lines must
        not fire any readonly pattern."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[    2.104233] EXT4-fs (sda1): mounted filesystem with ordered "
            "data mode. Quota mode: none.\n"
            "[    5.412345] EXT4-fs (sda1): re-mounted. Quota mode: none.\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestOomDetection (Wave 4 - live OOM kills, mode 55)
# ---------------------------------------------------------------------------

class TestOomDetection:
    """Detection tests for oom.yaml (runtime OOM-killer events).

    DELIBERATE CONTRACT CHANGE: before Wave 4 these lines matched nothing
    (they only appeared as inert context in kernel_panic_oom fixtures and
    as cross-fire negatives for grub/disk_full). They now produce a
    warning-severity oom finding by design.
    """

    def test_historical_oom_kill_on_healthy_vm_is_warning_only(self):
        """One historical OOM kill on a long-running, otherwise healthy VM:
        the warning is shown (survives_boot_success), and it is the ONLY
        finding - it must not drag in kernel/disk_full/grub."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "debian login: \n"
            "[86412.103311] java invoked oom-killer: gfp_mask=0x140dca"
            "(GFP_HIGHUSER_MOVABLE|__GFP_COMP|__GFP_ZERO), order=0, "
            "oom_score_adj=0\n"
            "[86412.209972] Out of memory: Killed process 1234 (java) "
            "total-vm:8388608kB, anon-rss:3145728kB, file-rss:0kB\n"
            "[86412.312345] oom_reaper: reaped process 1234 (java)\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        assert len(data['boot_errors']) == 1
        err = data['boot_errors'][0]
        assert err['name'] == 'oom_killed_process'
        assert err['severity'] == 'warning'
        assert err['category'] == 'oom'

    def test_invoked_oom_killer_line_alone_detected(self):
        """The invocation header alone (kill line lost to buffer rotation)
        must still produce the single oom finding."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  512.103311] chrome invoked oom-killer: gfp_mask=0x140cca, "
            "order=0, oom_score_adj=300\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert names.count('oom_killed_process') == 1

    def test_panic_on_oom_reports_both_oom_and_kernel_panic(self):
        """A panic_on_oom buffer reports BOTH the kill (oom, warning) and
        the panic (kernel_panic_oom, critical) - and the warning does not
        suppress or demote the critical (tier-1 suppressors require
        CRITICAL severity; oom is warning + detect-only)."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  512.103311] Out of memory: Killed process 1234 (java) "
            "total-vm:8388608kB\n"
            "[  512.209972] Kernel panic - not syncing: Out of memory: "
            "system-wide panic_on_oom is enabled\n"
        )
        data = _diagnose(serial)
        severities = {e['name']: e['severity'] for e in data['boot_errors']}
        assert severities.get('oom_killed_process') == 'warning'
        assert severities.get('kernel_panic_oom') == 'critical'
        assert 'kernel_panic_generic' not in severities

    def test_panic_line_alone_does_not_fire_oom(self):
        """The panic wording ('Out of memory: system-wide panic_on_oom is
        enabled') carries no kill PID - the oom regexes must not match it,
        proving they are bound to OOM-killer kill wording."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  600.001122] Kernel panic - not syncing: Out of memory: "
            "system-wide panic_on_oom is enabled\n"
        )
        data = _diagnose(serial)
        categories = {e['category'] for e in data['boot_errors']}
        assert 'kernel' in categories
        assert 'oom' not in categories

    def test_oom_warning_never_suppresses_emergency_mode(self):
        """Tier-1 guard: emergency mode (catch-all, critical) may only be
        replaced by a CRITICAL root cause - an oom warning must leave it
        untouched, and both findings are reported."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  312.103311] Out of memory: Killed process 812 (mkfs.ext4) "
            "total-vm:524288kB\n"
            "You are in emergency mode. After logging in, type "
            "\"journalctl -xb\" to view system logs.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'oom_killed_process' in names
        assert 'fstab_emergency_mode' in names

    def test_memcg_kill_matches_with_container_guidance(self):
        """Red-team C5 (decision lock): cgroup/container kills
        DELIBERATELY still match (the 'invoked oom-killer' header is
        byte-identical for global and memcg kills, and a memcg kill can
        be the real problem) - but the fixes must carry the
        container-vs-VM distinction so 'resize the VM' is not the only
        advice a GKE/Docker host gets."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[ 8123.101234] oom-kill:constraint=CONSTRAINT_MEMCG,"
            "nodemask=(null),cpuset=cri-containerd-abc,mems_allowed=0\n"
            "[ 8123.202345] Memory cgroup out of memory: Killed process "
            "2201 (stress) total-vm:1048576kB\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'oom_killed_process']
        assert len(findings) == 1
        assert findings[0].severity == 'warning'
        assert any('cgroup' in f.lower() for f in
                   findings[0].suggested_fixes)

    def test_prose_oom_mentions_not_flagged(self):
        """Prose quoting 'Out of memory' / 'oom-killer' without kernel kill
        wording (PID, gfp_mask) must not match."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "startup-script[900]: INFO: page oncall if app logs show "
            "\"Out of memory\" or the oom-killer was invoked\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestSelinuxDetection (Wave 4 - mode 51, RHEL family)
# ---------------------------------------------------------------------------

class TestSelinuxDetection:
    """Detection tests for selinux.yaml."""

    def test_policy_load_failure_freezing_detected(self):
        """The classic one-line RHEL unbootable: 'Failed to load SELinux
        policy, freezing.' - selinux fires; systemd_freeze must NOT
        (its anchor is the distinct 'Freezing execution' wording)."""
        serial = (
            "[    1.912345] systemd[1]: Successfully made /usr/ read-only.\n"
            "[    2.412345] systemd[1]: Failed to load SELinux policy, "
            "freezing.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'selinux_policy_load_failure' in names
        assert 'systemd_freeze' not in names
        severities = {e.name: e.severity for e in result.boot_errors}
        assert severities['selinux_policy_load_failure'] == 'critical'

    def test_policy_load_failure_two_line_form_reports_both(self):
        """Modern systemd splits cause and terminal action across lines:
        selinux reports the cause, systemd_freeze the freeze - both are
        true and both must surface."""
        serial = (
            "[    2.412345] systemd[1]: Failed to load SELinux policy.\n"
            "[    2.412500] systemd[1]: Freezing execution.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'selinux_policy_load_failure' in names
        assert 'systemd_freeze' in names

    def test_kernel_policy_read_failure_detected(self):
        """Kernel-side wording with its double space after 'SELinux:'."""
        serial = (
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64\n"
            "[    1.512345] SELinux:  policy read failure\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'selinux_policy_load_failure' in names

    def test_relabel_banner_is_warning(self):
        """The autorelabel banner is the start of a normal self-resolving
        relabel run - warning, not critical."""
        serial = (
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64\n"
            "*** Warning -- SELinux targeted policy relabel is required. "
            "***\n"
            "*** Relabeling could take a very long time, depending on file "
            "***\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        findings = [e for e in result.boot_errors
                    if e.name == 'selinux_relabel_required']
        assert findings
        assert all(f.severity == 'warning' for f in findings)

    def test_avc_denials_and_healthy_selinux_lines_not_flagged(self):
        """Routine AVC denials and the healthy policy-load lines on an
        enforcing RHEL boot must not produce selinux findings."""
        serial = (
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64\n"
            "[    1.512345] SELinux:  Initializing.\n"
            "[    3.412345] systemd[1]: Successfully loaded SELinux policy "
            "in 98.234ms.\n"
            "[    9.812345] audit: type=1400 audit(1719900000.123:4): avc:  "
            "denied  { read } for  pid=812 comm=\"httpd\" name=\"data\" "
            "dev=\"sda1\" ino=1234\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_stale_selinux_failure_cleared_by_later_successful_boot(self):
        """No survives_boot_success (deliberate): a later completed boot
        proves the policy loaded, so the old failure is stale noise that
        suppression must clear on a RUNNING VM."""
        serial = (
            "[    2.412345] systemd[1]: Failed to load SELinux policy, "
            "freezing.\n"
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64 (second "
            "boot)\n"
            "[    3.412345] systemd[1]: Successfully loaded SELinux policy "
            "in 98.234ms.\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []


# ---------------------------------------------------------------------------
# TestStartupScriptDetection (Wave 5 - GCE startup-script failures)
# ---------------------------------------------------------------------------

class TestStartupScriptDetection:
    """Detection tests for startup_script.yaml."""

    def test_nonzero_exit_status_detected_as_warning(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[  OK  ] Started google-startup-scripts.service - Google "
            "Compute Engine Startup Scripts.\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "google_metadata_script_runner[712]: startup-script exit "
            "status 1\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        err = data['boot_errors'][0]
        assert err['name'] == 'startup_script_failed'
        assert err['severity'] == 'warning'
        assert err['category'] == 'startup_script'

    def test_exit_status_zero_never_matches(self):
        """Overlap guard: the healthy 'startup-script exit status 0' line
        prints on every successful boot and must produce zero findings."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "google_metadata_script_runner[712]: startup-script exit "
            "status 0\n"
            "google_metadata_script_runner[712]: Finished running startup "
            "scripts.\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_exit_status_zero_alongside_real_failure_stays_silent(self):
        """A successful startup script next to an unrelated boot failure
        must not add a startup_script finding."""
        serial = (
            "Linux version 5.15.0\n"
            "Timed out waiting for device /dev/disk/by-uuid/"
            "deadbeef-1234-5678-9abc-def012345678\n"
            "google_metadata_script_runner[712]: startup-script exit "
            "status 0\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'fstab' in categories
        assert 'startup_script' not in categories

    def test_multidigit_exit_status_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "google_metadata_script_runner[712]: startup-script exit "
            "status 127\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'startup_script_failed' in names

    def test_startup_script_url_failure_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "google_metadata_script_runner[712]: startup-script-url exit "
            "status 1\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'startup_script_failed' in names

    def test_guest_agent_script_failed_wording_detected(self):
        """Guest-agent variant: Script "startup-script" failed with error.
        (No contiguous 'exit status' phrase - covered by the second
        regex.)"""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "GCEMetadataScripts: Script \"startup-script\" failed with "
            "error: exit status 127\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'startup_script_failed' in names

    def test_windows_startup_script_failures_detected(self):
        """Red-team C3: Windows startup-script failures on COM1 were
        completely undetected - both the exit-status form and the
        guest-agent 'Script ... failed' form, across ps1/cmd and
        sysprep-specialize variants, must now fire."""
        lines = (
            'GCEMetadataScripts: windows-startup-script-ps1 exit status 1',
            'GCEMetadataScripts: Script "windows-startup-script-ps1" '
            'failed with error: exit status 1',
            'GCEMetadataScripts: windows-startup-script-cmd exit status 1',
            'GCEMetadataScripts: sysprep-specialize-script-ps1 exit '
            'status 1',
        )
        for line in lines:
            serial = "Windows Boot Manager\n" + line + "\n"
            result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                           'TERMINATED')
            names = [e.name for e in result.boot_errors]
            assert 'startup_script_failed' in names, line
            severities = {e.name: e.severity for e in result.boot_errors}
            assert severities['startup_script_failed'] == 'warning'

    def test_windows_exit_status_zero_never_matches(self):
        """The zero-unmatchable guard must survive the Windows
        broadening: 'windows-startup-script-ps1 exit status 0' prints on
        every healthy Windows boot."""
        serial = (
            "Windows Boot Manager\n"
            "GCEMetadataScripts: windows-startup-script-ps1 exit "
            "status 0\n"
            "GCEMetadataScripts: Finished running startup scripts.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'startup_script' not in categories

    def test_shutdown_script_failure_still_not_matched(self):
        """Overlap guard kept from round 1 (SS-5): shutdown-script exit
        codes are not startup failures and must stay unmatched by the
        broadened regexes."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "google_metadata_script_runner[912]: shutdown-script exit "
            "status 1\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'startup_script' not in categories


# ---------------------------------------------------------------------------
# TestCloudInitDetection (Wave 5 - cloud-init failures)
# ---------------------------------------------------------------------------

class TestCloudInitDetection:
    """Detection tests for cloud_init.yaml."""

    def test_module_traceback_detected(self):
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[   10.312478] cloud-init[981]: Traceback (most recent call "
            "last):\n"
            "[   10.312600] cloud-init[981]:   File \"/usr/lib/python3/"
            "dist-packages/cloudinit/cmd/main.py\", line 761, in "
            "status_wrapper\n"
            "[   10.312700] cloud-init[981]: ValueError: bad user-data\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'cloud_init_stage_failure' in names

    def test_failed_to_run_module_detected(self):
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[   11.412478] cloud-init[981]: 2026-07-04 10:00:00,123 - "
            "util.py[WARNING]: Failed to run module scripts-user\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'cloud_init_stage_failure' in names

    def test_cloud_init_unit_failure_detected(self):
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start Initial cloud-init job (metadata "
            "service crawler).\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'cloud_init_unit_failed' in names

    def test_cloud_config_unit_failure_detected(self):
        """cloud-config.service's description carries no 'cloud-init'
        token - covered by the dedicated cloud-(config|final) regex."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start Apply the settings specified in "
            "cloud-config.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'cloud_init_unit_failed' in names

    def test_healthy_cloud_init_lines_not_flagged(self):
        """Normal cloud-init stage banners and the success 'finished'
        line must produce zero findings."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[   10.312478] cloud-init[498]: Cloud-init v. 24.1.3 running "
            "'modules:final' at Fri, 04 Jul 2026 10:00:00 +0000.\n"
            "[   11.812345] cloud-init[498]: Cloud-init v. 24.1.3 finished "
            "at Fri, 04 Jul 2026 10:00:01 +0000. Datasource DataSourceGCE. "
            "Up 11.28 seconds\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_ordering_cycle_naming_cloud_init_does_not_fire_cloud_init(self):
        """An ordering cycle that happens to delete cloud-init's job is a
        systemd_early finding, not a cloud_init one."""
        serial = (
            "[    3.912345] systemd[1]: sysinit.target: Found ordering "
            "cycle on sysinit.target/start\n"
            "[    3.912400] systemd[1]: sysinit.target: Breaking ordering "
            "cycle by deleting job cloud-init.service/start\n"
            "[    3.912500] systemd[1]: cloud-init.service: Job "
            "cloud-init.service/start deleted to break ordering cycle\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'systemd_early' in categories
        assert 'cloud_init' not in categories

    def test_provisioning_failure_survives_boot_success(self):
        """cloud-init failures coexist with a completed boot by nature -
        a RUNNING VM with the success marker AFTER the failure must still
        report it."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[   11.412478] cloud-init[981]: 2026-07-04 10:00:00,123 - "
            "util.py[WARNING]: Failed to run module scripts-user\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'cloud_init_stage_failure' in names


# ---------------------------------------------------------------------------
# TestNetworkBootDetection (Wave 5 - network bring-up failures)
# ---------------------------------------------------------------------------

class TestNetworkBootDetection:
    """Detection tests for network.yaml."""

    def test_networkd_unit_failure_detected(self):
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start systemd-networkd.service - Network "
            "Configuration.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'network_unit_failed' in names

    def test_network_manager_unit_failure_detected(self):
        serial = (
            "[    0.000000] Linux version 5.14.0-362.el9.x86_64\n"
            "[FAILED] Failed to start Network Manager.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'network_unit_failed' in names

    def test_dhcp_no_offers_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "dhclient[612]: DHCPDISCOVER on eth0 to 255.255.255.255 port "
            "67 interval 15\n"
            "dhclient[612]: No DHCPOFFERS received.\n"
            "dhclient[612]: No working leases in persistent database - "
            "sleeping.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'network_dhcp_failure' in names

    def test_failed_to_bring_up_eth0_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "ifup: failed to bring up eth0\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'network_dhcp_failure' in names

    def test_post_boot_nic_death_reported_on_running_vm(self):
        """THE support case this category exists for: the NIC dies after a
        completed boot. The error lines sit AFTER the last success marker,
        so the ordering check protects them from suppression even though
        the category does not declare survives_boot_success."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   12.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 8.1s (userspace) = 12.3s.\n"
            "debian login: \n"
            "dhclient[9812]: DHCPDISCOVER on eth0 to 255.255.255.255 port "
            "67 interval 20\n"
            "dhclient[9812]: No DHCPOFFERS received.\n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'boot_errors_detected'
        names = [e['name'] for e in data['boot_errors']]
        assert 'network_dhcp_failure' in names

    def test_benign_early_flap_cleared_by_boot_success(self):
        """A bring-up failure that the boot recovered from (marker AFTER
        the error) is exactly the transient noise survives_boot_success:
        false exists to clear - the VM must report healthy."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "dhclient[612]: No DHCPOFFERS received.\n"
            "[FAILED] Failed to start Wait for Network to be Configured.\n"
            "dhclient[615]: DHCPACK of 10.128.0.14 from 169.254.169.254\n"
            "[   22.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 18.1s (userspace) = 22.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []

    def test_wait_online_timeout_is_warning_not_unit_failure(self):
        """Red-team C2: the wait-online console line contains BOTH the
        unit name 'systemd-networkd-wait-online' and the description
        'Wait for Network to be Configured' - on a stopped healthy VM it
        was double-matched into ERROR 'core network service failed' with
        rescue guidance. It is a benign timeout (boot continues, SSH
        works): warning-severity wait-online pattern, and
        network_unit_failed must NOT fire."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start systemd-networkd-wait-online."
            "service - Wait for Network to be Configured.\n"
            "[   42.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 38.1s (userspace) = 42.3s.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        names = [e.name for e in result.boot_errors]
        assert 'network_unit_failed' not in names
        assert 'network_wait_online_timeout' in names
        severities = {e.name: e.severity for e in result.boot_errors}
        assert severities['network_wait_online_timeout'] == 'warning'

    def test_networkd_failure_still_error_despite_wait_online_lookahead(self):
        """The (?!-wait-online) lookahead must not eat the real thing:
        a genuine systemd-networkd.service failure on a stopped VM stays
        an error-severity network_unit_failed."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start systemd-networkd.service - Network "
            "Configuration.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        severities = {e.name: e.severity for e in result.boot_errors}
        assert severities.get('network_unit_failed') == 'error'
        assert 'network_wait_online_timeout' not in severities

    def test_benign_unit_failures_do_not_fire_network(self):
        """The healthy-noise units (apt-daily, motd-news style) must not
        match the unit-bound network regexes."""
        serial = (
            "[    0.000000] Linux version 6.8.0-31-generic\n"
            "[FAILED] Failed to start Update APT News.\n"
            "[FAILED] Failed to start Download data for packages that "
            "failed at package install time.\n"
        )
        result = analyze_serial_output(serial, 'test-vm', 'zone-a',
                                       'TERMINATED')
        categories = {e.category for e in result.boot_errors}
        assert 'network' not in categories


# ---------------------------------------------------------------------------
# TestSerialGettyDetection (Wave 5 - serial console access, ssh category)
# ---------------------------------------------------------------------------

class TestSerialGettyDetection:
    """Detection tests for ssh_serial_getty_failed (lives in ssh.yaml:
    the ssh category is the operator-access-paths category, and getty
    failures are boot-completing access failures exactly like sshd's)."""

    def test_getty_failed_result_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   14.512345] systemd[1]: serial-getty@ttyS0.service: Failed "
            "with result 'exit-code'.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_serial_getty_failed' in names
        categories = {e['category'] for e in data['boot_errors']}
        assert 'ssh' in categories

    def test_getty_restart_loop_detected(self):
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   14.512345] systemd[1]: serial-getty@ttyS0.service: Start "
            "request repeated too quickly.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_serial_getty_failed' in names

    def test_failed_to_start_serial_getty_detected(self):
        """Console status-line form (unit description, no unit name)."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[FAILED] Failed to start Serial Getty on ttyS0.\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_serial_getty_failed' in names

    def test_getty_survives_boot_success(self):
        """A dead getty does not block boot (ssh category semantics): the
        finding must survive a RUNNING VM's boot-success marker."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   14.512345] systemd[1]: serial-getty@ttyS0.service: Failed "
            "with result 'exit-code'.\n"
            "[   16.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 12.1s (userspace) = 16.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'ssh_serial_getty_failed' in names

    def test_healthy_started_getty_not_flagged(self):
        """The healthy 'Started serial-getty@ttyS0.service' journal line
        and console '[  OK  ] Started Serial Getty on ttyS0.' must not
        match."""
        serial = (
            "[    0.000000] Linux version 6.1.0-18-cloud-amd64\n"
            "[   14.512345] systemd[1]: Started serial-getty@ttyS0.service "
            "- Serial Getty on ttyS0.\n"
            "[  OK  ] Started Serial Getty on ttyS0.\n"
            "[   16.512345] systemd[1]: Startup finished in 4.2s (kernel) "
            "+ 12.1s (userspace) = 16.3s.\n"
            "debian login: \n"
        )
        data = _diagnose(serial)
        assert data['diagnosis_status'] == 'healthy'
        assert data['boot_errors'] == []
