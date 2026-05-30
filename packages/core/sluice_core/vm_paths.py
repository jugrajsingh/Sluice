from __future__ import annotations


def heartbeat_key(app: str, vm_id: str, *, root: str = "sluice") -> str:
    return f"{root}/apps/{app}/vms/{vm_id}/heartbeat.json"


def desired_key(app: str, vm_id: str, *, root: str = "sluice") -> str:
    return f"{root}/apps/{app}/vms/{vm_id}/desired.json"
