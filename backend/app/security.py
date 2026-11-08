"""Protection for endpoints that spend LLM tokens: optional access token + per-client rate limit."""
from __future__ import annotations

import hmac
import logging
import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional

from fastapi import Request

from .exceptions import DomainError
from .sessions import InvalidSession, SessionManager


log = logging.getLogger(__name__)


class UnauthorizedError(DomainError):
    status_code = 401

    def __init__(self, detail: str = "Missing or invalid X-API-Key."):
        super().__init__(detail)


class ForbiddenError(DomainError):
    status_code = 403

    def __init__(self, detail: str):
        super().__init__(detail)


class RateLimitedError(DomainError):
    status_code = 429

    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__(f"Too many AI requests. Try again in {retry_after}s.")


class RateLimiter:
    """Sliding window: at most `limit` calls per client per `window` seconds, and 4x that across all clients."""

    def __init__(self, limit: int, window: float = 60.0):
        self.limit, self.window = limit, window
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._all: Deque[float] = deque()
        self._lock = threading.Lock()

    def check(self, key: str, now: Optional[float] = None, count_global: bool = True) -> None:
        if self.limit <= 0:
            return
        now = time.monotonic() if now is None else now
        with self._lock:
            for q in (self._hits[key], self._all):
                while q and q[0] <= now - self.window:
                    q.popleft()
            mine = self._hits[key]
            if len(mine) >= self.limit:
                log.warning("rate limit hit", extra={"client": key, "limit": self.limit, "scope": "client"})
                raise RateLimitedError(max(1, int(mine[0] + self.window - now) + 1))
            if count_global and len(self._all) >= self.limit * 4:
                log.warning("rate limit hit", extra={"client": key, "limit": self.limit * 4, "scope": "global"})
                raise RateLimitedError(max(1, int(self._all[0] + self.window - now) + 1))
            mine.append(now)
            if count_global:
                self._all.append(now)


def client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")  # set by ngrok/proxies; the first entry is the caller
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def check_token(request: Request, expected: Optional[str]) -> None:
    if not expected:
        return
    got = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(got.encode(), expected.encode()):
        log.warning("bad or missing X-API-Key", extra={"client": client_key(request), "key_sent": bool(got)})
        raise UnauthorizedError()


def _api_key_ok(request: Request, expected: Optional[str]) -> Optional[bool]:
    """None = no key sent, True/False = sent and right/wrong."""
    got = request.headers.get("x-api-key")
    if got is None:
        return None
    return bool(expected) and hmac.compare_digest(got.encode(), expected.encode())


def authenticate(request: Request, sessions: SessionManager, access_token: Optional[str],
                 require_session: bool) -> Optional[str]:
    """Who is calling an AI endpoint? Returns an id for rate limiting ("s:<sid>" or "key"), or None for anonymous.

    - `Authorization: Bearer <session>` valid -> that session. Present but bad/expired -> 401 (client should re-mint).
    - `X-API-Key` matching API_ACCESS_TOKEN -> trusted server-to-server caller.
    - Neither: allowed only when auth is not enforced (local development).
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            return "s:" + sessions.verify(auth[7:].strip()).sid
        except InvalidSession as e:
            log.warning("session rejected", extra={"reason": e.reason, "client": client_key(request)})
            raise UnauthorizedError("Session expired. POST /api/v1/session for a new one." if e.reason == "expired"
                                    else "Invalid session token.") from None
    key = _api_key_ok(request, access_token)
    if key:
        return "key"
    if key is False:
        log.warning("bad X-API-Key", extra={"client": client_key(request)})
        raise UnauthorizedError()
    if require_session or access_token:
        log.warning("auth missing", extra={"client": client_key(request)})
        raise UnauthorizedError("Session required. POST /api/v1/session first, then send "
                                "'Authorization: Bearer <token>'.")
    return None


def require_admin(request: Request, access_token: Optional[str], require_session: bool) -> None:
    """Admin-only endpoints (e.g. /usage): need the X-API-Key. A browser session is NOT enough, since anyone can mint one."""
    if access_token:
        if not _api_key_ok(request, access_token):
            log.warning("admin access denied", extra={"client": client_key(request)})
            raise UnauthorizedError()
    elif require_session:
        raise ForbiddenError("Admin endpoints are disabled until API_ACCESS_TOKEN is set.")
