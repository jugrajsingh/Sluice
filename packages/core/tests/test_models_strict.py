import pytest
from pydantic import ValidationError
from sluice_core.models import AppSpec, BatchSpec, VmPlacementSpec


def test_should_forbid_extra_on_appspec():
    with pytest.raises(ValidationError):
        AppSpec(name="m", nope=1)


def test_should_forbid_extra_on_nested_models():
    with pytest.raises(ValidationError):
        BatchSpec(bogusField=7)  # unknown key — extra='forbid'
    with pytest.raises(ValidationError):
        VmPlacementSpec(machinetype="x")  # wrong case
