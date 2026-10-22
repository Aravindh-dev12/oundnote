from __future__ import annotations

import hashlib
import hmac
import os
import time

from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

SESSION_COOKIE = "oundnote_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_FAILED_LOGINS = 5


def sign_session(expires_at: int, secret: str) -> str:
    """Create a tamper-evident session token; it contains no meeting data."""
    payload = f"oundnote-session:{expires_at}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{expires_at}.{signature}"


def valid_session(token: str | None, secret: str, *, now: int | None = None) -> bool:
    """Validate signature and enforce a bounded, 12-hour session lifetime."""
    if not token:
        return False
    try:
        expiry_text, signature = token.split(".", maxsplit=1)
        expires_at = int(expiry_text)
    except (ValueError, TypeError):
        return False

    current_time = int(time.time()) if now is None else now
    if expires_at <= current_time or expires_at > current_time + SESSION_TTL_SECONDS + 60:
        return False
    expected = sign_session(expires_at, secret).split(".", maxsplit=1)[1]
    return hmac.compare_digest(signature, expected)


class CloudAccessMiddleware(BaseHTTPMiddleware):
    """Optional single-workspace password gate for internet-facing deployments.

    Local desktop installs remain unchanged when M2N_WEB_PASSWORD is unset.
    This is a private beta gate, not multi-user identity or tenant isolation.
    """

    def __init__(self, app: object) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.password = os.environ.get("M2N_WEB_PASSWORD", "")
        self.secret = os.environ.get("M2N_WEB_SESSION_SECRET", "")
        if self.password and len(self.secret) < 32:
            raise RuntimeError(
                "M2N_WEB_SESSION_SECRET must be at least 32 characters when "
                "M2N_WEB_PASSWORD is configured."
            )
        self.failed_logins: dict[str, list[float]] = {}

    async def dispatch(self, request: Request, call_next: object) -> Response:
        path = request.url.path
        if path == "/healthz" and request.method == "GET":
            return JSONResponse({"status": "ok"})
        if not self.password:
            return await call_next(request)  # type: ignore[operator]

        if path == "/login":
            if request.method == "GET":
                if valid_session(
                    request.cookies.get(SESSION_COOKIE), self.secret
                ):
                    return RedirectResponse("/", status_code=303)
                return self._login_page()
            if request.method == "POST":
                return await self._handle_login(request)
            return Response(status_code=405, headers={"Allow": "GET, POST"})

        if path == "/logout" and request.method == "POST":
            response = RedirectResponse("/login", status_code=303)
            response.delete_cookie(
                SESSION_COOKIE,
                path="/",
                secure=self._secure_request(request),
                httponly=True,
                samesite="strict",
            )
            response.headers["Cache-Control"] = "no-store"
            return response

        if not valid_session(request.cookies.get(SESSION_COOKIE), self.secret):
            if path == "/api" or path.startswith("/api/") or (
                "application/json" in request.headers.get("accept", "").lower()
            ):
                return JSONResponse(
                    {"detail": "Authentication required"},
                    status_code=401,
                    headers={"Cache-Control": "no-store", "WWW-Authenticate": "Session"},
                )
            return RedirectResponse("/login", status_code=303)

        response = await call_next(request)  # type: ignore[operator]
        # Transcripts, recordings and notes must not be stored by intermediary caches.
        response.headers["Cache-Control"] = "private, no-store"
        return response

    async def _handle_login(self, request: Request) -> Response:
        if "application/x-www-form-urlencoded" not in request.headers.get(
            "content-type", ""
        ).lower():
            return self._login_page("Submit the sign-in form to continue.", status_code=415)
        try:
            content_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return self._login_page("Invalid sign-in request.", status_code=400)
        if content_length > 8192:
            return self._login_page("Sign-in request is too large.", status_code=413)

        client_ip = request.client.host if request.client else "unknown"
        now = time.monotonic()
        recent = [
            attempt
            for attempt in self.failed_logins.get(client_ip, [])
            if now - attempt < LOGIN_WINDOW_SECONDS
        ]
        self.failed_logins[client_ip] = recent
        if len(recent) >= MAX_FAILED_LOGINS:
            return self._login_page(
                "Too many failed attempts. Try again in 15 minutes.",
                status_code=429,
            )

        try:
            form = await request.form()
            provided = form.get("password", "")
        except (ValueError, UnicodeDecodeError):
            return self._login_page("Invalid sign-in request.", status_code=400)

        if not isinstance(provided, str) or not hmac.compare_digest(
            provided.encode(), self.password.encode()
        ):
            recent.append(now)
            self.failed_logins[client_ip] = recent
            return self._login_page("Incorrect password.", status_code=401)

        self.failed_logins.pop(client_ip, None)
        expires_at = int(time.time()) + SESSION_TTL_SECONDS
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            sign_session(expires_at, self.secret),
            max_age=SESSION_TTL_SECONDS,
            path="/",
            secure=self._secure_request(request),
            httponly=True,
            samesite="strict",
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _secure_request(request: Request) -> bool:
        forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0]
        return request.url.scheme == "https" or forwarded_proto.strip().lower() == "https"

    @staticmethod
    def _login_page(message: str = "", *, status_code: int = 200) -> HTMLResponse:
        escaped = (
            message.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&#x27;")
        )
        notice = f'<p role="alert">{escaped}</p>' if escaped else ""
        html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Sign in · Oundnote</title>
</head>
<body>
<main>
<h1>Oundnote</h1>
<p>Sign in to your private meeting workspace.</p>
{notice}
<form method="post" action="/login">
<label for="password">Workspace password</label>
<input id="password" name="password" type="password" required
 autocomplete="current-password" autofocus>
<button type="submit">Sign in</button>
</form>
</main>
</body>
</html>"""
        return HTMLResponse(
            html,
            status_code=status_code,
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"},
        )
