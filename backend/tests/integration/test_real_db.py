"""Integration tests: the real FastAPI app + the real truecost.db (no fakes).

Skipped automatically when truecost.db is not present (e.g. on a fresh clone, since it is gitignored).
Run:  cd backend && ../.venv/bin/python -m pytest tests/integration -q
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.main import create_app

DB = Path(PROJECT_ROOT) / "truecost.db"
pytestmark = pytest.mark.skipif(not DB.exists(), reason="truecost.db not built")

INSURERS = ["Independent Health", "Highmark BCBS", "Univera"]
DIAGNOSTIC = ["77066", "76642", "19083"]
EXPECTED_HOSPITALS = {"Roswell Park", "ECMC", "Mercy Hospital of Buffalo", "Kenmore Mercy Hospital",
                      "Mount St. Mary's Hospital", "Buffalo General Medical Center",
                      "Millard Fillmore Suburban Hospital"}


@pytest.fixture(scope="module")
def api():
    return TestClient(create_app())


def ins(payer="Independent Health", ded=500, pct=20, oop=3000, plan=None):
    return {"payer": payer, "plan": plan, "deductible_remaining": ded, "coinsurance_pct": pct,
            "oop_max_remaining": oop}


def estimate(api, codes=DIAGNOSTIC, mode="insured", **kw):
    body = {"codes": codes, "mode": mode, **kw}
    if mode == "insured" and "insurance" not in body:
        body["insurance"] = ins()
    r = api.post("/api/v1/estimate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- reference data
def test_health(api):
    assert api.get("/health").json() == {"status": "ok"}


def test_all_seven_hospitals_with_coordinates(api):
    hs = api.get("/api/v1/hospitals").json()
    assert {h["hospital"] for h in hs} == EXPECTED_HOSPITALS
    for h in hs:  # Western NY bounding box
        assert 42.5 < h["lat"] < 43.5 and -79.5 < h["lng"] < -78.4, h
        assert h["address"]


def test_codes_catalog(api):
    codes = api.get("/api/v1/codes").json()
    by_phase = {p: [c for c in codes if c["phase"] == p] for p in ("diagnostic", "treatment", "diagnosis")}
    assert len(by_phase["diagnostic"]) == 11 and len(by_phase["treatment"]) == 7
    assert len(by_phase["diagnosis"]) > 50
    assert {"77066", "19083", "96413", "582"} <= {c["code"] for c in codes}


@pytest.mark.parametrize("payer", INSURERS)
def test_plans_available_for_each_insurer(api, payer):
    plans = api.get("/api/v1/plans", params={"payer": payer}).json()
    assert plans and plans == sorted(plans) and len(plans) == len(set(plans))


# ---------------------------------------------------------------- extraction
def test_extract_on_realistic_note(api):
    note = ("62F with abnormal screening mammogram. Plan: bilateral diagnostic mammogram, breast ultrasound, "
            "and ultrasound-guided core biopsy of the left breast with pathology review.")
    r = api.post("/api/v1/extract", json={"note": note}).json()
    got = {c["code"] for c in r["codes"]}
    assert {"77066", "19083", "88305"} <= got
    assert all(c["evidence"] and 0 < c["confidence"] <= 1 for c in r["codes"])


# ---------------------------------------------------------------- estimates (insured)
@pytest.mark.parametrize("payer", INSURERS)
def test_diagnostic_bundle_ranks_hospitals_for_every_insurer(api, payer):
    r = estimate(api, insurance=ins(payer))
    ranked = [h for h in r["hospitals"] if h["rank"]]
    assert len(ranked) >= 3, "expected several fully-priced hospitals"
    totals = [h["total_patient_pays"] for h in sorted(ranked, key=lambda h: h["rank"])]
    assert totals == sorted(totals)                                   # rank 1 = cheapest
    assert r["best_hospital"] == min(ranked, key=lambda h: h["rank"])["hospital"]
    assert r["max_savings"] == pytest.approx(max(totals) - min(totals), abs=0.01)
    assert r["disclaimers"]


def test_line_math_is_consistent(api):
    r = estimate(api, insurance=ins(ded=500, pct=20, oop=3000))
    for h in r["hospitals"]:
        for l in h["lines"]:
            if l["status"] != "priced":
                continue
            assert l["patient_pays"] == pytest.approx(l["deductible_paid"] + l["coinsurance_paid"], abs=0.02)
            assert l["patient_pays"] + l["insurer_pays"] == pytest.approx(l["allowed_amount"], abs=0.02)
            assert l["rate_low"] <= l["allowed_amount"] <= l["rate_high"]
        priced = [l for l in h["lines"] if l["status"] == "priced"]
        assert h["total_patient_pays"] == pytest.approx(sum(l["patient_pays"] for l in priced), abs=0.05)
        assert h["total_patient_pays"] <= 3000 + 0.01                 # never above OOP max


def test_oop_max_caps_every_hospital(api):
    r = estimate(api, insurance=ins(ded=10_000, pct=100, oop=250))
    assert all(h["total_patient_pays"] <= 250.01 for h in r["hospitals"])


def test_zero_remaining_everything_costs_patient_nothing(api):
    r = estimate(api, insurance=ins(ded=0, pct=0, oop=0))
    assert all(h["total_patient_pays"] == 0 for h in r["hospitals"])


def test_higher_coinsurance_never_cheaper(api):
    low = {h["hospital"]: h["total_patient_pays"] for h in estimate(api, insurance=ins(ded=0, pct=10))["hospitals"]}
    high = {h["hospital"]: h["total_patient_pays"] for h in estimate(api, insurance=ins(ded=0, pct=40))["hospitals"]}
    assert all(high[k] >= low[k] for k in low)


def test_ecmc_cannot_be_priced_for_insured_patient(api):
    ecmc = next(h for h in estimate(api)["hospitals"] if h["hospital"] == "ECMC")
    assert ecmc["rank"] is None and ecmc["complete"] is False
    assert all(l["status"] == "unavailable" and l["reason"] for l in ecmc["lines"])


def test_inpatient_drg_is_unavailable_with_reason(api):
    r = estimate(api, codes=["582"])
    assert r["best_hospital"] is None
    lines = [l for h in r["hospitals"] for l in h["lines"]]
    assert lines and all(l["status"] == "unavailable" and l["reason"] for l in lines)


def test_plan_hint_is_reported_back(api):
    plans = api.get("/api/v1/plans", params={"payer": "Independent Health"}).json()
    r = estimate(api, insurance=ins(plan=plans[0]))
    matched = [l for h in r["hospitals"] for l in h["lines"] if l["plan_matched"]]
    assert matched and all(plans[0].lower() in l["plan_used"].lower() for l in matched)


def test_distance_sorted_data_present_when_location_given(api):
    r = estimate(api, patient_lat=42.8864, patient_lng=-78.8784)  # downtown Buffalo
    d = {h["hospital"]: h["distance_miles"] for h in r["hospitals"]}
    assert all(v is not None for v in d.values())
    assert d["Buffalo General Medical Center"] < d["Mount St. Mary's Hospital"]


def test_code_order_changes_who_uses_the_deductible(api):
    a = estimate(api, codes=["77066", "19083"], insurance=ins(ded=400, pct=0, oop=5000))
    b = estimate(api, codes=["19083", "77066"], insurance=ins(ded=400, pct=0, oop=5000))
    la = next(h for h in a["hospitals"] if h["hospital"] == "Roswell Park")["lines"]
    lb = next(h for h in b["hospitals"] if h["hospital"] == "Roswell Park")["lines"]
    assert la[0]["code"] == "77066" and lb[0]["code"] == "19083"
    assert la[0]["deductible_paid"] > 0 and lb[0]["deductible_paid"] > 0
    assert la[0]["deductible_paid"] != lb[0]["deductible_paid"]


# ---------------------------------------------------------------- self pay
def test_self_pay_ranks_and_has_no_payer(api):
    r = estimate(api, mode="self_pay", codes=["77066"])
    assert r["payer"] is None and r["mode"] == "self_pay"
    ranked = [h for h in r["hospitals"] if h["rank"]]
    assert ranked, "at least one hospital should have a cash/gross price for 77066"
    assert all(l["price_basis"] in ("cash", "gross") for h in ranked for l in h["lines"])
    # self-pay patient pays the full allowed amount
    for h in ranked:
        assert h["total_patient_pays"] == pytest.approx(sum(l["allowed_amount"] for l in h["lines"]), abs=0.02)


def test_self_pay_includes_ecmc_via_gross_charge(api):
    r = estimate(api, mode="self_pay", codes=["77066"])
    ecmc = next(h for h in r["hospitals"] if h["hospital"] == "ECMC")
    assert ecmc["lines"][0]["status"] == "priced" and ecmc["lines"][0]["price_basis"] == "gross"


# ---------------------------------------------------------------- error handling on real data
def test_unknown_code_404(api):
    assert api.post("/api/v1/estimate", json={"codes": ["00000"], "mode": "self_pay"}).status_code == 404


def test_insured_without_insurance_422(api):
    assert api.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "insured"}).status_code == 422


# ---------------------------------------------------------------- data hygiene
def test_no_out_of_area_plans_in_dropdowns(api):
    for payer in INSURERS:
        plans = api.get("/api/v1/plans", params={"payer": payer}).json()
        assert not [p for p in plans if "out of area" in p.lower() or "mich" in p.lower()], plans


def test_suspicious_surgery_rates_carry_a_warning(api):
    r = estimate(api, codes=["19301"], insurance=ins("Independent Health"))
    kaleida = next(h for h in r["hospitals"] if h["hospital"] == "Buffalo General Medical Center")
    assert kaleida["lines"][0]["status"] == "priced" and kaleida["lines"][0]["warning"]
