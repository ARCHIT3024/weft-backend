"""Behavioural tests for `app/core/security.py` — the crypto primitives.

Before this file existed, `app/core/security.py` sat at ~50% coverage: the
module was imported (so the `CryptContext` construction and the function
`def` lines counted as executed) but not one of the four functions had ever
been *called* by a test. Password hashing, token signing, token validation and
refresh-token generation were unproven.

Nothing here is mocked. The `.env` in the backend root carries a real RS256
keypair, so every JWT assertion below is a genuine sign/verify round trip
against the same key material the application uses. The only key that is
manufactured is the *foreign* one, which exists to prove a token signed by
someone else does not validate.

No database, no network, no Redis.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.config import settings
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_jwt,
    hash_password,
    hash_refresh_token,
    verify_password,
)

USER_ID = "11111111-1111-1111-1111-111111111111"
PASSWORD = "correct-horse-battery-staple"


# ── Helpers ─────────────────────────────────────────────────────────────


def _foreign_private_key_pem() -> str:
    """An RSA private key the application has never seen.

    Generated per-call rather than at import so a collection-time failure can
    never be blamed on this module; 2048 bits keeps it fast enough for a test.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _sign(claims: dict[str, object], key: str | None = None) -> str:
    """Sign arbitrary claims with the configured (or a supplied) private key."""
    return jwt.encode(claims, key or settings.JWT_PRIVATE_KEY, algorithm=settings.JWT_ALGORITHM)


def _tamper_with_payload(token: str, **overrides: object) -> str:
    """Rewrite payload claims while keeping the original signature attached."""
    header, payload, signature = token.split(".")
    claims = json.loads(_b64url_decode(payload))
    claims.update(overrides)
    forged = _b64url_encode(json.dumps(claims, separators=(",", ":")).encode())
    return f"{header}.{forged}.{signature}"


# ── Password hashing ────────────────────────────────────────────────────


def test_hash_is_never_the_plaintext() -> None:
    """The stored value must not be, or contain, the password itself."""
    hashed = hash_password(PASSWORD)

    assert hashed != PASSWORD
    assert PASSWORD not in hashed
    assert hashed.startswith("$2"), "expected a bcrypt modular-crypt hash"


def test_verify_password_accepts_the_right_password() -> None:
    assert verify_password(PASSWORD, hash_password(PASSWORD)) is True


@pytest.mark.parametrize(
    "wrong",
    [
        "correct-horse-battery-stapl",  # one char short
        "Correct-Horse-Battery-Staple",  # case differs
        "correct-horse-battery-staple ",  # trailing space
        "",
    ],
    ids=["truncated", "case-flipped", "trailing-space", "empty"],
)
def test_verify_password_rejects_the_wrong_password(wrong: str) -> None:
    assert verify_password(wrong, hash_password(PASSWORD)) is False


def test_same_password_hashes_differently_each_time() -> None:
    """Distinct salts: two hashes of one password must differ, yet both verify.

    Equal hashes would mean an unsalted scheme, which makes the user table
    rainbow-table-able and leaks which accounts share a password.
    """
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)

    assert first != second
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)


def test_hash_is_not_a_plain_digest_of_the_password() -> None:
    """Guards against a future 'optimisation' to sha256/md5."""
    hashed = hash_password(PASSWORD)

    assert hashlib.sha256(PASSWORD.encode()).hexdigest() not in hashed
    assert hashlib.md5(PASSWORD.encode()).hexdigest() not in hashed  # noqa: S324


# ── Access tokens: happy path ───────────────────────────────────────────


def test_access_token_round_trips_with_claims_intact() -> None:
    token = create_access_token(user_id=USER_ID, role="CITIZEN", email="citizen@test.com")
    claims = decode_jwt(token)

    assert claims["sub"] == USER_ID
    assert claims["role"] == "CITIZEN"
    assert claims["email"] == "citizen@test.com"
    assert claims["exp"] > claims["iat"]
    assert len(claims["jti"]) == 32


def test_email_claim_is_omitted_when_not_supplied() -> None:
    claims = decode_jwt(create_access_token(user_id=USER_ID, role="ADMIN"))

    assert "email" not in claims
    assert claims["role"] == "ADMIN"


def test_token_is_signed_with_the_configured_asymmetric_algorithm() -> None:
    """A token that silently downgraded to HS256 would be forgeable by any
    service holding the (public) verification key."""
    header = jwt.get_unverified_header(create_access_token(USER_ID, "CITIZEN"))

    assert header["alg"] == "RS256"
    assert settings.JWT_ALGORITHM == "RS256"


def test_jti_is_unique_per_token() -> None:
    jtis = {decode_jwt(create_access_token(USER_ID, "CITIZEN"))["jti"] for _ in range(10)}

    assert len(jtis) == 10


def test_expiry_honours_the_configured_lifetime() -> None:
    before = datetime.now(UTC)
    claims = decode_jwt(create_access_token(USER_ID, "CITIZEN"))
    expected = before + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    assert abs(claims["exp"] - expected.timestamp()) < 5


# ── Access tokens: rejection paths ──────────────────────────────────────


def test_expired_token_is_rejected() -> None:
    """`exp` crafted in the past — the token is otherwise perfectly signed."""
    past = datetime.now(UTC) - timedelta(hours=1)
    token = _sign({"sub": USER_ID, "role": "CITIZEN", "iat": past - timedelta(hours=2), "exp": past})

    with pytest.raises(UnauthorizedError) as exc_info:
        decode_jwt(token)

    assert exc_info.value.status_code == 401


