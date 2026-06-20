from __future__ import annotations

import re

_PREFIX = "AppData/{app}"

#: Max length for a client-supplied batch input filename (object-store key segment).
_MAX_FILENAME_LEN = 200

#: A safe filename is a single path segment: alnum plus ``.``, ``_``, ``-`` only.
#: Anchored so the whole string must match — no separators, spaces, or punctuation.
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_filename(filename: str) -> str:
    """Return ``filename`` unchanged if it is a safe single path segment, else raise.

    A batch input filename flows from an untrusted client into an object-store key
    (and a presigned PUT). It must therefore be a single safe segment so it cannot
    traverse out of the job's ``input/`` prefix or smuggle separators:

    * non-empty and at most ``_MAX_FILENAME_LEN`` characters,
    * no path separators (``/`` or ``\\``) and no ``..`` parent reference,
    * no leading ``.`` (no hidden / dot files),
    * restricted charset (alnum, ``.``, ``_``, ``-``).

    Raises:
        ValueError: if ``filename`` is not a safe single path segment.
    """
    if not filename or len(filename) > _MAX_FILENAME_LEN:
        raise ValueError(f"invalid batch filename: {filename!r}")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"invalid batch filename: {filename!r}")
    if not _SAFE_FILENAME.match(filename):
        raise ValueError(f"invalid batch filename: {filename!r}")
    return filename


def job_prefix(app: str, job_id: str) -> str:
    return f"{_PREFIX.format(app=app)}/batch/{job_id}"


def input_key(app: str, job_id: str, filename: str) -> str:
    return f"{job_prefix(app, job_id)}/input/{validate_filename(filename)}"


def status_key(app: str, job_id: str, filename: str) -> str:
    return f"{job_prefix(app, job_id)}/status/{filename}.json"


def manifest_key(app: str, job_id: str) -> str:
    return f"{job_prefix(app, job_id)}/manifest.json"


def output_prefix(app: str, job_id: str, filename: str) -> str:
    return f"{job_prefix(app, job_id)}/output/{filename}"


def output_part_key(app: str, job_id: str, filename: str, start_offset: int) -> str:
    # Parts are always gzipped before the presigned PUT, so the key carries .gz — a bucket reader
    # (or the client's presigned download) knows to inflate, without out-of-band metadata.
    return f"{output_prefix(app, job_id, filename)}.part-{start_offset:09d}.jsonl.gz"
