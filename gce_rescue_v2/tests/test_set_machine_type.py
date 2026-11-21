"""Unit tests for SetMachineTypeOperation."""

from unittest.mock import Mock

from gce_rescue_v2.operations import SetMachineTypeOperation

ZONE = 'us-central1-a'
TYPE_URL = f'https://www.googleapis.com/compute/v1/projects/test-project/zones/{ZONE}/machineTypes/'


class TestSetMachineTypeOperation:

    def setup_method(self):
        self.mock_compute = Mock()
        self.instances = self.mock_compute.instances.return_value
        self.zone_ops = self.mock_compute.zoneOperations.return_value
        self.instances.setMachineType.return_value.execute.return_value = {'name': 'op-1'}
        self.zone_ops.get.return_value.execute.return_value = {'status': 'DONE'}
        self.logger = Mock()
        self.operation = SetMachineTypeOperation(
            self.mock_compute, 'test-project', ZONE, self.logger
        )

    def _vm(self, status='TERMINATED', machine_type='e2-small'):
        self.instances.get.return_value.execute.return_value = {
            'status': status, 'machineType': TYPE_URL + machine_type,
        }

    def test_changes_machine_type(self):
        self._vm()

        result = self.operation.execute(vm_name='test-vm', machine_type='e2-medium')

        assert result.success is True
        self.instances.setMachineType.assert_called_once_with(
            project='test-project', zone=ZONE, instance='test-vm',
            body={'machineType': f'zones/{ZONE}/machineTypes/e2-medium'}
        )
        assert result.rollback_data == {
            'vm_name': 'test-vm',
            'previous_machine_type': 'e2-small',
            'new_machine_type': 'e2-medium',
        }

    def test_already_target_type_makes_no_change(self):
        self._vm(machine_type='e2-medium')

        result = self.operation.execute(vm_name='test-vm', machine_type='e2-medium')

        assert result.success is True
        self.instances.setMachineType.assert_not_called()

    def test_running_vm_is_refused(self):
        self._vm(status='RUNNING')

        result = self.operation.execute(vm_name='test-vm', machine_type='e2-medium')

        assert result.success is False
        assert 'RUNNING' in result.error
        self.instances.setMachineType.assert_not_called()

    def test_api_error_gets_machine_type_suggestion(self):
        self._vm()
        self.instances.setMachineType.return_value.execute.side_effect = Exception(
            "Quota 'CPUS' exceeded. Limit: 24.0 in region us-central1."
        )

        result = self.operation.execute(vm_name='test-vm', machine_type='e2-medium')

        assert result.success is False
        assert 'Failed to change the machine type' in result.error
        assert 'machine-types list' in result.error

    def test_failed_operation_reports_its_error(self):
        self._vm()
        self.zone_ops.get.return_value.execute.return_value = {
            'status': 'DONE',
            'error': {'errors': [{'message': 'Machine type is not supported'}]},
        }

        result = self.operation.execute(vm_name='test-vm', machine_type='e2-medium')

        assert result.success is False
        assert 'Machine type is not supported' in result.message

    def test_rollback_restores_previous_type(self):
        self._vm(machine_type='e2-medium')

        success = self.operation.rollback({
            'vm_name': 'test-vm',
            'previous_machine_type': 'e2-small',
            'new_machine_type': 'e2-medium',
        })

        assert success is True
        self.instances.setMachineType.assert_called_once_with(
            project='test-project', zone=ZONE, instance='test-vm',
            body={'machineType': f'zones/{ZONE}/machineTypes/e2-small'}
        )

    def test_rollback_not_needed_when_type_unchanged(self):
        self._vm(machine_type='e2-small')

        success = self.operation.rollback({
            'vm_name': 'test-vm', 'previous_machine_type': 'e2-small',
        })

        assert success is True
        self.instances.setMachineType.assert_not_called()

    def test_rollback_refused_on_running_vm(self):
        self._vm(status='RUNNING', machine_type='e2-medium')

        success = self.operation.rollback({
            'vm_name': 'test-vm', 'previous_machine_type': 'e2-small',
        })

        assert success is False
        self.instances.setMachineType.assert_not_called()

    def test_rollback_reports_failed_operation(self):
        self._vm(machine_type='e2-medium')
        self.zone_ops.get.return_value.execute.return_value = {
            'status': 'DONE',
            'error': {'errors': [{'message': 'Internal error'}]},
        }

        success = self.operation.rollback({
            'vm_name': 'test-vm', 'previous_machine_type': 'e2-small',
        })

        assert success is False
