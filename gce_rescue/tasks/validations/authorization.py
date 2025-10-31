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

"""
Authorization validation to be called from ../pre_validations.py
Permissions:
  compute.instances.stop
  compute.instances.start
  compute.instances.attachDisk
  compute.instances.detachDisk
  compute.images.useReadOnly
  compute.disks.use
  compute.disks.setLabels
  compute.snapshots.create
  compute.disks.createSnapshot
  compute.instances.setMetadata
  compute.instances.setLabels
"""
import google.auth
from gce_rescue.tasks.validations.api import api_service

def authorize_check(project: str = None) -> bool:

  permissions_list = ['compute.snapshots.create']
  body_data = {'permissions': permissions_list}
  credentials, project_id = google.auth.default()

  if not project:
    project = project_id

  service = api_service('cloudresourcemanager', 'v1', credentials)
  result = service.projects().testIamPermissions(
    resource = project,
    body =  body_data
  ).execute()

  # BUG FIX: Handle case where result has no 'permissions' key
  # This happens when user has none of the requested permissions
  if 'permissions' not in result:
    raise PermissionError(
      f'Missing required permission: {permissions_list[0]}. '
      'You need at least Compute Instance Admin or Compute Admin role.'
    )

  if permissions_list != result['permissions']:
    raise PermissionError(
      f'Insufficient permissions. Required: {permissions_list}, '
      f'Granted: {result.get("permissions", [])}'
    )

  return True
