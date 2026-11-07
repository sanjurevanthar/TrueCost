"""Composition root: the one place that decides which concrete classes are wired together."""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .domain.geo import GeoService
from .domain.rate_selector import PlanAwareMedianSelector
from .repositories.base import PriceRepository
from .repositories.sqlite_repo import SqlitePriceRepository
from .services.estimate_service import EstimateService
from .services.extraction import CodeExtractor, KeywordCodeExtractor


@dataclass
class Container:
    repo: PriceRepository
    estimator: EstimateService
    extractor: CodeExtractor

    @classmethod
    def build(cls, settings: Settings) -> "Container":
        repo = SqlitePriceRepository(settings.db_path)
        return cls.from_repo(repo)

    @classmethod
    def from_repo(cls, repo: PriceRepository, extractor: CodeExtractor | None = None) -> "Container":
        return cls(
            repo=repo,
            estimator=EstimateService(repo, PlanAwareMedianSelector(), GeoService()),
            extractor=extractor or KeywordCodeExtractor(repo),  # swap in the LLM extractor here
        )
