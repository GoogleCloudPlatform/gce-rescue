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

""" Initilization Instance() with VM information. """

from dataclasses import dataclass, field
from typing import Dict, List, Union

from gce_rescue.tasks.backup import backup_metadata_items
from gce_rescue.tasks.disks import list_disk
from gce_rescue.tasks.pre_validations import Validations
from gce_rescue.utils import (
  validate_instance_mode,
  guess_guest,
  get_instance_info
)
import googleapiclient.discovery

@dataclass
class Instance:
  """Initialize instance."""
  zone: str
  name: str
  project: str = None
  test_mode: bool = field(default_factory=False)
  compute: googleapiclient.discovery.Resource = field(init=False)
  data: Dict[str, Union[str, int]] = field(init=False)
  ts: int = field(init=False)
  _status: str = ''
  _rescue_source_disk: str = ''
  _rescue_mode_status: Dict[str, Union[str, int]] = field(
    default_factory=lambda: ({})
  )
  _disks: Dict[str, str] = field(default_factory=lambda: ({}))
  _backup_items: Dict[str, Union[str, int]] = field(
    default_factory=lambda: ([])
  )

  def __post_init__(self):
    check = Validations(
        name=self.name,
        test_mode=self.test_mode,
        **self.project_data
    )
    self.compute = check.compute
    self.project = check.adc_project
    self.data = get_instance_info(
        compute=self.compute,
        name=self.name,
        project_data=self.project_data)

    self._rescue_mode_status = validate_instance_mode(self.data)
    self.ts = self._rescue_mode_status['ts']
    self._status = self.data['status']
    self._rescue_source_disk = guess_guest(self.data)
    self._disks = self._define_disks()

    # Backup metadata items
    self._backup_items = backup_metadata_items(
        data=self.data
    )

  def refresh_fingerprint(self) -> None:
    """Refresh the current metadata fingerprint value."""

    project_data = get_instance_info(
        compute=self.compute,
        name=self.name,
        project_data=self.project_data)

    new_fingerprint = project_data['metadata']['fingerprint']
    self.data['metadata']['fingerprint'] = new_fingerprint

  def _define_disks(self) -> Dict[str, str]:
    """Define the values of disk_name and device_name."""

    rescue_on = self._rescue_mode_status['rescue-mode']
    if not rescue_on:
      for disk in self.data['disks']:
        if disk['boot']:
          device_name = disk['deviceName']
          source = disk['source']
          disk_name = source.split('/')[-1]

    else:
      ts = self._rescue_mode_status['ts']
      disk_filter = f'labels.rescue={ts}'

      disk = list_disk(
        vm=self,
        project_data=self.project_data,
        label_filter=disk_filter
      )

      disk_name = disk[0]['name']
      disks = self.data['disks']
      for disk in disks:
        full_source = disk['source']
        source = full_source.split('/')[-1]
        if disk_name == source:
          device_name = disk['deviceName']

    result = {
        'device_name': device_name,
        'disk_name': disk_name
    }
    return result

  @property
  def rescue_mode_status(self) -> Dict[str, Union[str, int]]:
    return self._rescue_mode_status

  @property
  def project_data(self) -> str:
    return {'project': self.project, 'zone': self.zone}

  @property
  def rescue_disk(self) -> str:
    return f'linux-rescue-disk-{self.ts}'

  @property
  def status(self) -> str:
    return self._status

  @status.setter
  def status(self, v: str) -> None:
    self._status = v

  @property
  def rescue_source_disk(self) -> str:
    return self._rescue_source_disk

  @rescue_source_disk.setter
  def rescue_source_disk(self, v: str) -> None:
    self._rescue_source_disk = v

  @property
  def backup_items(self) -> List[str]:
    return self._backup_items

  @backup_items.setter
  def backup_items(self, v: List[str]) -> None:
    self._backup_items = v

  @property
  def disks(self) -> List[str]:
    return self._disks
