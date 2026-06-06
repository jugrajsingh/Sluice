from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sluice_autoscaler import main as m


class FakeCoord:
    def __init__(self, lease):
        self.lease = lease
        self.replaced = None

    async def read_namespaced_lease(self, name, namespace):
        return self.lease

    async def replace_namespaced_lease(self, name, namespace, body):
        self.replaced = body


def _lease(holder, renewed_ago_s, duration=45):
    return SimpleNamespace(
        spec=SimpleNamespace(
            holder_identity=holder,
            renew_time=datetime.now(UTC) - timedelta(seconds=renewed_ago_s),
            lease_duration_seconds=duration,
            acquire_time=None,
        )
    )


async def test_stale_foreign_lease_is_taken_over():
    coord = FakeCoord(_lease("dead-pod", renewed_ago_s=300, duration=45))
    assert await m._try_acquire_lease(coord) is True
    assert coord.replaced.spec.holder_identity == m.IDENTITY


async def test_fresh_foreign_lease_is_respected():
    coord = FakeCoord(_lease("other-live-pod", renewed_ago_s=5, duration=45))
    assert await m._try_acquire_lease(coord) is False
    assert coord.replaced is None


async def test_own_lease_renews():
    coord = FakeCoord(_lease(m.IDENTITY, renewed_ago_s=10, duration=45))
    assert await m._try_acquire_lease(coord) is True
    assert coord.replaced.spec.holder_identity == m.IDENTITY
