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

# pylint: disable=broad-exception-raised

"""keeper that the progress of the tasks. """

import googleapiclient.discovery
from time import sleep
from typing import Dict
import logging
import json

_logger = logging.getLogger(__name__)


def wait_for_operation(
  instance_obj: googleapiclient.discovery.Resource,
  oper: Dict
) -> Dict:
  """ Creating poll to wait the operation to finish. """

  while True:
    if oper['status'] == 'DONE':
      _logger.info('done.')
      if 'error' in oper:
        raise Exception(oper['error'])
      return oper

    oper = instance_obj.compute.zoneOperations().get(
      **instance_obj.project_data,
      operation = oper['name']).execute()
    sleep(1)

def wait_for_os_boot(vm: googleapiclient.discovery.Resource) -> bool:
  """Wait guest OS to complete the boot proccess."""

  timeout = 60
  wait_time = 2
  end_string = f'END:{vm.ts}'
  _logger.info('Waiting startup-script to complete.')
  while True:
    result = vm.compute.instances().getSerialPortOutput(
      **vm.project_data,
      instance = vm.name
    ).execute()

    if end_string in json.dumps(result):
      _logger.info('startup-script has ended.')
      return True

    sleep(wait_time)
    timeout -= wait_time
    if not timeout:
      return False
