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

""" Different functions to modify VM custom metadata. """

from gce_rescue.config import get_config
from gce_rescue.utils import wait_for_operation, wait_for_os_boot
from typing import Dict
from absl import logging

_logger = logging

def set_metadata(vm) -> Dict:
  """Configure Instance custom metadata.
  https://cloud.google.com/compute/docs/reference/rest/v1/instances/setMetadata
    a. Set rescue-mode=<ts unique id> if disable=False
    b. Delete rescue-mode if disable=True
    c. Replace startup-script with local startup-script.sh content."""

  startup_script_file = get_config('startup-script-file')
  device_name = vm.disks['device_name']
  with open(startup_script_file, encoding='utf-8') as file:
    file_content = file.read()
    file_content = file_content.replace('GOOGLE_DISK_NAME', device_name)
    file_content = file_content.replace('GOOGLE_TS', str(vm.ts))

  metadata_body = {
    'fingerprint': vm.data['metadata']['fingerprint'],
    'items': [{
      'key': 'startup-script',
      'value': file_content
    }]
  }
  _logger.info('Setting custom metadata...')

  operation = vm.compute.instances().setMetadata(
    **vm.project_data,
    instance = vm.name,
    body = metadata_body).execute()

  result = wait_for_operation(vm, oper=operation)
  return result


def restore_metadata_items(vm, remove_rescue_mode: bool = False) -> Dict:
  """Restore original metadata.items after the instance is running again."""

  vm.refresh_fingerprint()

  if not remove_rescue_mode:
    vm.backup_items.append({ 'key': 'rescue-mode', 'value': vm.ts })
  else:
    vm.backup_items.remove({ 'key': 'rescue-mode', 'value': vm.ts })

  metadata_body = {
    'fingerprint': vm.data['metadata']['fingerprint'],
    'items': vm.backup_items
  }
  _logger.info('Restoring original metadata...')
  if not remove_rescue_mode and not wait_for_os_boot(vm):
    raise Exception('Guest OS boot timeout.')
  operation = vm.compute.instances().setMetadata(
    **vm.project_data,
    instance = vm.name,
    body = metadata_body).execute()
  result = wait_for_operation(vm, oper=operation)
  return result
