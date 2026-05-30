from sluice_core.errors import ProvisionFailure
from sluice_core.models import ProvisionError, VmRecord, VmState
from sluice_core.vm_paths import desired_key, heartbeat_key


def test_vm_record_defaults():
    r = VmRecord(
        id="sluice-m-abc123",
        app="m",
        provider="gce",
        region="europe-west3",
        pricing="spot",
        machine_type="g2-standard-8",
    )
    assert r.state is VmState.provisioning and r.last_heartbeat is None


def test_provision_failure_carries_kind():
    e = ProvisionFailure(ProvisionError.STOCKOUT, "ZONE_RESOURCE_POOL_EXHAUSTED")
    assert e.kind is ProvisionError.STOCKOUT and "EXHAUSTED" in str(e)


def test_vm_paths():
    assert heartbeat_key("m", "vm1") == "sluice/apps/m/vms/vm1/heartbeat.json"
    assert desired_key("m", "vm1", root="x") == "x/apps/m/vms/vm1/desired.json"
