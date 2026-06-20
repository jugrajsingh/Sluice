"""gzip helpers for compressing stored bodies.

Used to shrink large, redundant inference *result* bodies (e.g. per-pixel mask JSON) in the object
store. The compressed output self-identifies via the gzip magic header, so no separate metadata is
needed to know later whether a stored body was compressed — which keeps it store-agnostic (the object
is opaque bytes; nothing sets Content-Encoding on the bucket). See
docs/superpowers/specs/2026-06-17-result-gzip-compression-design.md.
"""

from __future__ import annotations

import gzip

_GZIP_MAGIC = b"\x1f\x8b"


def gzip_if_smaller(body: bytes) -> bytes:
    """Return gzip(body) when it is strictly smaller than body, else body unchanged.

    Never grows a body: an incompressible or tiny payload is stored raw. mtime=0 keeps the output
    deterministic (no embedded timestamp).
    """
    packed = gzip.compress(body, compresslevel=6, mtime=0)
    return packed if len(packed) < len(body) else body


def gzip_bytes(body: bytes) -> bytes:
    """Always gzip (deterministic, mtime=0).

    Used where the stored object's key carries a ``.gz`` suffix, so a bucket reader can trust the
    suffix — the body is unconditionally gzipped (vs ``gzip_if_smaller``, which may keep it raw).
    """
    return gzip.compress(body, compresslevel=6, mtime=0)


def is_gzip(body: bytes) -> bool:
    """True if body begins with the gzip magic header (0x1f 0x8b)."""
    return body[:2] == _GZIP_MAGIC


def gunzip(body: bytes) -> bytes:
    return gzip.decompress(body)
