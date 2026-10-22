from __future__ import annotations

from local_meeting_ai.api.cloud_auth import SESSION_TTL_SECONDS, sign_session, valid_session


def test_session_signature_is_valid_until_expiry() -> None:
    secret = "test-session-secret-that-is-at-least-32-chars"
    expires_at = 2_000_000_000
    token = sign_session(expires_at, secret)

    assert valid_session(token, secret, now=expires_at - SESSION_TTL_SECONDS)
    assert not valid_session(token, secret, now=expires_at)


def test_session_rejects_tampering_and_wrong_secret() -> None:
    expires_at = 2_000_000_000
    token = sign_session(expires_at, "test-session-secret-that-is-at-least-32-chars")

    assert not valid_session(token + "x", "test-session-secret-that-is-at-least-32-chars", now=1_999_999_000)
    assert not valid_session(token, "a-different-session-secret-that-is-long-enough", now=1_999_999_000)


def test_session_rejects_unreasonably_long_lifetime() -> None:
    secret = "test-session-secret-that-is-at-least-32-chars"
    expires_at = 2_000_000_000

    assert not valid_session(
        sign_session(expires_at, secret),
        secret,
        now=expires_at - SESSION_TTL_SECONDS - 120,
    )
