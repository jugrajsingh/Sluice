import inspect

import sluice_worker.adapter as adapter_mod


def test_should_not_reference_ambient_object_store_in_amain_source():
    """The batch lane must be wired through the broker, never the ambient store factory (ADR-002/008)."""
    src = inspect.getsource(adapter_mod._amain)
    assert "build_object_store" not in src, "amain must not construct an ambient object store"
    assert "BrokerBatchWriter" in src
