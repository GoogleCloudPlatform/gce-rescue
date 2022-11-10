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

"""Mock API for tests purposes."""

import os
import pathlib
import json
from typing import List
import googleapiclient.discovery
from googleapiclient.http import HttpMockSequence


MOCK_TEST_VM = {
  'project': 'mock_project',
  'zone': 'europe-central1-a',
  'name' : 'mock_vm'
}

MOCK_TEST_DATA = {
  'project': 'mock_project',
  'zone': 'europe-central1-a',
  'name' : 'mock_vm',
  'disk' : 'mock-vm',
  'device': 'persistent-disk-0'
}

TESTDATA_PATH='test-data'
mock_data = {
  'compute': f'{TESTDATA_PATH}/instances.json',
  'disks': f'{TESTDATA_PATH}/disks.json',
  'operations': f'{TESTDATA_PATH}/operations.json',
  'serialconsole': f'{TESTDATA_PATH}/serialconsole.json',
}


def mock_api_object(mocks_list: List[str]):
  """ Returns mock HTTP sequence Resources to be used in API tests calls """
  responses = []
  for mock in mocks_list:
    if mock not in mock_data:
      raise Exception(ValueError, mock)
    file_name = os.path.join(os.path.dirname(__file__), mock_data[mock])
    if not pathlib.Path(file_name).is_file():
      raise Exception(FileNotFoundError, file_name)
    with open(file_name, encoding='utf-8') as fd:
      data = json.load(fd)
    responses.append(
      ({'status': '200'}, json.dumps(data))
    )
  http = HttpMockSequence(responses)
  service = googleapiclient.discovery.build('compute', 'v1', http = http)
  return service
