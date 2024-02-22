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

"""Compilations of all disks tasks related."""

import logging

import googleapiclient.errors

from typing import Dict
from threading import Thread
from googleapiclient.errors import HttpError
from gce_rescue.tasks.backup import create_snapshot
from gce_rescue.utils import ThreadHandler as Handler
from gce_rescue.tasks.keeper import wait_for_operation


_logger = logging.getLogger(__name__)
snapshot_thread = None


def _create_rescue_disk(vm, source_disk: str) -> Dict:
    """Create new temporary rescue disk based on source_disk.
    https://cloud.google.com/compute/docs/reference/rest/v1/disks/insert

    Returns:
      operation-result: Dict
    """
    chk_disk_exist = {}
    try:
        chk_disk_exist = (
            vm.compute.disks().get(
                **vm.project_data,
                disk=vm.rescue_disk
            ).execute()
        )
    except googleapiclient.errors.HttpError as e:
        if e.status_code == 404:
            _logger.info('Creating rescue disk %s...', vm.rescue_disk)
        else:
            raise Exception(e) from e

    if 'name' in chk_disk_exist.keys():
        if 'users' in chk_disk_exist.keys():
            disk_users = chk_disk_exist['users']
            raise Exception(
                f'Disk {vm.rescue_disk} is currently in use: {disk_users}')
        _logger.info('Disk %s already exist. Skipping...', vm.rescue_disk)
        return {}

    disk_body = {
        'name': vm.rescue_disk,
        'sourceImage': source_disk,
        'type': f'projects/{vm.project}/zones/{vm.zone}/diskTypes/pd-balanced',
    }
    operation = vm.compute.disks().insert(
        **vm.project_data, body=disk_body
    ).execute()

    result = wait_for_operation(vm, oper=operation)
    return result


def _set_disk_label(vm, disk_name=str) -> Dict:
    """Set labels.rescue=TS to be able to idenfied the boot disk when
    restore the VM to the normal configuration.
    https://cloud.google.com/compute/docs/reference/rest/v1/disks/setLabels

    Return:
      operation-result: Dict
    """
    name_filter = f'name={disk_name}'
    response = list_disk(
        vm,
        project_data=vm.project_data,
        label_filter=name_filter)
    label_fingerprint = response[0]['labelFingerprint']
    request_body = {'labels': {'rescue': vm.ts},
                    'labelFingerprint': label_fingerprint}
    operation = (
        vm.compute.disks()
        .setLabels(**vm.project_data, resource=disk_name, body=request_body)
        .execute()
    )

    return operation


def _delete_rescue_disk(vm, disk_name: str) -> Dict:
    """Delete rescue disk after resetting the instance to the original
    configuration.
    https://cloud.google.com/compute/docs/reference/rest/v1/disks/delete

    Param:
      disk_name: str, Name of the disk to be deleted.
    Returns:
      operation-result: Dict
    """
    _logger.info('Deleting disk %s...', disk_name)
    operation = vm.compute.disks().delete(
        **vm.project_data, disk=disk_name).execute()

    result = wait_for_operation(vm, oper=operation)
    return result


def list_disk(vm, project_data: Dict, label_filter: str) -> Dict:
    """Filter existing disks with labels.rescue=TS.
    https://cloud.google.com/compute/docs/reference/rest/v1/disks/list

    Returns:
      result: Dict
    """
    result = vm.compute.disks().list(
        **project_data,
        filter=label_filter
    ).execute()

    # TODO:
    # Add validation and throw exception if response has more than 1 disk:
    # len(response['items'])
    # For test phase - remember to fix that
    return result['items']


def attach_disk(
        vm,
        disk_name: str,
        device_name: str,
        boot: bool = False
) -> Dict:
    """Attach disk on the instance. By default, it will be attached as a
    secondary.
    https://cloud.google.com/compute/docs/reference/rest/v1/instances/attachDisk

    Returns:
        operation-result: Dict
    """
    if not boot:
        request = _set_disk_label(vm, disk_name)
        if request['status'] != 'DONE':
            _logger.error('Unable to set label to disk %s.', disk_name)
            raise Exception(request)
        else:
            _logger.info('Label configured successfully disk %s.', disk_name)
    attach_disk_body = {
        'boot': boot,
        'name': disk_name,
        'deviceName': device_name,
        'type': 'PERSISTENT',
        'source': f'projects/{vm.project}/zones/{vm.zone}/disks/{disk_name}',
    }
    _logger.info('Attaching disk %s...', disk_name)
    operation = (
        vm.compute.instances()
        .attachDisk(**vm.project_data, instance=vm.name, body=attach_disk_body)
        .execute()
    )

    result = wait_for_operation(vm, oper=operation)
    return result


def _detach_disk(vm, disk: str) -> Dict:
    """Detach disk from the instance.
    https://cloud.google.com/compute/docs/reference/rest/v1/instances/detachDisk
    """

    _logger.info('Detaching disk %s from %s...', disk, vm.name)
    operation = (
        vm.compute.instances()
        .detachDisk(**vm.project_data, instance=vm.name, deviceName=disk)
        .execute()
    )
    result = wait_for_operation(vm, oper=operation)
    return result


def take_snapshot(vm, join_snapshot=None) -> None:
    global snapshot_thread
    if not join_snapshot:
        snapshot_thread = Thread(
            target=create_snapshot, args=(
                vm,), daemon=True)
        snapshot_thread.start()
    else:
        snapshot_thread.join()


def create_rescue_disk(vm) -> None:
    device_name = vm.disks['device_name']
    # task1 = multitasks.Handler(
    #     target = backup,
    #     kwargs={'vm' : vm}
    #     )
    # task1.start()
    task2 = Handler(
        target = _create_rescue_disk,
        kwargs = {
            'vm': vm,
            'source_disk': vm.rescue_source_disk
        },
    )
    task2.start()
    task2.join()
    _detach_disk(vm, disk=device_name)
    attach_disk(
        vm,
        disk_name=vm.rescue_disk,
        device_name=vm.rescue_disk,
        boot=True)


def list_snapshot(vm) -> str:
    snapshot_name = f"{vm.disks['disk_name']}-{vm.ts}"
    try:
        vm.compute.snapshots().get(
            snapshot=snapshot_name,
            project=vm.project
        ).execute()
    except HttpError:
        _logger.info('Snapshot was not found for VM in active rescue mode')
        return ''
    return snapshot_name


def restore_original_disk(vm) -> None:
    """Restore tasks to the original disk."""
    device_name = vm.disks['device_name']

    _detach_disk(vm, disk=vm.rescue_disk)
    _detach_disk(vm, disk=device_name)
    _delete_rescue_disk(vm, disk_name=vm.rescue_disk)
    attach_disk(vm, **vm.disks, boot=True)
