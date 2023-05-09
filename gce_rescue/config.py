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

""" Default configurations values. """

import os
import argparse

dirname = os.path.dirname(__file__)

VERSION = '0.2-1'

config = {
  'version': VERSION,
  'debug': False,
  'startup-script-file': os.path.join(dirname, 'startup-script.txt'),
  'source_guests': {
    'x86_64':[
      'projects/debian-cloud/global/images/family/debian-11',
      'projects/rocky-linux-cloud/global/images/family/rocky-linux-9'
      ],
    'arm64':[
      'projects/debian-cloud/global/images/family/debian-11-arm64',
      'projects/rocky-linux-cloud/global/images/family/rocky-linux-9-arm64'
    ]
  }
}

def get_config(key):
  if key in config:
    return config[key]


def process_args():
  """ Print usage options. """
  parser = argparse.ArgumentParser(description='GCE Rescue v0.0.2-1 - Set/Reset\
    GCE instances to boot in rescue mode.')
  parser.add_argument('-p', '--project',
                      help='The project-id that has the instance.')
  parser.add_argument('-z', '--zone', help='Zone where the instance \
    is created.',
                      required=True)
  parser.add_argument('-n', '--name', help='Instance name.', required=True)
  parser.add_argument('-d', '--debug', action='store_true',
                      help='Print to the log file in debug leve')
  parser.add_argument('-f', '--force', action='store_true',
                      help='Don\'t ask for confirmation.')

  return parser


def set_configs(user_args):
  config['debug'] = getattr(user_args, 'debug')
