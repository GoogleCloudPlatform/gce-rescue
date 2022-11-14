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

"""Test code for rescue.py."""

from absl.testing import absltest
from gce_rescue.rescue import Instance
from gce_rescue.test.mocks import (
  mock_api_object,
  MOCK_TEST_VM,
  MOCK_TEST_DATA
)

class RescueTest(absltest.TestCase):
  vm: Instance


  def setUp(self):
    self.vm = Instance(test_mode=True, **MOCK_TEST_VM)
    self.instance_data =  self.vm.data


  def test_instance(self):
    """Compare the properties values with the fixed values in instances.json."""

    self.assertEqual(self.vm.name, 'mock_vm')
    self.assertEqual(
      self.vm.rescue_mode_status['rescue-mode'],
      False
    )
    self.assertEqual(
      self.vm.project_data,
      {
        'project': MOCK_TEST_DATA['project'],
        'zone': MOCK_TEST_DATA['zone']
      }
    )
    self.assertTrue(len(self.vm.backup_items) > 0)


  def test_define_disks(self):
    """Validate the vm._define_disks() by comparing the function returned
    dict to MOCK_TEST_DATA"""

    disks_test = self.vm._define_disks() # pylint: disable=protected-access

    self.assertEqual(
      disks_test,
      {
        'device_name': MOCK_TEST_DATA['device'],
        'disk_name': MOCK_TEST_DATA['disk']
      }
    )


  def test_refresh_fingerprint(self):
    """Validate the refresh fingerprint function by resetting the value,
    calling the function and comparing the hearded code in instances.json."""

    self.vm.compute = mock_api_object(['compute'])

    self.vm.data['metadata']['fingerprint'] = ''
    self.vm.refresh_fingerprint()

    self.assertEqual(
      self.vm.data['metadata']['fingerprint'],
      'cRcIE4_rlPM='
    )


if __name__ == '__main__':
  absltest.main()
