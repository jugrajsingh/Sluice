import pytest
from sluice_core.app_yaml import parse_app_yaml

BASE = "apiVersion: sluice/v1\nkind: App\nmetadata: {name: m}\nspec:\n  image: r/x:1\n"


def test_should_reject_unknown_top_level_field():
    with pytest.raises(ValueError, match="bogusTopLevel|extra"):
        parse_app_yaml(BASE + "  bogusTopLevel: 1\n")


def test_should_reject_typo_in_nested_batch():
    with pytest.raises(ValueError, match="batchSLAHours|extra"):
        parse_app_yaml(BASE + "  batch: {batchSLAHours: 99}\n")


def test_should_reject_unknown_key_in_queue_block():
    with pytest.raises(ValueError, match="queue|extra"):
        parse_app_yaml(BASE + "  queue: {ref: m, bogus: 1}\n")


def test_should_accept_a_fully_valid_spec():
    spec = parse_app_yaml(BASE + "  batch: {batchSlaHours: 12, outputPartitionSize: 500}\n")
    assert spec.batch.batch_sla_hours == 12 and spec.batch.output_partition_size == 500


def test_should_round_trip_idempotently():
    # serialize∘parse must be stable so `apply --dry-run` shows no phantom diffs on an unchanged spec.
    from sluice_core.app_yaml import serialize_app_yaml

    once = serialize_app_yaml(parse_app_yaml(BASE))
    twice = serialize_app_yaml(parse_app_yaml(once))
    assert once == twice
