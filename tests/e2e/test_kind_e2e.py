import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SLUICE_KIND") != "1", reason="requires kind + helm + built images (set SLUICE_KIND=1)"
)


def test_infer_roundtrip_on_kind():
    # 1) helm install sluice (memory/local backends for the smoke) + the example worker
    # 2) POST /v1/topwear/infer; expect 202 + ticket
    # 3) poll /v1/topwear/status/<ticket> until 200 with a JSON mask
    # 4) GET console /v1/apps shows topwear with workers running, scale_status ready
    # 5) POST pause -> Model.spec.desiredState == Paused; workers drain
    ...
