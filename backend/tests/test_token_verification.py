from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core import security

AUTH_ORIGIN = "https://ep-test.neonauth.example.aws.neon.tech"
AUTH_BASE_URL = f"{AUTH_ORIGIN}/neondb/auth"


@pytest.fixture
def signing_key(monkeypatch):
    """Real Ed25519 signatures, with only the network fetch of the public key replaced."""
    private_key = Ed25519PrivateKey.generate()
    jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _: SimpleNamespace(key=private_key.public_key())
    )
    monkeypatch.setattr(security, "_jwks_client", lambda _: jwks)
    monkeypatch.setattr(
        security,
        "get_settings",
        lambda: SimpleNamespace(
            neon_auth_jwks_url=f"{AUTH_BASE_URL}/.well-known/jwks.json",
            neon_auth_base_url=AUTH_BASE_URL,
        ),
    )
    return private_key


def token(key, issuer: str) -> str:
    now = datetime.now(UTC)
    claims = {"sub": "user-1", "iss": issuer, "iat": now, "exp": now + timedelta(minutes=15)}
    return jwt.encode(claims, key, algorithm="EdDSA")


def test_token_from_the_auth_origin_is_accepted(signing_key):
    assert security._verify_token_sync(token(signing_key, AUTH_ORIGIN))["sub"] == "user-1"


@pytest.mark.parametrize("issuer", ["https://attacker.example", AUTH_BASE_URL])
def test_token_from_any_other_issuer_is_rejected(signing_key, issuer):
    with pytest.raises(jwt.InvalidIssuerError):
        security._verify_token_sync(token(signing_key, issuer))


def test_token_signed_by_another_key_is_rejected(signing_key):
    with pytest.raises(jwt.InvalidSignatureError):
        security._verify_token_sync(token(Ed25519PrivateKey.generate(), AUTH_ORIGIN))
