import gzip

from sluice_core.compression import gunzip, gzip_if_smaller, is_gzip


def test_gzip_if_smaller_compresses_large_redundant_body():
    body = b'{"mask": [' + b"false, " * 100_000 + b"]}"
    out = gzip_if_smaller(body)
    assert is_gzip(out)  # stored compressed
    assert len(out) < len(body)  # and it actually shrank
    assert gunzip(out) == body  # losslessly


def test_gzip_if_smaller_keeps_tiny_body_raw():
    body = b'{"image_url": "https://x/y.jpg"}'  # ~32 B — gzip would only add overhead
    out = gzip_if_smaller(body)
    assert out == body  # stored raw, unchanged
    assert not is_gzip(out)


def test_is_gzip_detects_magic_header_only():
    assert is_gzip(gzip.compress(b"hello " * 100))  # begins 0x1f 0x8b
    assert not is_gzip(b'{"json": true}')  # JSON begins with '{'
    assert not is_gzip(b"")  # empty -> not gzip
    assert not is_gzip(b"\x1f")  # single byte -> not gzip


def test_gunzip_round_trips():
    body = b"some repetitive payload " * 50
    assert gunzip(gzip.compress(body)) == body
