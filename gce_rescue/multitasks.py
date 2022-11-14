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

""" Class to handle multithread tasks. """

from threading import Thread

class Handler(Thread):
  """Handler for multithread tasks."""

  def __init__(
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
