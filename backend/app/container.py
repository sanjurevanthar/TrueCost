"""Composition root: the one place that decides which concrete classes are wired together."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .config import Settings
from .domain.geo import GeoService
from .domain.rate_selector import PlanAwareMedianSelector
from .repositories.base import PriceRepository
from .repositories.sqlite_repo import SqlitePriceRepository
from .usage import UsageTracker
from .services.estimate_service import EstimateService
from .services.extraction import CodeExtractor, KeywordCodeExtractor
from .services.transcription import GeminiNoteTranscriber, NoteTranscriber


def build_extractor(repo: PriceRepository, settings: Settings) -> CodeExtractor:
    """EXTRACTOR=keyword|claude|gemini|auto. auto = Gemini if GEMINI_API_KEY, else Claude if ANTHROPIC_API_KEY,
    else the keyword matcher. LLM extractors fall back to the keyword matcher on any failure."""
    mode = settings.extractor
    if mode == "auto":
        mode = "gemini" if os.getenv("GEMINI_API_KEY") else "claude" if os.getenv("ANTHROPIC_API_KEY") else "keyword"
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

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        repo = SqlitePriceRepository(settings.db_path)
        transcriber = GeminiNoteTranscriber(model=settings.gemini_model) if os.getenv("GEMINI_API_KEY") else None
        return cls.from_repo(repo, extractor=build_extractor(repo, settings), transcriber=transcriber)

    @classmethod
    def from_repo(cls, repo: PriceRepository, extractor: CodeExtractor | None = None,
                  transcriber: NoteTranscriber | None = None) -> "Container":
        return cls(
            repo=repo,
            estimator=EstimateService(repo, PlanAwareMedianSelector(), GeoService()),
            extractor=extractor or KeywordCodeExtractor(repo),  # swap in the LLM extractor here
            transcriber=transcriber,
        )
