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

"""Test code for backup.py."""

from absl.testing import absltest
from gce_rescue.tasks import backup
from gce_rescue.test.mocks import mock_api_object, MOCK_TEST_VM
from gce_rescue.rescue import Instance


class BackupTest(absltest.TestCase):
  vm : Instance


  def setUp(self):
    self.vm = Instance(test_mode=True, **MOCK_TEST_VM)
    self.vm.compute = mock_api_object(['operations'])


  def test_backup_metadata_items(self):
    """Test Backup_metadata_Items."""
    backup_data = backup.backup_metadata_items(self.vm.data)
    self.assertTrue(backup_data is not None)
    self.assertTrue('key' in backup_data[0])
    self.assertTrue('value' in backup_data[0])


  def test_backup(self):
    """Test backup task."""
    backup.backup(self.vm)


if __name__ == '__main__':
  absltest.main()
