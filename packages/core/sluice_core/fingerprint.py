from __future__ import annotations

import hashlib
import json


def request_fingerprint(body: bytes) -> str:
    """Cache/dedupe key for a request body.

    Returns the body's top-level string ``_rid`` when present and non-empty; otherwise the sha256
    hex of the raw bytes. Used for both real-time dedupe and batch output correlation.
    """
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return hashlib.sha256(body).hexdigest()
    if isinstance(doc, dict):
        rid = doc.get("_rid")
        if isinstance(rid, str) and rid:
            return rid
    return hashlib.sha256(body).hexdigest()
