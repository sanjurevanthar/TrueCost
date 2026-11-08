"""Composition root: the one place that decides which concrete classes are wired together."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .config import Settings
from .domain.geo import GeoService
from .domain.rate_selector import PlanAwareMedianSelector
from .repositories.base import PriceRepository
from .repositories.sqlite_repo import SqlitePriceRepository
from .security import RateLimiter
from .sessions import SessionManager
from .usage import UsageTracker
from .services.estimate_service import EstimateService
from .services.extraction import CodeExtractor, KeywordCodeExtractor
from .services.transcription import GeminiNoteTranscriber, NoteTranscriber


log = logging.getLogger(__name__)


def build_extractor(repo: PriceRepository, settings: Settings) -> CodeExtractor:
    """EXTRACTOR=keyword|claude|gemini|auto. auto = Gemini if GEMINI_API_KEY, else Claude if ANTHROPIC_API_KEY,
    else the keyword matcher. LLM extractors fall back to the keyword matcher on any failure."""
    mode = settings.extractor
    if mode == "auto":
        mode = "gemini" if os.getenv("GEMINI_API_KEY") else "claude" if os.getenv("ANTHROPIC_API_KEY") else "keyword"
    log.info("extractor selected", extra={"requested": settings.extractor, "using": mode})
    if mode == "gemini":
        from .services.gemini_extractor import GeminiCodeExtractor
        return GeminiCodeExtractor(repo, model=settings.gemini_model)
    if mode == "claude":
        from .services.claude_extractor import ClaudeCodeExtractor
        return ClaudeCodeExtractor(repo, model=settings.claude_model)
    return KeywordCodeExtractor(repo)


@dataclass
class Container:
    repo: PriceRepository
    estimator: EstimateService
    extractor: CodeExtractor
    transcriber: NoteTranscriber | None = None  # None = image upload disabled (no Gemini key)
    usage: UsageTracker = field(default_factory=UsageTracker)
    limiter: RateLimiter = field(default_factory=lambda: RateLimiter(0))  # 0 = off (tests); build() turns it on
    access_token: str | None = None
    sessions: SessionManager = field(default_factory=SessionManager)
    session_limiter: RateLimiter = field(default_factory=lambda: RateLimiter(0))  # throttles minting new sessions
    require_session: bool = False
    daily_token_budget: int = 0          # 0 = unlimited
    fallback_extractor: CodeExtractor | None = None  # used when the daily budget is spent

    def budget_exceeded(self) -> bool:
        return self.daily_token_budget > 0 and self.usage.tokens_today() >= self.daily_token_budget

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        repo = SqlitePriceRepository(settings.db_path)
        transcriber = GeminiNoteTranscriber(model=settings.gemini_model) if os.getenv("GEMINI_API_KEY") else None
        c = cls.from_repo(repo, extractor=build_extractor(repo, settings), transcriber=transcriber)
        c.limiter = RateLimiter(settings.ai_rate_limit_per_min)
        c.access_token = settings.api_access_token
        c.require_session = settings.require_session
        c.sessions = SessionManager(settings.session_secret, settings.session_ttl_min * 60)
        c.session_limiter = RateLimiter(settings.session_rate_limit_per_min)
        c.daily_token_budget = settings.daily_token_budget
        if c.sessions.ephemeral_secret and settings.require_session:
            log.warning("SESSION_SECRET is not set: sessions reset on every restart. Set it in .env.")
        return c

    @classmethod
    def from_repo(cls, repo: PriceRepository, extractor: CodeExtractor | None = None,
                  transcriber: NoteTranscriber | None = None) -> "Container":
        return cls(
            repo=repo,
            estimator=EstimateService(repo, PlanAwareMedianSelector(), GeoService()),
            extractor=extractor or KeywordCodeExtractor(repo),  # swap in the LLM extractor here
            transcriber=transcriber,
            fallback_extractor=KeywordCodeExtractor(repo),
        )
