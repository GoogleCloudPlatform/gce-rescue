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

""" Test code for messages.py. """

from absl.testing import absltest
from gce_rescue import messages
from gce_rescue.rescue import Instance
from gce_rescue.test.mocks import mock_api_object, MOCK_TEST_VM


class MessagesTest(absltest.TestCase):
  vm: Instance


  def setUp(self):
    self.vm = Instance(test_mode=True, **MOCK_TEST_VM)
    self.vm.compute = mock_api_object(['compute'])
    self.instance_data =  self.vm.data


  def test_tip_connect_ssh(self):
    output = messages.tip_connect_ssh(self.vm)
    self.assertTrue(len(output) > 1)


  def test_tip_restore_disk(self):
    output = messages.tip_restore_disk(self.vm)
    self.assertTrue(len(output) > 1)


if __name__ == '__main__':
  absltest.main()
