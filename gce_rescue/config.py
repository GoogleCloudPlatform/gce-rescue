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

dirname = os.path.dirname(__file__)

config = {
  'verbosity': 'INFO',
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
