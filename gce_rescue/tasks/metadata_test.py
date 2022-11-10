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

"""Test code for metadata.py."""

from absl.testing import absltest
from absl import logging

from gce_rescue.rescue import Instance
from gce_rescue.tasks import metadata
from gce_rescue.test.mocks import  mock_api_object, MOCK_TEST_VM


class MetadataTest(absltest.TestCase):
  def setUp(self):
    self.vm = Instance(test_mode=True, **MOCK_TEST_VM)
    self.instance_data =  self.vm.data


  def test_set_metadata(self):
    self.vm.compute = mock_api_object(['operations'])
    result = metadata.set_metadata(self.vm)
    self.assertTrue(len(result)> 1)


  def test_restore_metadata(self):
    self.vm.ts = 1666774335
    self.vm.compute = mock_api_object([
      'compute',
      'serialconsole',
      'operations',
    ])
    result = metadata.restore_metadata_items(self.vm)
    self.assertTrue(len(result)> 1)

if __name__ == '__main__':
  absltest.main()

