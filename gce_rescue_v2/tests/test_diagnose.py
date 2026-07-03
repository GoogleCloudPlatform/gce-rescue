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

def _diagnose(serial: str):
    """Run DiagnoseOperation against a serial excerpt, return boot_errors."""
    compute = _make_compute(serial_output=serial)
    op = DiagnoseOperation(compute, 'proj', 'zone-a', _make_logger())
    result = op.execute('test-vm')
    return result.rollback_data


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
