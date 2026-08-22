"""CSRF protection for the dashboard.

Signed double-submit cookie pattern. The middleware issues a `dotty_csrf`
cookie containing a random per-session token plus an HMAC signature.
Mutating requests under `/ui/` must echo the raw token back in the
`X-CSRF-Token` header (htmx does this via the configRequest listener in
dashboard.html, which reads the token from a `<meta name="csrf-token">`
tag). API endpoints under `/api/...`, `/metrics`, `/health`, and the exact
machine-ingest path `/admin/activity` are exempt — they have their own auth
model. Other `/admin/*` mutations remain protected.

Kill switch: set `DOTTY_CSRF_ENFORCE=0` to log-only mode (cookie still
issued, mismatches logged as `csrf would-block`, requests pass through).
Useful if the middleware misbehaves at 2 AM and the dashboard is locked
out — flip the env, restart the unit, fix forward with journal evidence.
"""
from __future__ import annotations

import hmac
import logging
import os
import secrets
from hashlib import sha256

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger("csrf")

COOKIE_NAME = "dotty_csrf"
HEADER_NAME = "X-CSRF-Token"
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_EXEMPT_PREFIXES = ("/api/", "/metrics", "/health", "/admin/activity")


def _load_secret() -> bytes:
    env = os.environ.get("DOTTY_CSRF_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    log.warning(
        "DOTTY_CSRF_SECRET not set — using ephemeral secret. "
        "CSRF tokens will not survive a bridge restart. "
        "Set DOTTY_CSRF_SECRET in /etc/default/zeroclaw-bridge for production."
    )
    return secrets.token_urlsafe(32).encode("utf-8")


_SECRET = _load_secret()
_ENFORCE = os.environ.get("DOTTY_CSRF_ENFORCE", "1") != "0"


def _sign(raw: str) -> str:
    sig = hmac.new(_SECRET, raw.encode("utf-8"), sha256).hexdigest()
    return f"{raw}.{sig}"


def _unsign(cookie_value: str) -> str | None:
    if not cookie_value or "." not in cookie_value:
        return None
    raw, _, sig = cookie_value.rpartition(".")
    if not raw or not sig:
        return None
    expected = hmac.new(_SECRET, raw.encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return raw


def _is_exempt(path: str) -> bool:
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


class CSRFMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        cookie_raw = _unsign(request.cookies.get(COOKIE_NAME, ""))
        issue_new = cookie_raw is None
        if issue_new:
            cookie_raw = secrets.token_urlsafe(32)
        request.state.csrf_token = cookie_raw

        if method in _MUTATING and not _is_exempt(path):
            header_token = request.headers.get(HEADER_NAME, "")
            ok = bool(header_token) and hmac.compare_digest(header_token, cookie_raw)
            if not ok:
                msg = f"csrf {'would-block' if not _ENFORCE else 'block'}: {method} {path}"
                log.warning(msg)
                if _ENFORCE:
                    resp = Response("csrf: missing or invalid token", status_code=403)
                    if issue_new:
                        _set_cookie(resp, cookie_raw)
                    return resp

        response = await call_next(request)
        if issue_new:
            _set_cookie(response, cookie_raw)
        return response


def _set_cookie(response: Response, raw: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        _sign(raw),
        httponly=False,  # JS reads it via the meta tag, not directly
        samesite="lax",
        secure=False,    # bridge serves plain HTTP on LAN
        max_age=60 * 60 * 24 * 7,
        path="/",
    )
