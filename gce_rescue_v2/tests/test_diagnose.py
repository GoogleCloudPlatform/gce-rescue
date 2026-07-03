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
