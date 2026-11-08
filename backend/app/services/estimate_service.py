"""EstimateService: the use-case that ranks hospitals by what the patient would pay.

Collaborators are injected (repository, rate selector, geo) so each can be swapped or faked in tests.
Money math is delegated to a CostCalculator chosen per request (insured vs self-pay).
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from ..domain.calculators import CostCalculator, InsuredCostCalculator, SelfPayCostCalculator, money
from ..domain.geo import GeoService
from ..domain.models import CoverageMode, Hospital, InsuranceProfile, MedicalCode, PriceRecord, SelectedRate
from ..domain.rate_selector import RateSelector
from ..exceptions import MissingInsuranceError, UnknownCodeError
from ..repositories.base import PriceRepository
from ..schemas import EstimateRequest, EstimateResponse, HospitalEstimate, LineEstimate

log = logging.getLogger(__name__)

DISCLAIMERS = [
    "Estimates only - not a bill or a guarantee of coverage.",
    "Facility fees only. Doctor, radiologist and pathologist fees are billed separately.",
    "Based on hospital price-transparency files; rates can change and may contain errors.",
    "Assumes a coinsurance-based plan. Flat copays are not modelled.",
    "Inpatient stays are priced by formula - contact the hospital for an estimate.",
]


HIGH_VS_CHARGE = Decimal("2")     # negotiated rate more than 2x the hospital's own listed charge
LOW_CASH_VS_CHARGE = Decimal("0.25")  # cash price under 25% of the listed charge


def _gross_of(recs: List[PriceRecord]) -> Optional[Decimal]:
    return next((r.gross_charge for r in recs if r.gross_charge), None)


@dataclass
class _PricedLine:
    """A code the hospital could price: the amount the plan allows, and how we got it."""
    code: str
    allowed: Decimal
    basis: str  # negotiated | cash | gross
    selected: Optional[SelectedRate] = None
    warning: Optional[str] = None


class EstimateService:
    def __init__(self, repo: PriceRepository, selector: RateSelector, geo: GeoService):
        self._repo = repo
        self._selector = selector
        self._geo = geo

    # ------------------------------------------------------------------ public
    def estimate(self, req: EstimateRequest) -> EstimateResponse:
        t0 = time.perf_counter()
        known = self._load_codes(req.codes)
        profile, payer_name = self._profile(req)
        calculator = self._calculator_for(req.mode, profile)

        records = self._repo.find_prices(req.codes, payer_name)
        index: Dict[Tuple[str, str], List[PriceRecord]] = defaultdict(list)
        for r in records:
            index[(r.hospital, r.code)].append(r)

        hospitals = [self._estimate_hospital(h, req, known, index, profile, calculator)
                     for h in self._repo.list_hospitals()]
        self._rank(hospitals)

        complete = [h for h in hospitals if h.complete]
        best = min(complete, key=lambda h: (h.rank or 0)) if complete else None
        best_group = [h for h in complete if h.rank == 1]
        warned = sum(1 for h in hospitals for ln in h.lines if ln.warning)
        log.info("estimate", extra={
            "mode": req.mode.value, "payer": profile.payer.value if profile else None, "codes": req.codes,
            "hospitals": len(hospitals), "fully_priced": len(complete), "best": best.hospital if best else None,
            "price_warnings": warned, "ms": round((time.perf_counter() - t0) * 1000, 1)})
        if not complete:
            log.warning("no hospital could price every code", extra={"codes": req.codes, "mode": req.mode.value})
        return EstimateResponse(
            mode=req.mode,
            payer=profile.payer if profile else None,
            codes=req.codes,
            hospitals=hospitals,
            best_hospital=best.hospital if best else None,
            best_hospitals=[h.hospital for h in best_group],
            notice=self._tie_notice(best_group, req.mode),
            max_savings=max((h.savings_vs_most_expensive or 0 for h in complete), default=None),
            disclaimers=DISCLAIMERS,
        )

    # ------------------------------------------------------------------ setup helpers
    def _load_codes(self, codes: Sequence[str]) -> Dict[str, MedicalCode]:
        known = {c.code: c for c in self._repo.get_codes(codes)}
        missing = [c for c in codes if c not in known]
        if missing:
            log.warning("unknown codes requested", extra={"codes": missing})
            raise UnknownCodeError(missing)
        return known

    @staticmethod
    def _profile(req: EstimateRequest) -> Tuple[Optional[InsuranceProfile], Optional[str]]:
        if req.mode == CoverageMode.SELF_PAY:
            return None, None
        if req.insurance is None:
            raise MissingInsuranceError()
        i = req.insurance
        profile = InsuranceProfile(
            payer=i.payer, plan=i.plan,
            deductible_remaining=Decimal(str(i.deductible_remaining)),
            coinsurance_pct=Decimal(str(i.coinsurance_pct)),
            oop_max_remaining=Decimal(str(i.oop_max_remaining)))
        return profile, i.payer.value

    @staticmethod
    def _calculator_for(mode: CoverageMode, profile: Optional[InsuranceProfile]) -> CostCalculator:
        return InsuredCostCalculator(profile) if mode == CoverageMode.INSURED and profile else SelfPayCostCalculator()

    # ------------------------------------------------------------------ per hospital
    def _estimate_hospital(self, hospital: Hospital, req: EstimateRequest, known: Dict[str, MedicalCode],
                           index: Dict[Tuple[str, str], List[PriceRecord]],
                           profile: Optional[InsuranceProfile], calculator: CostCalculator) -> HospitalEstimate:
        priced: List[_PricedLine] = []
        unavailable: Dict[str, str] = {}
        for code in req.codes:
            recs = index.get((hospital.name, code), [])
            line, reason = self._price_code(known[code], recs, profile)
            if line:
                priced.append(line)
            else:
                unavailable[code] = reason

        for code, why in unavailable.items():
            log.debug("unpriced", extra={"hospital": hospital.name, "code": code, "reason": why})
        costs = calculator.calculate([p.allowed for p in priced])
        cost_by_code = {p.code: (p, c) for p, c in zip(priced, costs)}

        lines: List[LineEstimate] = []
        for code in req.codes:
            desc = known[code].description
            if code in cost_by_code:
                p, c = cost_by_code[code]
                sel = p.selected
                lines.append(LineEstimate(
                    code=code, description=desc, status="priced", price_basis=p.basis,
                    allowed_amount=float(c.allowed),
                    rate_low=float(sel.low) if sel else None, rate_high=float(sel.high) if sel else None,
                    plan_used=sel.record.plan if sel else None, plan_matched=sel.plan_matched if sel else None,
                    deductible_paid=float(c.deductible_paid), coinsurance_paid=float(c.coinsurance_paid),
                    patient_pays=float(c.patient_pays), insurer_pays=float(c.insurer_pays),
                    hit_oop_max=c.hit_oop_max, warning=p.warning))
            else:
                lines.append(LineEstimate(code=code, description=desc, status="unavailable",
                                          reason=unavailable[code]))

        return HospitalEstimate(
            hospital=hospital.name, address=hospital.address, lat=hospital.lat, lng=hospital.lng,
            distance_miles=self._geo.distance_miles(req.patient_lat, req.patient_lng, hospital.lat, hospital.lng),
            complete=len(priced) == len(req.codes), coverage=f"{len(priced)}/{len(req.codes)}",
            total_allowed=float(sum((c.allowed for c in costs), Decimal("0"))),
            total_patient_pays=float(sum((c.patient_pays for c in costs), Decimal("0"))),
            lines=lines)

    def _price_code(self, code: MedicalCode, recs: List[PriceRecord],
                    profile: Optional[InsuranceProfile]) -> Tuple[Optional[_PricedLine], str]:
        if code.code_type == "MS-DRG":
            return None, "Inpatient stay is priced by formula - contact the hospital for an estimate."
        if profile is None:
            return self._price_self_pay(code, recs)
        return self._price_insured(code, recs, profile)

    def _price_insured(self, code: MedicalCode, recs: List[PriceRecord],
                       profile: InsuranceProfile) -> Tuple[Optional[_PricedLine], str]:
        payer_recs = [r for r in recs if r.payer == profile.payer.value]
        selected = self._selector.select(payer_recs, profile.plan)
        if selected:
            gross = _gross_of(recs)
            warn = None
            if gross and selected.rate > HIGH_VS_CHARGE * gross:
                warn = ("This rate is much higher than the hospital's own listed charge, so it may be a bundled or "
                        "case rate. Please confirm the price with the hospital.")
            return _PricedLine(code.code, selected.rate, "negotiated", selected, warn), ""
        if payer_recs:
            return None, f"{profile.payer.value} rate is formula-based (percent of charges), not a dollar amount."
        if recs:
            return None, f"No {profile.payer.value} rate published - this hospital lists a gross charge only."
        return None, "Not found in this hospital's price file."

    @staticmethod
    def _price_self_pay(code: MedicalCode, recs: List[PriceRecord]) -> Tuple[Optional[_PricedLine], str]:
        cash = next((r.cash_price for r in recs if r.cash_price is not None), None)
        if cash is not None:
            gross = _gross_of(recs)
            warn = None
            if gross and cash < LOW_CASH_VS_CHARGE * gross:
                warn = ("This self-pay price is far below the hospital's listed charge. Please confirm it with the "
                        "hospital before relying on it.")
            return _PricedLine(code.code, money(cash), "cash", None, warn), ""
        gross = next((r.gross_charge for r in recs if r.gross_charge is not None), None)
        if gross is not None:
            return _PricedLine(code.code, money(gross), "gross"), ""
        return None, "Not found in this hospital's price file."

    # ------------------------------------------------------------------ ranking
    @staticmethod
    def _tie_notice(best_group: List[HospitalEstimate], mode: CoverageMode) -> Optional[str]:
        """Say WHY hospitals tie: identical prices (e.g. one health system's shared price list), or the patient's
        deductible / out-of-pocket maximum deciding the cost."""
        if len(best_group) < 2:
            return None
        n, cost = len(best_group), f"${best_group[0].total_patient_pays:,.0f}"
        if len({round(h.total_allowed, 2) for h in best_group}) == 1:
            return f"{n} hospitals list identical prices for these services, so each would cost you {cost}."
        return (f"You'd pay the same ({cost}) at {n} hospitals. With your deductible and out-of-pocket maximum, "
                f"the price differences don't change what you pay - they change what your insurer pays.")

    @staticmethod
    def _rank(hospitals: List[HospitalEstimate]) -> None:
        # Rank by what the PATIENT pays. Equal cost = equal rank (1, 1, 3). total_allowed only orders the display.
        complete = sorted((h for h in hospitals if h.complete),
                          key=lambda h: (round(h.total_patient_pays, 2), h.total_allowed, h.hospital))
        if complete:
            worst = max(h.total_patient_pays for h in complete)
            rank, prev = 0, None
            for n, h in enumerate(complete, start=1):
                cost = round(h.total_patient_pays, 2)
                if cost != prev:
                    rank, prev = n, cost
                h.rank = rank
                h.savings_vs_most_expensive = round(worst - h.total_patient_pays, 2)
            sizes = Counter(h.rank for h in complete)
            for h in complete:
                h.tied = sizes[h.rank] > 1
        # Final order: ranked hospitals, then partially priced ones (most coverage first).
        hospitals.sort(key=lambda h: (h.rank is None, h.rank or 0, -int(h.coverage.split("/")[0]), h.hospital))
