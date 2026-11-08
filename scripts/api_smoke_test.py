"""API automation: black-box smoke/regression test for a RUNNING TrueCost API (local, tunnel or deployed).

Standard library only - no pip install needed.

Usage (from project root):
  .venv/bin/python scripts/api_smoke_test.py                                  # http://localhost:8000
  .venv/bin/python scripts/api_smoke_test.py --base-url https://abc.ngrok.app # test the tunnel Lovable uses
  .venv/bin/python scripts/api_smoke_test.py --cors-origin https://myapp.lovable.app

Exit code 0 = all checks passed, 1 = at least one failed (usable in CI / a pre-demo checklist).
Checks the API contract the frontend relies on: response shapes, required fields, ranking order, error codes, CORS.
"""
import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    import certifi
    CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    CTX = None

PAYERS = ["Independent Health", "Highmark BCBS", "Univera"]
RESULTS = []
API_KEY = None  # set from --api-key / API_ACCESS_TOKEN


def call(base, method, path, body=None, headers=None, timeout=30):
    """Return (status, parsed_json_or_None, response_headers, seconds)."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", "Accept": "application/json", "ngrok-skip-browser-warning": "1",
         **({"X-API-Key": API_KEY} if API_KEY else {}), **(headers or {})}
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method, headers=h)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            raw, status, hdrs = r.read(), r.status, r.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    return status, parsed, hdrs, time.time() - t0


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  PASS  {name}")
    except Exception as e:  # noqa: BLE001 - report every failure, keep going
        RESULTS.append((name, False, str(e)))
        print(f"  FAIL  {name}\n        -> {e}")


def expect(cond, msg):
    if not cond:
        raise AssertionError(msg)


def insured(payer="Independent Health", **kw):
    return {"payer": payer, "plan": None, "deductible_remaining": 500, "coinsurance_pct": 20,
            "oop_max_remaining": 3000, **kw}


def run(base, origin):
    state = {}

    def t_health():
        s, j, _, _ = call(base, "GET", "/health")
        expect(s == 200 and j == {"status": "ok"}, f"got {s} {j}")

    def t_openapi():
        s, j, _, _ = call(base, "GET", "/openapi.json")
        expect(s == 200, f"status {s}")
        paths = set(j["paths"])
        need = {"/api/v1/codes", "/api/v1/hospitals", "/api/v1/plans", "/api/v1/extract", "/api/v1/estimate"}
        expect(need <= paths, f"missing endpoints: {need - paths}")

    def t_hospitals():
        s, j, _, _ = call(base, "GET", "/api/v1/hospitals")
        expect(s == 200 and len(j) >= 7, f"{s}, {len(j or [])} hospitals")
        for h in j:
            expect({"hospital", "address", "lat", "lng"} <= set(h), f"bad shape {h}")
            expect(h["lat"] is not None and h["lng"] is not None, f"{h['hospital']} has no coordinates")
        state["hospitals"] = [h["hospital"] for h in j]

    def t_codes():
        s, j, _, _ = call(base, "GET", "/api/v1/codes?phase=diagnostic")
        expect(s == 200 and j, f"{s}")
        expect(all(c["phase"] == "diagnostic" for c in j), "phase filter leaked other phases")
        expect({"code", "code_type", "description", "phase"} <= set(j[0]), "bad shape")
        expect("77066" in {c["code"] for c in j}, "77066 missing")

    def t_plans():
        for p in PAYERS:
            s, j, _, _ = call(base, "GET", "/api/v1/plans?" + urllib.parse.urlencode({"payer": p}))
            expect(s == 200 and isinstance(j, list) and j, f"{p}: {s} {j}")

    def t_plans_bad_payer():
        s, _, _, _ = call(base, "GET", "/api/v1/plans?payer=Aetna")
        expect(s == 422, f"expected 422, got {s}")

    def t_extract():
        note = "Abnormal screening mammogram. Order bilateral diagnostic mammogram and stereotactic biopsy."
        s, j, _, dt = call(base, "POST", "/api/v1/extract", {"note": note})
        expect(s == 200, f"status {s}: {j}")
        expect({"extractor", "codes"} <= set(j), f"bad shape {j}")
        got = {c["code"] for c in j["codes"]}
        expect({"77066", "19081"} <= got, f"expected 77066 + 19081, got {got}")
        expect(all({"code", "description", "evidence", "confidence"} <= set(c) for c in j["codes"]), "bad code shape")
        expect(dt < 20, f"extract took {dt:.1f}s (too slow for a live demo)")

    def t_extract_validation():
        s, _, _, _ = call(base, "POST", "/api/v1/extract", {"note": ""})
        expect(s == 422, f"expected 422, got {s}")

    def t_estimate_insured():
        for payer in PAYERS:
            body = {"codes": ["77066", "76642", "19083"], "mode": "insured", "insurance": insured(payer)}
            s, j, _, dt = call(base, "POST", "/api/v1/estimate", body)
            expect(s == 200, f"{payer}: status {s}: {j}")
            for k in ("mode", "payer", "codes", "hospitals", "best_hospital", "max_savings", "disclaimers"):
                expect(k in j, f"{payer}: response missing '{k}'")
            expect(j["payer"] == payer and j["disclaimers"], f"{payer}: payer/disclaimers wrong")
            ranked = sorted((h for h in j["hospitals"] if h["rank"]), key=lambda h: h["rank"])
            expect(len(ranked) >= 2, f"{payer}: only {len(ranked)} ranked hospitals")
            totals = [h["total_patient_pays"] for h in ranked]
            expect(totals == sorted(totals), f"{payer}: ranking not ascending: {totals}")
            expect(j["best_hospital"] == ranked[0]["hospital"], f"{payer}: best_hospital mismatch")
            expect(abs(j["max_savings"] - (totals[-1] - totals[0])) < 0.02, f"{payer}: max_savings wrong")
            expect(dt < 10, f"{payer}: estimate took {dt:.1f}s")
            for h in j["hospitals"]:
                expect({"hospital", "rank", "complete", "coverage", "total_patient_pays", "lines"} <= set(h),
                       f"hospital shape: {sorted(h)}")
                for l in h["lines"]:
                    expect(l["status"] in ("priced", "unavailable"), f"bad status {l['status']}")
                    expect(l["status"] == "priced" or l["reason"], f"{h['hospital']} {l['code']}: no reason")
                    if l["status"] == "priced":
                        expect(abs(l["patient_pays"] + l["insurer_pays"] - l["allowed_amount"]) < 0.03,
                               f"{h['hospital']} {l['code']}: patient+insurer != allowed")
                expect(h["total_patient_pays"] <= 3000.01, f"{h['hospital']}: above OOP max")

    def t_estimate_self_pay():
        s, j, _, _ = call(base, "POST", "/api/v1/estimate", {"codes": ["77066"], "mode": "self_pay"})
        expect(s == 200 and j["payer"] is None, f"{s} {j and j.get('payer')}")
        expect(any(h["rank"] for h in j["hospitals"]), "nobody ranked")

    def t_estimate_distance():
        body = {"codes": ["77066"], "mode": "self_pay", "patient_lat": 42.8864, "patient_lng": -78.8784}
        s, j, _, _ = call(base, "POST", "/api/v1/estimate", body)
        expect(s == 200 and all(h["distance_miles"] is not None for h in j["hospitals"]), "distance missing")

    def t_estimate_drg_unavailable():
        body = {"codes": ["582"], "mode": "insured", "insurance": insured()}
        s, j, _, _ = call(base, "POST", "/api/v1/estimate", body)
        expect(s == 200 and j["best_hospital"] is None, f"{s}, best={j and j.get('best_hospital')}")

    def t_errors():
        cases = [
            ("unknown code", {"codes": ["00000"], "mode": "self_pay"}, 404),
            ("insured w/o insurance", {"codes": ["77066"], "mode": "insured"}, 422),
            ("empty codes", {"codes": [], "mode": "self_pay"}, 422),
            ("coinsurance > 100", {"codes": ["77066"], "mode": "insured", "insurance": insured(coinsurance_pct=150)}, 422),
            ("negative deductible", {"codes": ["77066"], "mode": "insured", "insurance": insured(deductible_remaining=-1)}, 422),
            ("unsupported insurer", {"codes": ["77066"], "mode": "insured", "insurance": insured("Aetna")}, 422),
            ("bad latitude", {"codes": ["77066"], "mode": "self_pay", "patient_lat": 999, "patient_lng": 0}, 422),
        ]
        bad = []
        for name, body, want in cases:
            s, _, _, _ = call(base, "POST", "/api/v1/estimate", body)
            if s != want:
                bad.append(f"{name}: expected {want}, got {s}")
        expect(not bad, "; ".join(bad))

    def t_cors():
        s, _, h, _ = call(base, "OPTIONS", "/api/v1/estimate", headers={
            "Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        expect(s in (200, 204), f"preflight status {s}")
        allow = h.get("access-control-allow-origin")
        expect(allow in ("*", origin), f"Access-Control-Allow-Origin={allow!r} - Lovable would be blocked")

    def t_idempotent():
        body = {"codes": ["77066", "19083"], "mode": "insured", "insurance": insured()}
        a = call(base, "POST", "/api/v1/estimate", body)[1]
        b = call(base, "POST", "/api/v1/estimate", body)[1]
        expect(a == b, "same request gave different answers")

    print(f"\nTrueCost API smoke test -> {base}\n")
    for name, fn in [
        ("GET /health", t_health), ("OpenAPI lists all endpoints", t_openapi),
        ("GET /hospitals has coordinates", t_hospitals), ("GET /codes?phase filter", t_codes),
        ("GET /plans for each insurer", t_plans), ("GET /plans rejects unknown insurer", t_plans_bad_payer),
        ("POST /extract finds codes", t_extract), ("POST /extract validates input", t_extract_validation),
        ("POST /estimate insured: shape, ranking, math (3 insurers)", t_estimate_insured),
        ("POST /estimate self_pay", t_estimate_self_pay), ("POST /estimate distance", t_estimate_distance),
        ("POST /estimate inpatient DRG -> unavailable", t_estimate_drg_unavailable),
        ("POST /estimate error codes", t_errors), (f"CORS preflight from {origin}", t_cors),
        ("estimates are deterministic", t_idempotent),
    ]:
        check(name, fn)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--cors-origin", default="https://example.lovable.app")
    ap.add_argument("--api-key", default=os.getenv("API_ACCESS_TOKEN"), help="X-API-Key if the server sets API_ACCESS_TOKEN")
    a = ap.parse_args()
    global API_KEY
    API_KEY = a.api_key
    try:
        call(a.base_url, "GET", "/health", timeout=10)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Cannot reach {a.base_url}: {e}\nStart the API first (see HANDOFF.md) or pass --base-url.")
    run(a.base_url, a.cors_origin)
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
