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


def test_tampered_payload_rejected():
    """A token whose payload claims are altered after signing must be rejected.

    Reconstruct the three-part JWT with a modified payload (different ``app`` claim)
    but keep the original signature segment intact — the signature no longer matches the
    new payload, so PyJWT must raise InvalidSignatureError which is wrapped as TokenError.
    """
    import base64
    import json

    tok = mint_worker_token(app="seg", worker_id="w1", key=KEY, ttl_s=3600)
    header_b64, payload_b64, sig_b64 = tok.split(".")

    # Decode the payload, flip the app claim, re-encode WITHOUT re-signing.
    # base64url may lack padding — add it back before decoding.
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    claims = json.loads(base64.urlsafe_b64decode(padded))
    claims["app"] = "TAMPERED"
    new_payload_b64 = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(b"=").decode()

    tampered = f"{header_b64}.{new_payload_b64}.{sig_b64}"
    with pytest.raises(TokenError):
        verify_worker_token(tampered, key=KEY)
