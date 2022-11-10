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

from absl import app, flags, logging
from gce_rescue import messages
from gce_rescue.rescue import Instance
from gce_rescue.tasks.actions import call_tasks
from gce_rescue.utils import log_to_file, read_input

FLAGS = flags.FLAGS
flags.DEFINE_string('project', None, 'The project-id that has the instance.')
flags.DEFINE_string('zone', None, 'Zone where the instance is created.')
flags.DEFINE_string('name', None, 'Instance name.')
flags.DEFINE_boolean('debug', False, 'Print to the log file in debug level.')
flags.DEFINE_boolean('force', False, 'Don\'t ask for confirmation.')
flags.mark_flag_as_required('zone')
flags.mark_flag_as_required('name')


def main(argv):
  del argv

  if FLAGS.debug:
    log_level = 'DEBUG'
  else:
    log_level = 'INFO'
  log_to_file(log_file_name=FLAGS.name, level=log_level)

  parse_kwargs = {
      'zone': FLAGS.zone,
      'name': FLAGS.name,
  }

  if FLAGS.project:
    parse_kwargs['project'] = FLAGS.project

  vm = Instance(test_mode=False, **parse_kwargs)
  rescue_on = vm.rescue_mode_status['rescue-mode']
  if not rescue_on:
    if not FLAGS.force:
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

    if not FLAGS.force:
      info = (f'The instance \"{vm.name}\" is currently configured '
              f'to boot as rescue mode since {rescue_date}.\nWould you like to'
              ' restore the original configuration ? [y/N]: ')
      read_input(msg=info)

    print('Restoring VM...')
    action = 'reset_rescue_mode'
    msg = messages.tip_restore_disk(vm)

  call_tasks(vm=vm, action=action)
  print(msg)

def run_script():
  """ Function to be called as script. """
  app.run(main)

if __name__ == '__main__':
  run_script()
