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

""" List of ordered tasks to be executed when set/reset VM rescue mode. """

from typing import List
import logging

from gce_rescue.gce import Instance
from gce_rescue.tasks.disks import (
  take_snapshot,
  create_rescue_disk,
  restore_original_disk,
  attach_disk
)
from gce_rescue.tasks.operations import (
  start_instance,
  stop_instance
)
from gce_rescue.tasks.metadata import (
  set_metadata,
  restore_metadata_items
)
from gce_rescue.utils import Tracker
from gce_rescue.config import get_config
_logger = logging.getLogger(__name__)

def _list_tasks(vm: Instance, action: str) -> List:
  """ List tasks, by order, per operation
    operations (str):
      1. set_rescue_mode
      2. reset_rescue_mode
  """
  all_tasks = {
    'set_rescue_mode': [
      {
        'name': stop_instance,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': take_snapshot,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': create_rescue_disk,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': set_metadata,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': start_instance,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': attach_disk,
        'args': [{
          'vm': vm,
          'boot': False,
          **vm.disks
        }],
      },
      {
        'name': restore_metadata_items,
        'args': [{
          'vm': vm
        }],
      }
  ],
    'reset_rescue_mode': [
      {
        'name': stop_instance,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': restore_original_disk,
        'args': [{
          'vm': vm
        }]
      },
      {
        'name': restore_metadata_items,
        'args': [{
          'vm': vm,
          'remove_rescue_mode': True
        }]
      },
      {
        'name': start_instance,
        'args': [{
          'vm': vm
        }]
      },
    ]
  }

  if action not in all_tasks:
    _logger.info(f'Unable to find "{action}".')
    raise ValueError()
  return all_tasks[action]


def call_tasks(vm: Instance, action: str) -> None:
  """ Loop tasks dict and execute """
  tasks = _list_tasks(vm = vm, action = action)
  total_tasks = len(tasks)

  tracker = Tracker(total_tasks)
  tracker.start()

  for task in tasks:
    if task['name'].__name__ == "take_snapshot":
      if get_config('skip-snapshot'):
        _logger.info(f'Skipping snapshot backup.')
        continue
    execute = task['name']
    args = task['args'][0]

    execute(**args)
    tracker.advance(step = 1)

  tracker.finish()