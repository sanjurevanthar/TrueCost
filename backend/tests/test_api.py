INS = {"payer": "Univera", "plan": "PPO", "deductible_remaining": 0, "coinsurance_pct": 20, "oop_max_remaining": 5000}


def est(client, **body):
    body.setdefault("mode", "insured")
    if body["mode"] == "insured":
        body.setdefault("insurance", INS)
    return client.post("/api/v1/estimate", json=body)


def test_health_and_reference(client):
    assert client.get("/health").json()["status"] == "ok"
    assert len(client.get("/api/v1/codes").json()) == 3
    assert [c["code"] for c in client.get("/api/v1/codes?phase=treatment").json()] == ["582"]
    assert len(client.get("/api/v1/hospitals").json()) == 3
    assert client.get("/api/v1/plans?payer=Univera").json() == ["HMO", "PPO"]


def test_ranking_cheapest_first_and_savings(client):
    r = est(client, codes=["77066"]).json()
    ranked = [h for h in r["hospitals"] if h["rank"]]
    assert [h["hospital"] for h in ranked] == ["Beta", "Alpha"]          # 20% of 200 < 20% of 300
    assert ranked[0]["total_patient_pays"] == 40.0
    assert ranked[0]["savings_vs_most_expensive"] == 20.0
    assert r["best_hospital"] == "Beta" and r["max_savings"] == 20.0


def test_plan_hint_picks_plan(client):
    # Beta lists PPO 200 and HMO 220. Asking for HMO must use 220.
    r = est(client, codes=["77066"], insurance={**INS, "plan": "HMO"}).json()
    beta = next(h for h in r["hospitals"] if h["hospital"] == "Beta")
    assert beta["lines"][0]["allowed_amount"] == 220.0 and beta["lines"][0]["plan_matched"] is True


def test_no_plan_hint_uses_median_and_reports_range(client):
    r = est(client, codes=["77066"], insurance={**INS, "plan": None}).json()
    line = next(h for h in r["hospitals"] if h["hospital"] == "Beta")["lines"][0]
    assert (line["rate_low"], line["rate_high"]) == (200.0, 220.0)


def test_gross_only_hospital_is_unavailable_when_insured(client):
    r = est(client, codes=["77066"]).json()
    gamma = next(h for h in r["hospitals"] if h["hospital"] == "Gamma")
    assert gamma["rank"] is None and gamma["complete"] is False
    assert "gross charge only" in gamma["lines"][0]["reason"]


def test_inpatient_drg_unavailable_and_blocks_ranking(client):
    r = est(client, codes=["77066", "582"]).json()
    assert all(h["rank"] is None for h in r["hospitals"])
    assert r["best_hospital"] is None
    assert any("contact the hospital" in l["reason"] for h in r["hospitals"] for l in h["lines"] if l["status"] == "unavailable")


def test_deductible_carries_across_services(client):
    ins = {**INS, "deductible_remaining": 1100, "oop_max_remaining": 5000}
    alpha = next(h for h in est(client, codes=["77066", "19081"], insurance=ins).json()["hospitals"]
                 if h["hospital"] == "Alpha")
    first, second = alpha["lines"]
    assert first["patient_pays"] == 300.0                        # all deductible
    assert second["deductible_paid"] == 800.0 and second["patient_pays"] == 800.0 + 0.2 * 200


def test_oop_max_caps_total(client):
    ins = {**INS, "coinsurance_pct": 100, "oop_max_remaining": 250}
    alpha = next(h for h in est(client, codes=["77066", "19081"], insurance=ins).json()["hospitals"]
                 if h["hospital"] == "Alpha")
    assert alpha["total_patient_pays"] == 250.0


def test_self_pay_uses_cash_then_gross(client):
    r = est(client, codes=["77066"], mode="self_pay").json()
    prices = {h["hospital"]: (h["lines"][0]["allowed_amount"], h["lines"][0]["price_basis"]) for h in r["hospitals"]}
    assert prices == {"Alpha": (150.0, "cash"), "Beta": (120.0, "cash"), "Gamma": (400.0, "gross")}
    assert r["hospitals"][0]["hospital"] == "Beta" and r["payer"] is None


def test_distance_when_location_given(client):
    r = est(client, codes=["77066"], patient_lat=42.89, patient_lng=-78.87).json()
    assert all(h["distance_miles"] is not None for h in r["hospitals"])
    assert all(h["distance_miles"] is None for h in est(client, codes=["77066"]).json()["hospitals"])


def test_errors(client):
    assert est(client, codes=["99999"]).status_code == 404
    assert client.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "insured"}).status_code == 422
    assert est(client, codes=["77066"], insurance={**INS, "coinsurance_pct": 150}).status_code == 422
    assert est(client, codes=[]).status_code == 422
    assert est(client, codes=["77066"], insurance={**INS, "payer": "Aetna"}).status_code == 422


def test_duplicate_codes_are_deduplicated(client):
    assert est(client, codes=["77066", "77066"]).json()["codes"] == ["77066"]


def test_extract_endpoint(client):
    note = "Patient needs a bilateral diagnostic mammogram and a stereotactic biopsy of the left breast."
    r = client.post("/api/v1/extract", json={"note": note}).json()
    assert {"77066", "19081"} <= {c["code"] for c in r["codes"]}
    assert "77065" not in {c["code"] for c in r["codes"]}
