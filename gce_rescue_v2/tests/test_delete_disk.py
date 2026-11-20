"""Request IDs on retried Compute mutations."""

from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from googleapiclient.discovery import build
from googleapiclient.http import HttpMockSequence

from gce_rescue_v2.core.auth import RetryingHttpRequest
from gce_rescue_v2.operations.delete_disk import DeleteDiskOperation


def test_delete_retry_reuses_request_id():
    transport = HttpMockSequence([
        ({"status": "503"}, b"retry"), ({"status": "200"}, b"{}"),
        ({"status": "200"}, b"{}"),
    ])
    compute = build("compute", "v1", http=transport, requestBuilder=RetryingHttpRequest)
    op = DeleteDiskOperation(compute, "project", "zone", Mock())
    assert op.execute("first").success
    assert op.execute("second").success
    ids = [UUID(parse_qs(urlsplit(uri).query)["requestId"][0])
           for uri, _, _, _ in transport.request_sequence]
    assert ids[0] == ids[1]
    assert ids[0] != ids[2]
    assert all(request_id.version == 4 for request_id in ids)
