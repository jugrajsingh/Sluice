from __future__ import annotations

import pytest
from sluice_core import batch_paths as bp


def test_should_build_namespaced_keys_when_given_app_and_job() -> None:
    assert bp.job_prefix("sam3", "J1") == "AppData/sam3/batch/J1"
    assert bp.input_key("sam3", "J1", "a.jsonl") == "AppData/sam3/batch/J1/input/a.jsonl"
    assert bp.status_key("sam3", "J1", "a.jsonl") == "AppData/sam3/batch/J1/status/a.jsonl.json"
    assert bp.manifest_key("sam3", "J1") == "AppData/sam3/batch/J1/manifest.json"
    assert bp.output_prefix("sam3", "J1", "a.jsonl") == "AppData/sam3/batch/J1/output/a.jsonl"


def test_should_name_output_part_by_offset_when_flushed() -> None:
    k = bp.output_part_key("sam3", "J1", "a.jsonl", 2000)
    assert k == "AppData/sam3/batch/J1/output/a.jsonl.part-000002000.jsonl.gz"
    assert bp.output_part_key("sam3", "J1", "a.jsonl", 2000) == k  # resume overwrites same key


def test_should_accept_safe_filename_when_single_segment() -> None:
    for good in ("a.jsonl", "data_001.jsonl", "shard-12.jsonl", "x.JSONL"):
        assert bp.validate_filename(good) == good


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "../escape.jsonl",  # parent traversal
        "a/b.jsonl",  # nested segment
        "/abs.jsonl",  # absolute / leading slash
        ".hidden",  # leading dot
        "..",  # bare parent
        "a\\b.jsonl",  # backslash separator
        "name with space.jsonl",  # disallowed charset
        "weird;name.jsonl",  # disallowed punctuation
        "x" * 256,  # over length cap
    ],
)
def test_should_reject_unsafe_filename_when_not_single_safe_segment(bad: str) -> None:
    with pytest.raises(ValueError, match="filename"):
        bp.validate_filename(bad)
