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

"""Different operations to guarantee VM disks backup, before performing
any modifications."""

import logging

from typing import Dict, List
from gce_rescue.tasks.keeper import wait_for_operation


_logger = logging.getLogger(__name__)


def backup_metadata_items(data: Dict) -> List:
    """Returns the 'items' content (ssh-keys, scripts, etc) to be
    restored at the end of the process. After the instance booted
    and executed the rescue start-script
    """
    if 'items' in data['metadata'].keys():
        return data['metadata']['items']
    return []


def create_snapshot(vm) -> Dict:
    """
    Create a snaphost of the instance boot disk, adding self._ts to the
    disk name.
    https://cloud.google.com/compute/docs/reference/rest/v1/disks/createSnapshot

    Returns:
      operation-result: Dict
    """
    disk_name = vm.disks['disk_name']
    # Patch issues/23
    region = vm.zone[:-2]
    snapshot_name = f'{disk_name}-{vm.ts}'
    snapshot_body = {'name': snapshot_name, 'storageLocations': [region]}
    _logger.info('Creating snapshot %s...', snapshot_body)

    operation = (
        vm.compute.disks()
        .createSnapshot(**vm.project_data, disk=disk_name, body=snapshot_body)
        .execute()
    )
    result = wait_for_operation(vm, oper=operation)
    return result
