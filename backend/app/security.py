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


log = logging.getLogger(__name__)


class UnauthorizedError(DomainError):
    status_code = 401

    def __init__(self):
        super().__init__("Missing or invalid X-API-Key.")


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

    def check(self, key: str, now: Optional[float] = None) -> None:
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
            if len(self._all) >= self.limit * 4:
                log.warning("rate limit hit", extra={"client": key, "limit": self.limit * 4, "scope": "global"})
                raise RateLimitedError(max(1, int(self._all[0] + self.window - now) + 1))
            mine.append(now)
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
