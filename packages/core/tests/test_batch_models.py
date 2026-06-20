import pytest
from pydantic import ValidationError
from sluice_core.models import AppSpec, BatchSpec, ResourcesSpec


def _app(**kw):
    return AppSpec(name="sam3", image="i", handler="h:H", resources=ResourcesSpec(gpu=1), **kw)


def test_should_default_to_no_batch_and_field_defaults_when_app_built():
    assert _app().batch is None
    a = _app(batch=BatchSpec())
    assert a.batch.batch_sla_hours == 24 and a.batch.output_partition_size == 1000
    assert a.batch_queue_ref == "sam3-batch"


def test_should_parse_camel_aliases_when_validated_from_dict():
    a = AppSpec.model_validate(
        {
            "name": "sam3",
            "image": "i",
            "handler": "h:H",
            "resources": {"gpu": 1},
            "scaling": {"inferSlaMinutes": 45, "ratePerInstancePerMin": 2000, "putConcurrency": 8},
            "batch": {"batchSlaHours": 12},
        }
    )
    assert a.scaling.infer_sla_minutes == 45 and a.scaling.rate_per_instance_per_min == 2000
    assert a.batch.batch_sla_hours == 12


def test_should_reject_when_output_partition_size_is_zero():
    with pytest.raises(ValidationError):
        BatchSpec(outputPartitionSize=0)
