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
        """OOM panic should report kernel_panic_oom, not generic."""
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
        """A panic cause not covered by specific patterns falls to generic."""
        serial = (
            "[    0.000000] Linux version 5.10.0-28-cloud-amd64\n"
            "[    5.002311] Kernel panic - not syncing: Attempted to kill init! exitcode=0x00000009\n"
            "[    5.008442] CPU: 0 PID: 1 Comm: init Not tainted 5.10.0-28-cloud-amd64 #1\n"
        )
        data = _diagnose(serial)
        names = [e['name'] for e in data['boot_errors']]
        assert 'kernel_panic_generic' in names

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
            "init! exitcode=0x00007f00\n"
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
        confused with kernel OOM (kernel/disk_full categories)."""
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
        match grub_out_of_memory — proves the 'error:' line-start binding."""
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
