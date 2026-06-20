"""Static guards on the burst-VM terraform modules (ADR-012 stateless lifecycle).

The module internals aren't in the rendered root workdir (the root only calls the module), so these
assert the module source directly: spot preemption + idle self-termination must DELETE the instance
and free its disk — never leave a STOPPED-but-billing instance behind.
"""

from __future__ import annotations

from pathlib import Path

_MODULES = Path(__file__).resolve().parents[3] / "infra" / "terraform" / "modules"
_GCE = _MODULES / "sluice-vm-gce"
_EC2 = _MODULES / "sluice-vm-ec2"


def test_gce_spot_deletes_and_disk_auto_deletes() -> None:
    main_tf = (_GCE / "main.tf").read_text()
    # spot preemption DELETES (was STOP, which leaked the disk); on-demand stays null.
    assert 'instance_termination_action = var.spot ? "DELETE" : null' in main_tf
    # boot disk explicitly dies with the instance.
    assert "auto_delete = true" in main_tf


def test_gce_startup_self_deletes_on_idle() -> None:
    startup = (_GCE / "startup.sh.tpl").read_text()
    # idle self-terminate DELETES the instance (a guest shutdown only STOPs a GCE box), with a
    # power-off fallback for the autoscaler reconcile backstop.
    assert "gcloud compute instances delete" in startup
    assert "|| shutdown -h now" in startup


def test_ec2_spot_terminates_and_root_volume_deleted() -> None:
    main_tf = (_EC2 / "main.tf").read_text()
    assert 'instance_interruption_behavior = "terminate"' in main_tf
    assert 'spot_instance_type             = "one-time"' in main_tf  # terminate requires one-time
    assert 'instance_initiated_shutdown_behavior = "terminate"' in main_tf
    assert "delete_on_termination = true" in main_tf
