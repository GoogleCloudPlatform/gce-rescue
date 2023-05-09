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

""" Common API objects """
import googleapiclient
import google_auth_httplib2
import httplib2

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource

def api_service(
    service: str,
    version: str,
    credentials: Credentials) -> Resource:

  def _builder(http, *args, **kwargs):
    # google api client is not thread safe
    # https://github.com/googleapis/google-api-python-client/blob/main/docs/thread_safety.md
    del http
    headers = kwargs.setdefault('headers',{})
    headers['user-agent'] = 'gce_rescue_header'
    auth_http = google_auth_httplib2.AuthorizedHttp(credentials,
                                                   http=httplib2.Http())
    return googleapiclient.http.HttpRequest(auth_http, *args, **kwargs)

  service_ = googleapiclient.discovery.build(service, version,
                        cache_discovery=False,
                        credentials=credentials,
                        requestBuilder=_builder)
  return service_
