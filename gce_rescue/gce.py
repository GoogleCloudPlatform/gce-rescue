""" Initilization Instance() with VM information. """

from googleapiclient.discovery import Resource

from dataclasses import dataclass, field
from typing import Dict, List, Union
from time import time
from gce_rescue.tasks.backup import backup_metadata_items
from gce_rescue.tasks.disks import list_disk
from gce_rescue.tasks.pre_validations import Validations
from gce_rescue.config import get_config


def get_instance_info(
  compute: Resource,
  name: str,
  project_data: Dict[str, str]
) -> Dict:
  """Set Dictionary with complete data from instances().get() from the instance.
  https://cloud.google.com/compute/docs/reference/rest/v1/instances/get
  Attributes:
    compute: obj, API Object
    instance: str, Instace name
    project_data: dict, Dictionary containing project and zone keys to be
      unpacked when calling the API.
  """
  return compute.instances().get(
      **project_data,
      instance = name).execute()


def guess_guest(data: Dict) -> str:
  """Determined which Guest OS Family is being used and select a
  different OS for recovery disk.
     Default: projects/debian-cloud/global/images/family/debian-11"""

  guests = get_config('source_guests')
  for disk in data['disks']:
    if disk['boot']:
      if 'architecture' in disk:
        arch = disk['architecture'].lower()
      else:
        arch = 'x86_64'
      guest_default = guests[arch][0]
      guest_name = guest_default.split('/')[-1]
      for lic in disk['licenses']:
        if guest_name in lic:
          guest_default = guests[arch][1]
  return guest_default


def validate_instance_mode(data: Dict) -> Dict:
  """Validate if the instance is already configured as rescue mode."""

  result = {
      'rescue-mode': False,
      'ts': generate_ts()
  }
  if 'metadata' in data and  'items' in data['metadata']:
    metadata = data['metadata']
    for item in metadata['items']:
      if item['key'] == 'rescue-mode':
        result = {
          'rescue-mode': True,
          'ts': item['value']
        }

  return result

def generate_ts() -> int:
  """Get the current timestamp to be used as unique ID
  during this execution."""
  return int(time())


@dataclass
class Instance(Resource):
  """Initialize instance."""
  zone: str
  name: str
  project: str = None
  test_mode: bool = field(default_factory=False)
  compute: Resource = field(init=False)
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