def test_token_issued_with_a_negative_lifetime_is_already_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same property, but exercised through `create_access_token` itself."""
    monkeypatch.setattr(settings, "JWT_ACCESS_TOKEN_EXPIRE_MINUTES", -5)
    token = create_access_token(USER_ID, "ADMIN")

    with pytest.raises(UnauthorizedError):
        decode_jwt(token)


def test_token_signed_by_a_different_key_is_rejected() -> None:
    """The central asymmetric guarantee: only the holder of *our* private key
    can mint tokens this API accepts."""
    foreign = _sign(
        {"sub": USER_ID, "role": "ADMIN", "exp": datetime.now(UTC) + timedelta(hours=1)},
        key=_foreign_private_key_pem(),
    )

    with pytest.raises(UnauthorizedError):
        decode_jwt(foreign)


def test_tampered_payload_is_rejected() -> None:
    """Privilege escalation attempt: CITIZEN rewritten to ADMIN in-place."""
    token = create_access_token(USER_ID, "CITIZEN")
    assert decode_jwt(token)["role"] == "CITIZEN"

    forged = _tamper_with_payload(token, role="ADMIN")

    with pytest.raises(UnauthorizedError):
        decode_jwt(forged)


def test_tampered_subject_is_rejected() -> None:
    """Account takeover attempt: `sub` swapped for another user's id."""
    forged = _tamper_with_payload(
        create_access_token(USER_ID, "CITIZEN"),
        sub="22222222-2222-2222-2222-222222222222",
    )

    with pytest.raises(UnauthorizedError):
        decode_jwt(forged)


def test_token_without_subject_claim_is_rejected() -> None:
    """A validly signed token is still useless without `sub`: every downstream
    lookup keys off it, so a missing subject must fail closed."""
    token = _sign({"role": "ADMIN", "exp": datetime.now(UTC) + timedelta(hours=1)})

    with pytest.raises(UnauthorizedError) as exc_info:
        decode_jwt(token)

    assert "subject" in exc_info.value.error_message


def test_token_signed_with_hs256_over_the_public_key_is_rejected() -> None:
    """Classic algorithm-confusion attack: the attacker knows the public key
    (it is public) and uses it as an HMAC secret, hoping the verifier trusts
    the `alg` header. Assembled by hand because python-jose refuses to *sign*
    this way — the attacker's tooling has no such scruples.
    """
    exp = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64url_encode(json.dumps({"sub": USER_ID, "role": "ADMIN", "exp": exp}, separators=(",", ":")).encode())
    signature = _b64url_encode(
        hmac.new(
            settings.JWT_PUBLIC_KEY.encode(),
            f"{header}.{payload}".encode(),
            hashlib.sha256,
        ).digest()
    )

    with pytest.raises(UnauthorizedError):
        decode_jwt(f"{header}.{payload}.{signature}")


@pytest.mark.parametrize(
    "garbage",
    ["", "not-a-token", "a.b.c", "Bearer eyJhbGciOiJSUzI1NiJ9.e30.sig", "eyJhbGciOiJSUzI1NiJ9..", "null"],
)
def test_malformed_tokens_are_rejected(garbage: str) -> None:
    with pytest.raises(UnauthorizedError):
        decode_jwt(garbage)


def test_rejection_is_unauthorized_error_with_the_contracted_shape() -> None:
    """Callers (and the global exception handler) depend on 401 + UNAUTHORIZED."""
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_jwt("not-a-token")

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "UNAUTHORIZED"
    assert exc_info.value.detail["error"]["code"] == "UNAUTHORIZED"


def test_decode_does_not_leak_the_private_key_in_the_error_message() -> None:
    with pytest.raises(UnauthorizedError) as exc_info:
        decode_jwt("not-a-token")

    assert "PRIVATE KEY" not in exc_info.value.error_message


# ── Refresh tokens ──────────────────────────────────────────────────────


def test_create_refresh_token_returns_raw_plus_its_sha256() -> None:
    raw, token_hash = create_refresh_token()

    assert raw != token_hash
    assert token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert token_hash == hash_refresh_token(raw)
    assert len(token_hash) == 64


def test_refresh_token_carries_real_entropy() -> None:
    """`secrets.token_urlsafe(64)` — 64 random bytes, ~86 URL-safe chars."""
    raw, _ = create_refresh_token()

    assert len(raw) >= 80
    assert raw.strip("-_") != ""


def test_successive_refresh_tokens_are_distinct() -> None:
    """Two users (or two logins) must never share a refresh token or its hash."""
    pairs = [create_refresh_token() for _ in range(25)]

    assert len({raw for raw, _ in pairs}) == 25
    assert len({token_hash for _, token_hash in pairs}) == 25


def test_hash_refresh_token_is_deterministic() -> None:
    """DB lookup by hash only works if the same raw token always hashes alike."""
    raw, _ = create_refresh_token()

    assert hash_refresh_token(raw) == hash_refresh_token(raw)


def test_hash_refresh_token_separates_near_identical_tokens() -> None:
    raw, token_hash = create_refresh_token()

    assert hash_refresh_token(raw + "x") != token_hash
    assert hash_refresh_token(raw[:-1]) != token_hash
