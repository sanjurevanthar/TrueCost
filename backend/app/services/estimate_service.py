"""EstimateService: the use-case that ranks hospitals by what the patient would pay.

Collaborators are injected (repository, rate selector, geo) so each can be swapped or faked in tests.
Money math is delegated to a CostCalculator chosen per request (insured vs self-pay).
"""
from __future__ import annotations

from collections import defaultdict
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
        best = min(complete, key=lambda h: h.total_patient_pays) if complete else None
        return EstimateResponse(
            mode=req.mode,
            payer=profile.payer if profile else None,
            codes=req.codes,
            hospitals=hospitals,
            best_hospital=best.hospital if best else None,
            max_savings=max((h.savings_vs_most_expensive or 0 for h in complete), default=None),
            disclaimers=DISCLAIMERS,
        )

    # ------------------------------------------------------------------ setup helpers
    def _load_codes(self, codes: Sequence[str]) -> Dict[str, MedicalCode]:
        known = {c.code: c for c in self._repo.get_codes(codes)}
        missing = [c for c in codes if c not in known]
        if missing:
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
    def _rank(hospitals: List[HospitalEstimate]) -> None:
        complete = sorted((h for h in hospitals if h.complete),
                          key=lambda h: (h.total_patient_pays, h.total_allowed, h.hospital))
        if complete:
            worst = max(h.total_patient_pays for h in complete)
            for n, h in enumerate(complete, start=1):
                h.rank = n
                h.savings_vs_most_expensive = round(worst - h.total_patient_pays, 2)
        # Final order: ranked hospitals, then partially priced ones (most coverage first).
        hospitals.sort(key=lambda h: (h.rank is None, h.rank or 0, -int(h.coverage.split("/")[0]), h.hospital))
