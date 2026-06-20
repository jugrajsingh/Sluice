import gzip
import os

import pytest
from sluice_core.batch_models import BatchFileStatus
from sluice_worker.batch_writer import BrokerBatchWriter


class _Broker:
    def __init__(self):
        self.streamed = []
        self.statuses = {}

    async def batch_output_url(self, job_id, file, start_offset):
        return f"https://signed/{job_id}/{file}/{start_offset}"

    async def put_file(self, url, path):
        with open(path, "rb") as fh:  # noqa: ASYNC230 (test double reading a tiny temp file)
            self.streamed.append((url, fh.read()))

    async def batch_status_put(self, job_id, file, status):
        self.statuses[(job_id, file)] = status

    async def batch_status_get(self, job_id, file):
        return self.statuses.get((job_id, file))


@pytest.mark.asyncio
async def test_should_request_url_then_stream_gzipped_part_when_put_output_part(tmp_path):
    b = _Broker()
    w = BrokerBatchWriter(b, app="app1")
    p = tmp_path / "part.jsonl"
    p.write_bytes(b'{"_rid":"a"}')
    await w.put_output_part("app1", "job1", "part-0.jsonl", 50, os.fspath(p))
    url, body = b.streamed[0]
    assert url == "https://signed/job1/part-0.jsonl/50"
    # Stored gzip-compressed (storage conservation); the client gunzips after download.
    assert gzip.decompress(body) == b'{"_rid":"a"}'


@pytest.mark.asyncio
async def test_should_return_none_when_get_file_status_absent():
    b = _Broker()
    w = BrokerBatchWriter(b, app="app1")
    assert await w.get_file_status("app1", "job1", "part-0.jsonl") is None


@pytest.mark.asyncio
async def test_should_round_trip_status_model_when_put_then_get():
    b = _Broker()
    w = BrokerBatchWriter(b, app="app1")
    st = BatchFileStatus(
        file="part-0.jsonl", state="running", records_total=5, records_done=2, records_failed=0, updated_at=1.0
    )
    await w.put_file_status("app1", "job1", st)
    got = await w.get_file_status("app1", "job1", "part-0.jsonl")
    assert got is not None and got.records_done == 2
