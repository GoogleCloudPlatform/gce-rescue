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

""" Define the Rescuer functions. """

from dataclasses import dataclass
from typing import Dict
import logging
from gce_rescue.gce_instance import Instance
from gce_rescue.tasks.disks import (
  config_rescue_disks,
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
from utils import Tracker

_logger = logging.getLogger(__name__)


def _list_tasks(vm: Instance, action: str) -> Dict:
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
        'name': config_rescue_disks,
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
    raise Exception(ValueError)
  return all_tasks[action]


@dataclass
class Rescuer:
  """Initialize Rescuer instance."""
  rescuee: Instance
  # _rescue_source_disk: str = ''
  # _rescue_mode_status: Dict[str, Union[str, int]] = field(
  #   default_factory=lambda: ({})
  # )

  # @property
  # def rescue_disk(self) -> str:
  #   return f'linux-rescue-disk-{self.ts}'


  # @property
  # def rescue_source_disk(self) -> str:
  #   return self._rescue_source_disk


  # @rescue_source_disk.setter
  # def rescue_source_disk(self, v: str) -> None:
  #   self._rescue_source_disk = v


  def set_rescue_mode(self) -> None:
    """ execute the set rescue mode tasks """

    self._call_tasks('set_rescue_mode')


  def reset_rescue_mode(self) -> None:
    """  execute the reset rescue mode tasks """

    self._call_tasks('reset_rescue_mode')


  def _call_tasks(self, action) -> None:
    """ Loop tasks dict and execute tasks"""

    tasks = _list_tasks(vm = self.rescuee, action = action)
    total_tasks = len(tasks)

    tracker = Tracker(total_tasks)
    tracker.start()

    for task in tasks:
      execute = task['name']
      args = task['args'][0]

      execute(**args)
      tracker.advance(step = 1)

    tracker.finish()
