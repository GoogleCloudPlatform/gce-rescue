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

""" List of classes and functions to be used across the code. """

import googleapiclient.discovery

from time import time, sleep
from typing import Dict
from gce_rescue.config import get_config
from absl import logging
import multiprocessing
import sys
import json

_logger = logging

class Tracker():
  """ Track tasks using multiprocessing and print progress bar. """

  def __init__(self, target):
    self.target = target
    self._pivot = multiprocessing.Value('i', 1)
    self._proc = None

  def start(self):
    self._proc = multiprocessing.Process(target=self._run)
    self._proc.start()
    print('- Configuring...')

  def advance(self, step=None):
    if not step:
      step = 1
    self._pivot.value += step

  def finish(self):
    self._pivot.value = self.target
    self._proc.join()
    sleep(0.5)
    print('- Configurations finished.')

  def _loading(self):
    chars = ['-', '|', '/', '|', '\\']
    i = 0
    while True:
      yield chars[i]
      i += 1
      if i == len(chars):
        i = 0

  def _run(self):
    self._gen = self._loading()
    while self._pivot.value < self.target:
      try:
        sleep(0.001)
        self._print()
      except Exception as exc:
        raise f'{exc}: {self._pivot.value} = {self.target}'

    self._print()
    print('\r')

  def _print(self):
    size = 60
    loading = next(self._gen)
    if self._pivot.value == self.target:
      loading = '█'
    count = self._pivot.value
    total = self.target
    x = int(size * self._pivot.value / self.target)
    progress = '█' * x
    bar = '.' * (size-x)
    print(f' \\- Progress {count}/{total} [{progress}{loading}{bar}]',
      end='\r',
      file=sys.stderr,
      flush=True)

def get_instance_info(
  compute: googleapiclient.discovery.Resource,
  name: str,
  project_data: Dict[str, str]
) -> Dict:
  """Set Dictionary with complete data from instances().get() from the instance.
  https://cloud.google.com/compute/docs/reference/rest/v1/instances/get
  Attributes:
    compute: obj, API Object
    instance: str, Instace name
    project_data: dict, Dictionary containing project and zone keys to be
      unpacked when calling the API.
  """
  return compute.instances().get(
      **project_data,
      instance = name).execute()

def generate_ts() -> int:
  """Get the current timestamp to be used as unique ID
  during this execution."""
  return int(time())

def validate_instance_mode(data: Dict) -> Dict:
  """Validate if the instance is already configured as rescue mode."""

  result = {
      'rescue-mode': False,
      'ts': generate_ts()
  }
  if 'metadata' in data and  'items' in data['metadata']:
    metadata = data['metadata']
    for item in metadata['items']:
      if item['key'] == 'rescue-mode':
        result = {
          'rescue-mode': True,
          'ts': item['value']
        }

  return result

def guess_guest(data: Dict) -> str:
  """Determined which Guest OS Family is being used and select a
  different OS for recovery disk.
     Default: projects/debian-cloud/global/images/family/debian-11"""

  guests = get_config('source_guests')
  for disk in data['disks']:
    if disk['boot']:
      if 'architecture' in disk:
        arch = disk['architecture'].lower()
      else:
        arch = 'x86_64'
      guest_default = guests[arch][0]
      guest_name = guest_default.split('/')[-1]
      for lic in disk['licenses']:
        if guest_name in lic:
          guest_default = guests[arch][1]
  return guest_default


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


def log_to_file(log_file_name: str, level: str = 'INFO') -> None:
  """ Set logfile and verbosity """

  log_level = getattr(logging, level.upper())
  logging.set_verbosity(log_level)
  logging.use_absl_handler()
  logging.get_absl_handler().use_absl_log_file(log_file_name, './')


def read_input(msg: str) -> None:
  """Read user input if --force is not provided."""
  print(msg, end='')
  input_answer = input()
  if input_answer.upper() != 'Y':
    print('Cancelled.')
    sys.exit(1)
