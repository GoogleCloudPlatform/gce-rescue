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

"""Test code for disks.py."""

from absl.testing import absltest
from absl import logging

from gce_rescue.rescue import Instance
from gce_rescue.tasks import disks
from gce_rescue.test.mocks import (
  mock_api_object,
  MOCK_TEST_VM,
  MOCK_TEST_DATA,
)

logging.set_verbosity('INFO')
# logging.set_verbosity('DEBUG')

class DisksTest(absltest.TestCase):
  def setUp(self):
    self.vm = Instance(test_mode=True, **MOCK_TEST_VM)
    self.instance_data =  self.vm.data

  def test_list_disk(self):
    self.vm.compute = mock_api_object(['disks'])
    disk_filter = f'labels.rescue={self.vm.ts}'
    result = disks.list_disk(self.vm,
      self.vm.project_data,
      disk_filter)
    self.assertTrue(len(result) > 0)
    self.assertEqual(result[0]['name'], MOCK_TEST_DATA['disk'])


  def test_attach_disk(self):
    self.vm.compute = mock_api_object([
      'disks',
      'disks',
      'operations',
      'operations',
    ])
    disk_filter = f'labels.rescue={self.vm.ts}'
    disk_list = disks.list_disk(self.vm,
      self.vm.project_data,
      disk_filter)
    disk_name = disk_list[0]['name']
    disks_ = self.instance_data['disks']
    disk = disks_[0]
    device_name = disk['deviceName']
    disks.attach_disk(
       self.vm,
       disk_name,
       device_name)


  def test_config_rescue_disks(self):
    self.vm.compute = mock_api_object([
      'operations',
      'operations',
      'operations',
      'operations',
      'operations',
      'disks',
    ])
    disks.config_rescue_disks(self.vm)


  def test_restore_original_disk(self):
    self.vm.compute = mock_api_object([
      'operations',
      'operations',
      'operations',
      'operations',
    ])
    disks.restore_original_disk(self.vm)


if __name__ == '__main__':
  absltest.main()
