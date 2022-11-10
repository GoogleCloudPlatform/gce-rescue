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

""" Authentication validation to be called from ../pre_validations.py """
import googleapiclient.discovery
import google.auth
import sys
from gce_rescue.test.mocks import mock_api_object

PROJECT = ''

def _get_auth():
  global PROJECT
  try:
    credentials, adc_project = google.auth.default()
    if not adc_project and not PROJECT:
      msg = _info_no_project()
      print(msg, file=sys.stderr)
      sys.exit(1)
    if not PROJECT and adc_project:
      PROJECT = adc_project
    return credentials
  except google.auth.exceptions.DefaultCredentialsError:
    msg = _info_auth_cred()
    print(msg, file=sys.stderr)
    sys.exit(1)

def authenticate_check(
  zone: str,
  instance_name: str,
  project: str = None,
  test_mode: bool = False
) -> googleapiclient.discovery.Resource:
  global PROJECT
  PROJECT = project
  if test_mode:
    service = mock_api_object(['compute'])
    return service
  credentials = _get_auth()
  if not credentials:
    return False
  service = googleapiclient.discovery.build(
    'compute',
    'v1',
    credentials = credentials
  )
  request = service.instances().get(
		project = PROJECT,
		zone = zone,
		instance = instance_name)
  try:
    request.execute()
    return service
  except google.auth.exceptions.RefreshError:
    msg = _info_auth_refresh()
    print(msg, file=sys.stderr)
    sys.exit(1)

def project_name() -> str:
  return PROJECT

def _info_auth_refresh() -> str:
  return (
	'    Please use application-default Credentials (ADC) to authenticate:\n'
	'    $ gcloud auth login --update-adc'
  )

def _info_auth_cred() -> str:
  return (
	'    Please use application-default Credentions (ADC) to authenticate:\n'
	'     $ gcloud auth application-default login\n'
	f'     $ gcloud auth application-default set-quota-project {PROJECT}'
  )

def _info_no_project() -> str:
  return (
	'    Was not possible to find the project where the VM is created.\n'
	'    You can use the option --project to declare the project-id or '
  'set to your configuration:\n'
	'    $ gcloud config set project PROJECT_ID'
  )
