"""Tests for BatchFileProcessor — partition, _rid echo, resume, per-record errors."""

from __future__ import annotations

import json

from sluice_worker.batch_driver import BatchFileProcessor


class FakeWriter:
    def __init__(self, resume_done: int = 0) -> None:
        self.parts: dict[int, bytes] = {}
        self.status: dict[str, object] = {}
        self._resume_done = resume_done

    async def put_output_part(self, app: str, job_id: str, filename: str, start_offset: int, body: str) -> None:
        # `body` is now the spilled temp-file PATH (disk-spill, not an in-RAM bytes buffer). Reading it
        # back keeps the existing `parts[offset]` byte assertions valid AND proves the spill happened.
        assert isinstance(body, str), "partition must be spilled to a file path, not held in RAM"
        with open(body, "rb") as fh:  # noqa: ASYNC230 (test double reading a tiny temp file)
            self.parts[start_offset] = fh.read()

    async def put_file_status(self, app: str, job_id: str, st: object) -> None:
        self.status[st.file] = st  # type: ignore[attr-defined]

    async def get_file_status(self, app: str, job_id: str, filename: str) -> object:
        from sluice_core.batch_models import BatchFileStatus

        return BatchFileStatus(
            file=filename,
            state="running",
            records_total=10,
            records_done=self._resume_done,
        )


async def _echo(record: bytes) -> bytes:
    return record  # identity "model"


async def test_should_spill_partition_to_a_file_path_when_flushing() -> None:
    w = FakeWriter()
    lines = [json.dumps({"_rid": f"r{i}"}).encode() for i in range(4)]
    p = BatchFileProcessor(call_record=_echo, writer=w, output_partition_size=2)
    final = await p.process("sam3", "J1", "a.jsonl", lines, resume_from=0)
    assert final.state == "completed" and final.records_done == 4
    assert set(w.parts) == {0, 2}  # offset-named parts flushed at 0 and 2, each from a spilled file
    assert all(b'"_rid"' in v for v in w.parts.values())


async def test_should_partition_output_and_echo_rid_when_processing_records() -> None:
    w = FakeWriter()
    lines = [json.dumps({"_rid": f"r{i}", "x": i}).encode() for i in range(5)]
    p = BatchFileProcessor(call_record=_echo, writer=w, output_partition_size=2)
    st = await p.process("sam3", "J1", "a.jsonl", lines, resume_from=0)

    assert st.records_done == 5 and st.records_failed == 0
    # parts flushed at offsets 0, 2, 4
    assert set(w.parts) == {0, 2, 4}
    first = [json.loads(x) for x in w.parts[0].splitlines()]
    assert first[0]["_rid"] == "r0"  # output echoes _rid


async def test_should_skip_already_done_records_when_resuming() -> None:
    w = FakeWriter(resume_done=2)
    seen: list[int] = []

    async def track(record: bytes) -> bytes:
        seen.append(json.loads(record)["i"])
        return record

    lines = [json.dumps({"_rid": f"r{i}", "i": i}).encode() for i in range(5)]
    p = BatchFileProcessor(call_record=track, writer=w, output_partition_size=2)
    await p.process("sam3", "J1", "a.jsonl", lines, resume_from=2)

    assert seen == [2, 3, 4]  # records 0, 1 skipped on resume
    assert set(w.parts) == {2, 4}  # output parts land at absolute offsets 2 and 4


async def test_should_write_error_entry_not_drop_record_when_per_record_call_fails() -> None:
    w = FakeWriter()

    async def boom(record: bytes) -> bytes:
        if json.loads(record)["i"] == 1:
            raise RuntimeError("bad")
        return record

    lines = [json.dumps({"_rid": f"r{i}", "i": i}).encode() for i in range(3)]
    p = BatchFileProcessor(call_record=boom, writer=w, output_partition_size=10)
    st = await p.process("sam3", "J1", "a.jsonl", lines, resume_from=0)

    assert st.records_failed == 1 and st.records_done == 3
    out = [json.loads(x) for x in w.parts[0].splitlines()]
    assert any(o.get("error") for o in out) and out[1]["_rid"] == "r1"
