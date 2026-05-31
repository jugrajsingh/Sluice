import uuid

import pytest

pytest.importorskip("testcontainers")
from sluice_core.testing.store_conformance import ObjectStoreConformance  # noqa: E402
from sluice_drivers.gcs_store import GcsObjectStore  # noqa: E402
from testcontainers.core.container import DockerContainer  # noqa: E402


@pytest.fixture(scope="session")
def fake_gcs():
    try:
        c = (
            DockerContainer("fsouza/fake-gcs-server:latest")
            .with_command("-scheme http -port 4443")
            .with_exposed_ports(4443)
        )
        c.start()
    except Exception as e:  # Docker daemon unreachable (e.g. CI without Docker)
        pytest.skip(f"Docker not available for fake-gcs-server: {e}")
    host = c.get_container_host_ip()
    port = c.get_exposed_port(4443)
    yield f"http://{host}:{port}"
    c.stop()


class TestGcsStore(ObjectStoreConformance):
    @pytest.fixture
    async def store(self, fake_gcs):
        s = GcsObjectStore(bucket=f"b-{uuid.uuid4().hex}", endpoint=fake_gcs)
        await s.ensure_bucket()
        return s
