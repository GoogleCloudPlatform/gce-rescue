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

""" Install GCE Rescue """

from setuptools import setup, find_packages
from os import path

# Import V2 version for beta releases, fallback to V1 for stable
try:
    from gce_rescue_v2.core.config import VERSION
except ImportError:
    from gce_rescue.config import VERSION

my_pwd = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(my_pwd, 'README.md'), encoding='utf-8') as f:
  long_description_readme = f.read()

# Combined dependencies for V1 + V2
install_requires = [
    # V1 dependencies
    'absl-py>=2.3.0',
    'google-api-python-client>=2.185.0',
    'google-auth>=2.42.0',
    # V2 additional dependencies
    'google-auth-httplib2>=0.2.0',
    'httplib2>=0.22.0',
    'pyyaml>=6.0.1',
]

setup(
  name = 'gce-rescue',
  version = VERSION,
  description='GCE Rescue - Boot your GCE VM in rescue mode (V1 + V2 Beta).',
  url = 'https://github.com/googlecloudplatform/gce-rescue',
  author = 'GCE Rescue Team',
  author_email = 'gce-rescue-dev@google.com',
  license = 'Apache-2.0',
  long_description = long_description_readme,
  long_description_content_type = 'text/markdown',
  install_requires = install_requires,
  zip_safe = False,
  packages = find_packages(exclude=['test', '*_test.py']),  # Finds both gce_rescue and gce_rescue_v2
  package_dir = {'': '.'},
  package_data = {
      '': ['startup-script.txt'],
      'gce_rescue_v2': ['startup_scripts/*.sh', 'startup_scripts/*.ps1'],
  },
  include_package_data = True,
  python_requires='>=3.9',
  entry_points={
        'console_scripts': [
            'gce-rescue = gce_rescue.bin.rescue:main',        # V1 (Legacy/Stable)
            'gce-rescue-v2 = gce_rescue_v2.cli:main',         # V2 (Beta/New)
        ],
    },
  classifiers = [
    'Development Status :: 4 - Beta',
    'License :: OSI Approved :: Apache Software License',
    'Operating System :: OS Independent',
    'Programming Language :: Python :: 3.9',
    'Programming Language :: Python :: 3.10',
    'Programming Language :: Python :: 3.11',
    'Programming Language :: Python :: 3.12',
    'Programming Language :: Python :: 3.13',
    'Topic :: System :: Boot',
    'Topic :: System :: Boot :: Init',
    'Topic :: System :: Recovery Tools',
    'Topic :: System :: System Shells',
    'Topic :: System :: Systems Administration',
  ]
)
