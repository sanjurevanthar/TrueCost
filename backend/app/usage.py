"""Process-wide LLM token counter (since the server started). Thread-safe; exposed at GET /api/v1/usage."""
from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Optional

from .services.extraction import TokenUsage


class UsageTracker:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._lock = threading.Lock()
        self._by_op: Dict[str, dict] = {}
        self._clock = clock
        self._day = self._today()
        self._tokens_today = 0

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _roll(self) -> None:           # call with the lock held; resets the daily counter at 00:00 UTC
        today = self._today()
        if today != self._day:
            self._day, self._tokens_today = today, 0

    def tokens_today(self) -> int:
        with self._lock:
            self._roll()
            return self._tokens_today

    def record(self, operation: str, usage: Optional[TokenUsage], fallback_used: bool = False) -> None:
        with self._lock:
            self._roll()
            d = self._by_op.setdefault(operation, {"calls": 0, "fallbacks": 0, "input_tokens": 0,
                                                   "output_tokens": 0, "total_tokens": 0})
            d["calls"] += 1
            d["fallbacks"] += int(fallback_used)
            if usage:
                d["input_tokens"] += usage.input_tokens
                d["output_tokens"] += usage.output_tokens
                d["total_tokens"] += usage.total_tokens
                self._tokens_today += usage.total_tokens

    def snapshot(self) -> dict:
        with self._lock:
            self._roll()
            ops = {k: dict(v) for k, v in self._by_op.items()}
        total = {f: sum(v[f] for v in ops.values()) for f in ("calls", "fallbacks", "input_tokens", "output_tokens", "total_tokens")}
        return {"total": total, "by_operation": ops, "today_utc": {"date": self._day, "tokens": self._tokens_today}}
