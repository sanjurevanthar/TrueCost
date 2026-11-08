"""The exact request bodies we hand out for Swagger, run against the real app + real truecost.db."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT
from app.main import create_app

pytestmark = pytest.mark.skipif(not (Path(PROJECT_ROOT) / "truecost.db").exists(), reason="truecost.db not built")


@pytest.fixture(scope="module")
def api():
    return TestClient(create_app())


def post(api, body):
    return api.post("/api/v1/estimate", json=body)


def test_swagger_default_example_fails_with_404_unknown_code(api):
    default = {"codes": ["string"], "mode": "insured",
               "insurance": {"payer": "Independent Health", "plan": "string", "deductible_remaining": 0,
                             "coinsurance_pct": 0, "oop_max_remaining": 0},
               "patient_lat": 0, "patient_lng": 0}
    r = post(api, default)
    assert r.status_code == 404 and "string" in r.json()["detail"]


def test_recommended_insured_body_ranks_hospitals(api):
    r = post(api, {"codes": ["77066", "76642", "19083"], "mode": "insured",
                   "insurance": {"payer": "Independent Health", "plan": None, "deductible_remaining": 500,
                                 "coinsurance_pct": 20, "oop_max_remaining": 3000},
                   "patient_lat": 42.8864, "patient_lng": -78.8784})
    assert r.status_code == 200
    j = r.json()
    ranked = [h for h in j["hospitals"] if h["rank"]]
    assert len(ranked) >= 3 and j["best_hospital"] and j["max_savings"] >= 0
    ecmc = next(h for h in j["hospitals"] if h["hospital"] == "ECMC")
    assert ecmc["rank"] is None


def test_self_pay_body(api):
    j = post(api, {"codes": ["77066", "76642"], "mode": "self_pay"}).json()
    assert j["payer"] is None and any(h["rank"] for h in j["hospitals"])


def test_treatment_phase_body_returns_rows_for_every_hospital(api):
    j = post(api, {"codes": ["19301", "38525", "96413", "77385"], "mode": "insured",
                   "insurance": {"payer": "Independent Health", "deductible_remaining": 1000,
                                 "coinsurance_pct": 20, "oop_max_remaining": 5000}}).json()
    assert len(j["hospitals"]) == 7
    assert any(l["status"] == "unavailable" for h in j["hospitals"] for l in h["lines"])  # gaps are explained


def test_drg_body_says_contact_hospital(api):
    j = post(api, {"codes": ["582"], "mode": "self_pay"}).json()
    assert j["best_hospital"] is None


@pytest.mark.parametrize("body,status", [
    ({"codes": ["77066"], "mode": "insured"}, 422),
    ({"codes": ["77066"], "mode": "insured", "insurance": {"payer": "Aetna", "deductible_remaining": 0,
                                                            "coinsurance_pct": 0, "oop_max_remaining": 0}}, 422),
    ({"codes": ["77066"], "mode": "insured", "insurance": {"payer": "Univera", "deductible_remaining": 0,
                                                            "coinsurance_pct": 150, "oop_max_remaining": 0}}, 422),
])
def test_documented_error_cases(api, body, status):
    assert post(api, body).status_code == status


def test_plans_requires_payer(api):
    assert api.get("/api/v1/plans").status_code == 422
    assert api.get("/api/v1/plans", params={"payer": "Independent Health"}).json()
