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

my_pwd = path.abspath(path.dirname(__file__))

# Get the long description from the README file
with open(path.join(my_pwd, 'README.md'), encoding='utf-8') as f:
  long_description_readme = f.read()

# Get requirements
with open(path.join(my_pwd, 'requirements.txt'), encoding='utf-8') as f:
  list_requirements = f.readlines()

setup(
  name = 'gce-rescue',
  version = '0.0.1',
  description='GCE Rescue - Boot your GCE VM in rescue mode.',
  url = 'https://github.com/googlecloudplatform/gce-rescue',
  author = 'Halley de Souza',
  author_email = 'gce-rescue-dev@google.com',
  license = 'Apache-2.0',
  long_description = long_description_readme,
  long_description_content_type = 'text/markdown',
  install_requires = list_requirements,
  zip_safe = False,
  packages = find_packages(exclude=['test', '*_test.py']),
  package_dir = {'': '.'},
  package_data = {'': ['startup-script.txt']},
  include_package_data = True,
  entry_points={
        'console_scripts': [
            'gce-rescue = gce_rescue.bin.rescue:run_script',
        ],
    },
  classifiers = [
    "Development Status :: 3 - Alpha",
    "License :: OSI Approved :: Apache Software License",
    "Operating System :: OS Independent",
    "Programming Language :: Python :: 3",
    "Topic :: System :: Boot",
    "Topic :: System :: Boot :: Init",
    "Topic :: System :: Recovery Tools",
    "Topic :: System :: System Shells",
    "Topic :: System :: Systems Administration",
  ]
)
