"""Rate selection (Strategy pattern).

The price table can hold several plans / rates for one (hospital, code, payer). A RateSelector
turns that list into ONE rate, and reports the low-high spread so the UI can show a range.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from statistics import median
from typing import Optional, Sequence

from .calculators import money
from .models import PriceRecord, SelectedRate


class RateSelector(ABC):
    @abstractmethod
    def select(self, candidates: Sequence[PriceRecord], plan_hint: Optional[str] = None) -> Optional[SelectedRate]:
        ...


class PlanAwareMedianSelector(RateSelector):
    """If the patient named a plan, use matching plans; otherwise (or if nothing matches) use all.
    The chosen rate is the median, so one odd plan can't skew the estimate."""

    def select(self, candidates: Sequence[PriceRecord], plan_hint: Optional[str] = None) -> Optional[SelectedRate]:
        priced = [c for c in candidates if c.is_dollar_rate]
        if not priced:
            return None

        pool, matched = priced, False
        hint = (plan_hint or "").strip().lower()
        if hint:
            hits = [c for c in priced if c.plan and hint in c.plan.lower()]
            if hits:
                pool, matched = hits, True

        rates = [c.negotiated_rate for c in pool]
        mid = money(median(rates))
        closest = min(pool, key=lambda c: abs(c.negotiated_rate - mid))
        return SelectedRate(record=closest, rate=mid, low=min(rates), high=max(rates),
                            candidates=len(pool), plan_matched=matched)
