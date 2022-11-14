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

""" Multitask test code """

from absl.testing import absltest
from gce_rescue.multitasks import Handler
import time

OUTPUT1 = 'task1 done'
OUTPUT2 = 'task1 done'


class MultitasksTest(absltest.TestCase):
  status = {
	  'task1_done': False,
	  'task2_done': False,
	}


  @classmethod
  def task1(cls):
    for i in range(0, 3):
      print(f'task 1, time {i}')
      time.sleep(0.001)
    MultitasksTest.status['task1_done'] = True
    return OUTPUT1


  @classmethod
  def task2(cls):
    for i in range(0, 3):
      print(f'task 2, time {i}')
      time.sleep(0.001)
    MultitasksTest.status['task2_done'] = True
    return OUTPUT2


  def test_multitasks(self):
    t1 = Handler(target=MultitasksTest.task1)
    t2 = Handler(target=MultitasksTest.task2)
    t1.start()
    t2.start()
    output1 = t1.result()
    output2 = t2.result()
    self.assertEqual(output1, OUTPUT1)
    self.assertEqual(output2, OUTPUT2)
    self.assertTrue(MultitasksTest.status['task1_done'])
    self.assertTrue(MultitasksTest.status['task2_done'])


if __name__ == '__main__':
  absltest.main()
