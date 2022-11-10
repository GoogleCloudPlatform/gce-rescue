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

""" Validations that should be performed before continue the script execution.
    Each validations steps can be appended to the class Validations() and keep
    the file under validations/ folder."""

from dataclasses import dataclass, field
import googleapiclient.discovery
from gce_rescue.tasks.validations.authentication import (
  authenticate_check,
  project_name
)

@dataclass
class Validations:
  """Run different validations before continue the script.
  	Return:
  		discovery.build object.
  """
  zone: str
  name: str
  project: str = None
  test_mode: bool = field(default_factory=False)

  def _authentication(self):
    return authenticate_check(
      project = self.project,
      zone = self.zone,
      instance_name = self.name,
      test_mode = self.test_mode,
    )

  @property
  def compute(self) -> googleapiclient.discovery.Resource:
    return self._authentication()

  @property
  def adc_project(self) -> str:
    return project_name()
