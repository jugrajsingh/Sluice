import hashlib

from sluice_core.fingerprint import request_fingerprint


def test_should_use_explicit_rid_when_present():
    a = request_fingerprint(b'{"_rid": "user-key-1", "inputs": [1]}')
    b = request_fingerprint(b'{"_rid": "user-key-1", "inputs": [2]}')  # different body, same _rid
    assert a == "user-key-1" and b == "user-key-1" and a == b


def test_should_fall_back_to_body_hash_when_rid_absent():
    body = b'{"inputs": [1]}'
    assert request_fingerprint(body) == hashlib.sha256(body).hexdigest()


def test_should_fall_back_to_body_hash_when_rid_blank_or_non_string_or_not_json():
    h = hashlib.sha256
    assert request_fingerprint(b'{"_rid": "", "x": 1}') == h(b'{"_rid": "", "x": 1}').hexdigest()
    assert request_fingerprint(b'{"_rid": 5}') == h(b'{"_rid": 5}').hexdigest()
    assert request_fingerprint(b"not json") == h(b"not json").hexdigest()
