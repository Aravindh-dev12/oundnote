from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from local_meeting_ai.api.cloud_auth import (
    SESSION_TTL_SECONDS,
    CloudAccessMiddleware,
    sign_session,
    valid_session,
)


def test_session_signature_is_valid_until_expiry() -> None:
    secret = "test-session-secret-that-is-at-least-32-chars"
    expires_at = 2_000_000_000
    token = sign_session(expires_at, secret)

    assert valid_session(token, secret, now=expires_at - SESSION_TTL_SECONDS)
    assert not valid_session(token, secret, now=expires_at)


def test_session_rejects_tampering_and_wrong_secret() -> None:
    secret = "test-session-secret-that-is-at-least-32-chars"
    expires_at = 2_000_000_000
    token = sign_session(expires_at, secret)

    assert not valid_session(token + "x", secret, now=1_999_999_000)
    assert not valid_session(
        token,
        "a-different-session-secret-that-is-long-enough",
        now=1_999_999_000,
    )


def test_session_rejects_unreasonably_long_lifetime() -> None:
    secret = "test-session-secret-that-is-at-least-32-chars"
    expires_at = 2_000_000_000

    assert not valid_session(
        sign_session(expires_at, secret),
        secret,
        now=expires_at - SESSION_TTL_SECONDS - 120,
    )


def test_cloud_password_gate_and_private_health_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("M2N_WEB_PASSWORD", "correct-horse-battery-staple")
    monkeypatch.setenv("M2N_WEB_SESSION_SECRET", "test-session-secret-that-is-at-least-32-chars")

    app = FastAPI()
    app.add_middleware(CloudAccessMiddleware)

    @app.get("/workspace")
    def workspace() -> dict[str, bool]:
        return {"private": True}

    @app.get("/api/private")
    def api_private() -> dict[str, bool]:
        return {"private": True}

    with TestClient(app, base_url="https://testserver") as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}

        protected = client.get("/workspace", follow_redirects=False)
        assert protected.status_code == 303
        assert protected.headers["location"] == "/login"

        api_unauthorized = client.get("/api/private", headers={"accept": "application/json"})
        assert api_unauthorized.status_code == 401

        login = client.post(
            "/login",
            data={"password": "correct-horse-battery-staple"},
            follow_redirects=False,
        )
        assert login.status_code == 303
        assert "oundnote_session" in login.headers.get("set-cookie", "")
        assert "httponly" in login.headers.get("set-cookie", "").lower()
        assert "secure" in login.headers.get("set-cookie", "").lower()

        authorized = client.get("/workspace")
        assert authorized.status_code == 200
        assert authorized.json() == {"private": True}
        assert authorized.headers["cache-control"] == "private, no-store"

        logout = client.post("/logout", follow_redirects=False)
        assert logout.status_code == 303


def test_cloud_password_requires_long_session_secret(monkeypatch) -> None:
    monkeypatch.setenv("M2N_WEB_PASSWORD", "workspace-password")
    monkeypatch.setenv("M2N_WEB_SESSION_SECRET", "too-short")
    app = FastAPI()
    try:
        CloudAccessMiddleware(app)
    except RuntimeError as exc:
        assert "at least 32 characters" in str(exc)
    else:
        raise AssertionError("Expected an invalid session secret to be rejected")
