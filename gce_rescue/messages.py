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

""" List of messages to inform and educate the user. """

from gce_rescue.gce import Instance

def tip_connect_ssh(vm: Instance) -> str:
  return (f'└── Your instance is READY! You can now connect your instance '
    f' {vm.name} via:\n  1. CLI. (add --tunnel-through-iap if necessary)\n'
    f'    $ gcloud compute ssh {vm.name} --zone={vm.zone} '
    f'--project={vm.project} --ssh-flag="-o StrictHostKeyChecking=no"\n  OR\n'
    f'  2. Google Cloud Console:\n'
    f'    https://ssh.cloud.google.com/v2/ssh/projects/{vm.project}/zones/'
    f'{vm.zone}/instances/{vm.name}?authuser=0&hl=en_US&useAdminProxy=true&'
    f'troubleshoot4005Enabled=true\n')

def tip_restore_disk(vm: Instance, snapshot=False) -> str:
  if not snapshot:
    snapshot_restore_msg = ''
  else:
    disk_name = vm.disks['disk_name']
    snapshot_restore_msg = (f' Use the snapshot below '
    f'if you need to restore the modification made while the instance was '
    f'in rescue mode.\n Snapshot name: {disk_name}-{vm.ts}\n'
    f' More information: '
    f'https://cloud.google.com/compute/docs/disks/restore-snapshot\n')

  return f'└── The instance {vm.name} was restored!' + snapshot_restore_msg
