import asyncio

from handler import SegHandler


def test_predict_returns_one_result_per_input():
    h = SegHandler()
    asyncio.run(h.load())
    out = asyncio.run(h.predict([b"imgA", b"imgB"]))
    assert len(out) == 2
    assert all(o.startswith(b"{") for o in out)  # JSON mask payloads
