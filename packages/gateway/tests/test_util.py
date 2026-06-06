from sluice_gateway.util import content_hash, eta_seconds


def test_hash_stable():
    assert content_hash(b"abc") == content_hash(b"abc")
    assert len(content_hash(b"abc")) == 64


def test_eta_scales_with_depth():
    assert eta_seconds(visible=0, throughput_per_s=2, min_s=5) == 5
    assert eta_seconds(visible=100, throughput_per_s=2, min_s=5) == 50
