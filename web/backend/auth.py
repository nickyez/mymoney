from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from threading import Lock

from argon2 import PasswordHasher
from fastapi import HTTPException, Request, Response

COOKIE = "mymoney_session"
SESSION_TTL = 60 * 60 * 24 * 7
_ph = PasswordHasher()
_sessions: dict[str, tuple[float, str]] = {}
_attempts: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def _secret() -> bytes:
    value = os.environ.get("MYMONEY_SESSION_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("MYMONEY_SESSION_SECRET must be set to at least 32 characters")
    return value.encode()


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return _ph.verify(encoded, password)
    except Exception:
        return False


def _rate_limit(ip: str) -> None:
    now = time.time()
    with _lock:
        q = _attempts[ip]
        while q and q[0] < now - 600:
            q.popleft()
        if len(q) >= 8:
            raise HTTPException(429, "Too many login attempts; wait 10 minutes")
        q.append(now)


def login(password: str, stored_hash: str, ip: str, response: Response) -> None:
    _rate_limit(ip)
    if not verify_password(password, stored_hash):
        # Constant-time dummy compare reduces username/password timing clues.
        hmac.compare_digest(hashlib.sha256(password.encode()).digest(), b"\0" * 32)
        raise HTTPException(401, "Invalid password")
    token = secrets.token_urlsafe(48)
    expiry = time.time() + SESSION_TTL
    with _lock:
        _sessions[token] = (expiry, "authenticated")
    forwarded_proto = response.headers.get("x-forwarded-proto", "")
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True,
                        secure=True, samesite="lax", path="/")


def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(COOKIE)
    if token:
        with _lock:
            _sessions.pop(token, None)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")


def require_auth(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Authentication required")
    with _lock:
        session = _sessions.get(token)
        if not session or session[0] < time.time():
            _sessions.pop(token, None)
            raise HTTPException(401, "Session expired")


def check_csrf(request: Request) -> None:
    # Same-origin web app; reject cross-site state changes based on browser metadata.
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        site = request.headers.get("sec-fetch-site")
        if site and site not in {"same-origin", "none"}:
            raise HTTPException(403, "Cross-site request blocked")
        origin = request.headers.get("origin")
        if origin:
            expected = request.headers.get("x-forwarded-host") or request.headers.get("host")
            if expected and origin.split("://", 1)[-1].split("/", 1)[0] != expected:
                raise HTTPException(403, "Invalid request origin")
