# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

""" Standard VM operations. """

from gce_rescue.rescue import Instance
from gce_rescue.utils import wait_for_operation
from absl import logging

_logger = logging

def start_instance(vm: Instance) -> str:
  """Start instance."""

  _logger.info(f'Starting {vm.name}...')
  if vm.status == 'RUNNING':
    _logger.info(f'{vm.name} is already runnning.')
    return

  operation = vm.compute.instances().start(
    **vm.project_data,
    instance = vm.name).execute()
  result = wait_for_operation(vm, oper=operation)

  if result['status'] == 'DONE':
    vm.status = 'RUNNING'
  return vm.status


def stop_instance(vm: Instance) -> str:
  """Stop instance."""
  _logger.info(f'Stopping {vm.name}...')
  if vm.status == 'TERMINATED':
    _logger.info(f'{vm.name} is already stopped.')
    return

  operation = vm.compute.instances().stop(
    **vm.project_data,
    instance = vm.name).execute()
  result = wait_for_operation(vm, oper=operation)

  if result['status'] == 'DONE':
    vm.status = 'TERMINATED'
  return vm.status

