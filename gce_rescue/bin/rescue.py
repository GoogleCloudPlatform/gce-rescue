#!/usr/bin/env python3

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

""" Main script to be used to set/reset rescue mode. """

from datetime import datetime
import logging

from gce_rescue.config import get_config, process_args
from gce_rescue import messages
from gce_rescue.rescue import Instance
from gce_rescue.tasks.actions import call_tasks
from gce_rescue.utils import read_input, set_logging

def main():
  """ Main script function. """
  parser = process_args()
  args = parser.parse_args()

  set_logging(vm_name=args.name, level=get_config('debug'))

  parse_kwargs = {
      'zone': args.zone,
      'name': args.name,
  }

  if args.project:
    parse_kwargs['project'] = args.project

  vm = Instance(test_mode=False, **parse_kwargs)
  rescue_on = vm.rescue_mode_status['rescue-mode']
  if not rescue_on:
    if not args.force:
      info = (f'This option will boot the instance {vm.name} in '
              'RESCUE MODE. \nIf your instance is running it will be rebooted. '
              '\nDo you want to continue [y/N]: ')
      read_input(msg=info)

    print('Starting...')
    # save in the log file current configuration of the VM as backup.
    logging.info('RESTORE#%s\n', vm.data)
    action = 'set_rescue_mode'
    msg = messages.tip_connect_ssh(vm)

  else:
    rescue_ts = vm.rescue_mode_status['ts']
    rescue_date = datetime.fromtimestamp(int(rescue_ts))

    if not args.force:
      info = (f'The instance \"{vm.name}\" is currently configured '
              f'to boot as rescue mode since {rescue_date}.\nWould you like to'
              ' restore the original configuration ? [y/N]: ')
      read_input(msg=info)

    print('Restoring VM...')
    action = 'reset_rescue_mode'
    msg = messages.tip_restore_disk(vm)

  call_tasks(vm=vm, action=action)
  print(msg)


if __name__ == '__main__':
  main()
