import jwt
import pytest
from sluice_core.auth import TokenError, mint_worker_token, verify_worker_token

KEY = "test-signing-key"  # gitleaks:allow (test fixture, not a secret)


def test_mint_then_verify_roundtrip():
    tok = mint_worker_token(app="seg", worker_id="w1", key=KEY, ttl_s=3600)
    claims = verify_worker_token(tok, key=KEY)
    assert claims["app"] == "seg" and claims["worker_id"] == "w1"


def test_expired_token_rejected():
    tok = mint_worker_token(app="seg", worker_id="w1", key=KEY, ttl_s=-1)
    with pytest.raises(TokenError):
        verify_worker_token(tok, key=KEY)


def test_wrong_key_rejected():
    tok = mint_worker_token(app="seg", worker_id="w1", key=KEY, ttl_s=3600)
    with pytest.raises(TokenError):
        verify_worker_token(tok, key="other-key")  # gitleaks:allow (test fixture)


def test_wrong_audience_rejected():
    bad = jwt.encode(
        {
            "app": "seg",
            "worker_id": "w1",
            "aud": "elsewhere",
            "iss": "sluice-autoscaler",
            "iat": 1000,
            "exp": 9999999999,
        },
        KEY,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        verify_worker_token(bad, key=KEY)
