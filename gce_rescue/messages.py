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

"""List of messages to inform and educate the user."""

from gce_rescue.gce import Instance


def tip_connect_ssh(vm: Instance) -> str:
    return (
        '└── Your instance is READY! You can now connect your instance '
        f'{vm.name} via:'
        '\n  1. CLI. (add --tunnel-through-iap if necessary)'
        f'\n    $ gcloud compute ssh {vm.name} --zone={vm.zone} '
        f'--project={vm.project} --ssh-flag="-o StrictHostKeyChecking=no"'
        '\n  OR'
        '\n  2. Google Cloud Console:'
        f'\n    https://ssh.cloud.google.com/v2/ssh/projects/{vm.project}'
        f'/zones/{vm.zone}/instances/{vm.name}?authuser=0&hl=en_US&'
        'useAdminProxy=true&troubleshoot4005Enabled=true\n'
    )


def tip_restore_disk(vm: Instance, snapshot=False) -> str:
    if not snapshot:
        snapshot_restore_msg = ''
    else:
        snapshot_restore_msg = (
            'Use the snapshot below if you need to restore the modification '
            'made while the instance was in rescue mode.'
            f"\nSnapshot name: {vm.disks['disk_name']}-{vm.ts}"
            f'\nMore information: '
            'https://cloud.google.com/compute/docs/disks/restore-snapshot\n'
        )

    return f'└── The instance {vm.name} was restored! {snapshot_restore_msg}'
