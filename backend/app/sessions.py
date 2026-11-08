"""Anonymous, short-lived session tokens (no accounts, nothing stored).

POST /api/v1/session issues `payload.signature` (HMAC-SHA256). The server keeps no state: it just re-computes the
signature. A leaked token stops working when it expires, unlike a static API key baked into a web page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

MAX_TOKEN_CHARS = 512


class InvalidSession(Exception):
    def __init__(self, reason: str):
        self.reason = reason            # malformed | bad_signature | expired
        super().__init__(reason)


@dataclass(frozen=True)
class Session:
    sid: str
    issued_at: int
    expires_at: int


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SessionManager:
    def __init__(self, secret: Optional[str] = None, ttl_seconds: int = 3600, clock: Callable[[], float] = time.time):
        self.ephemeral_secret = not secret      # True -> every restart invalidates all sessions
        self._key = (secret or secrets.token_urlsafe(48)).encode()
        self.ttl = ttl_seconds
        self._clock = clock

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode(), hashlib.sha256).digest())

    def issue(self) -> Tuple[str, Session]:
        now = int(self._clock())
        s = Session(sid=secrets.token_hex(8), issued_at=now, expires_at=now + self.ttl)
        payload = _b64(json.dumps({"sid": s.sid, "iat": s.issued_at, "exp": s.expires_at}, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}", s

    def verify(self, token: str) -> Session:
        if not token or len(token) > MAX_TOKEN_CHARS or token.count(".") != 1:
            raise InvalidSession("malformed")
        payload, sig = token.split(".")
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise InvalidSession("bad_signature")
        try:
            d = json.loads(_unb64(payload))
            s = Session(str(d["sid"]), int(d["iat"]), int(d["exp"]))
        except (ValueError, KeyError, TypeError):
            raise InvalidSession("malformed") from None
        if self._clock() >= s.expires_at:
            raise InvalidSession("expired")
        return s
