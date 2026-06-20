import pytest
from sluice_autoscaler.k8s import KubeClusterInspector
from sluice_core.interfaces import NoWorkerPods
from sluice_core.models import AppSpec, WorkerState, WorkerStatus


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = chunks

    async def iter_any(self):
        for c in self._chunks:
            yield c


class _FakeResp:
    def __init__(self, chunks):
        self.content = _FakeContent(chunks)


class _FakeCore:
    def __init__(self, chunks):
        self._chunks = chunks
        self.seen: dict = {}

    async def read_namespaced_pod_log(self, **kw):
        self.seen.update(kw)
        return _FakeResp(self._chunks)


def _inspector(core, workers):
    insp = KubeClusterInspector(in_cluster=False, namespace="ns")

    def _get_core():
        return core

    async def _workers(app):
        return workers

    insp._core = _get_core
    insp.workers = _workers
    return insp


async def test_should_stream_active_pod_logs_when_no_pod_given():
    core = _FakeCore([b"line1\n", b"line2\n"])
    insp = _inspector(core, [WorkerStatus(pod="m-run", state=WorkerState.running, age_s=5)])
    out = b"".join([c async for c in insp.pod_logs(AppSpec(name="m"), tail=50)])
    assert out == b"line1\nline2\n"
    assert core.seen["name"] == "m-run" and core.seen["tail_lines"] == 50


async def test_should_use_explicit_pod_when_given():
    core = _FakeCore([b"x"])
    insp = _inspector(core, [])
    _ = [c async for c in insp.pod_logs(AppSpec(name="m"), pod="chosen")]
    assert core.seen["name"] == "chosen"


async def test_should_raise_no_worker_pods_when_none_available():
    core = _FakeCore([b"x"])
    insp = _inspector(core, [])  # no workers, no explicit pod
    with pytest.raises(NoWorkerPods):
        _ = [c async for c in insp.pod_logs(AppSpec(name="m"))]
