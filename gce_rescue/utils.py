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

""" List of classes and functions to be used across the code. """

from time import sleep
import logging
import multiprocessing
from threading import Thread
import sys
from gce_rescue.config import get_config


_logger = logging.getLogger(__name__)

class Tracker():
  """ Track tasks using multiprocessing and print progress bar. """

  def __init__(self, target):
    self.target = target
    self._pivot = multiprocessing.Value('i', 1)
    self._proc = None

  def start(self):
    self._proc = multiprocessing.Process(target=self._run)
    self._proc.start()
    print('┌── Configuring...')

  def advance(self, step=None):
    if not step:
      step = 1
    self._pivot.value += step

  def finish(self):
    self._pivot.value = self.target
    self._proc.join()
    sleep(0.5)
    print('├── Configurations finished.')

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
        raise Exception(f'{exc}: {self._pivot.value} = {self.target}') from exc

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
    print(f'│   └── Progress {count}/{total} [{progress}{loading}{bar}]',
      end='\r',
      file=sys.stderr,
      flush=True)


class ThreadHandler(Thread):
  """Handler for multithread tasks."""

  def __init__(  # pylint: disable=too-many-positional-arguments
      self,
      group=None,
      target=None,
      name=None,
      args=None,
      kwargs=None
    ):

    if not args:
      args = ()

    if not kwargs:
      kwargs = {}

    Thread.__init__(self, group, target, name, args, kwargs)
    self._result = None

  def run(self):
    if self._target is not None:
      self._result = self._target(*self._args, **self._kwargs)

  def result(self, *args):
    Thread.join(self, *args)
    return self._result


def set_logging(vm_name: str) -> None:
  """ Set logfile and verbosity. """

  level = 'DEBUG' if get_config('debug') else 'INFO'
  log_level = getattr(logging, level.upper())
  file_name = f'{vm_name}.log'
  logging.basicConfig(
    filename=file_name,
    filemode='a',
    format='%(asctime)s,%(msecs)03d %(levelname)-8s [%(filename)s:%(lineno)d]\
      %(message)s',
    datefmt='%Y-%m-%d:%H:%M:%S',
    level=log_level)


def read_input(msg: str) -> None:
  """Read user input if --force is not provided."""
  print(msg, end='')
  input_answer = input()
  input_answer = input_answer.strip()
  if input_answer.upper() != 'Y':
    print(f'got input: "{input_answer}". Aborting')
    sys.exit(1)
