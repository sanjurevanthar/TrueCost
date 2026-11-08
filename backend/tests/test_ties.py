"""When the deductible / out-of-pocket max decides the patient's cost, hospitals can tie. Rank them as a tie."""
from fastapi.testclient import TestClient

from app.container import Container
from app.domain.models import Hospital
from app.main import create_app
from tests.conftest import FakeRepo, rec


class TieRepo(FakeRepo):
    HOSPITALS = [Hospital("A", "1", 42.9, -78.9), Hospital("B", "2", 42.9, -78.8), Hospital("C", "3", 42.9, -78.7)]
    PRICES = [rec("A", "77066", "Univera", "PPO", 100, cash=90, gross=200),
              rec("B", "77066", "Univera", "PPO", 140, cash=90, gross=200),   # dearer for the insurer, same for patient
              rec("C", "77066", "Univera", "PPO", 500, cash=90, gross=900)]


def est(pct, oop, ded=0, mode="insured"):
    c = TestClient(create_app(container=Container.from_repo(TieRepo())))
    body = {"codes": ["77066"], "mode": mode}
    if mode == "insured":
        body["insurance"] = {"payer": "Univera", "deductible_remaining": ded, "coinsurance_pct": pct, "oop_max_remaining": oop}
    return c.post("/api/v1/estimate", json=body).json()


def by_name(r):
    return {h["hospital"]: h for h in r["hospitals"]}


def test_cap_makes_everyone_tie_for_first():
    r = est(pct=100, oop=50)                                   # everyone hits the $50 cap
    h = by_name(r)
    assert [h[n]["rank"] for n in "ABC"] == [1, 1, 1] and all(h[n]["tied"] for n in "ABC")
    assert sorted(r["best_hospitals"]) == ["A", "B", "C"] and r["max_savings"] == 0
    assert "same ($50) at 3 hospitals" in r["notice"] and "insurer" in r["notice"]


def test_competition_ranking_skips_after_a_tie():
    r = est(pct=10, oop=1000)                                  # A pays 10, B pays 14, C pays 50  -> no tie
    assert [by_name(r)[n]["rank"] for n in "ABC"] == [1, 2, 3]
    r = est(pct=10, oop=12)                                    # A 10, B capped 12, C capped 12  -> B and C tie for 2nd
    h = by_name(r)
    assert [h[n]["rank"] for n in "ABC"] == [1, 2, 2] and (h["B"]["tied"], h["C"]["tied"], h["A"]["tied"]) == (True, True, False)
    assert r["best_hospitals"] == ["A"] and r["notice"] is None      # the TOP spot is not tied


def test_no_tie_no_notice_and_flags_false():
    r = est(pct=20, oop=5000)
    assert r["notice"] is None and not any(h["tied"] for h in r["hospitals"]) and r["best_hospitals"] == ["A"]


def test_tied_hospitals_are_ordered_by_what_the_insurer_pays_least():
    r = est(pct=100, oop=50)
    assert [h["hospital"] for h in r["hospitals"]] == ["A", "B", "C"]          # allowed 100 < 140 < 500
    assert r["best_hospital"] == "A"                                          # still a single name for older clients


def test_self_pay_ties_get_a_self_pay_message():
    class SameCash(TieRepo):
        PRICES = [rec(n, "77066", cash=120, gross=300) for n in "ABC"]
    c = TestClient(create_app(container=Container.from_repo(SameCash())))
    r = c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"}).json()
    assert r["notice"] == "3 hospitals list identical prices for these services, so each would cost you $120."


def test_incomplete_hospitals_never_join_a_tie():
    class Partial(TieRepo):
        PRICES = TieRepo.PRICES[:2]                              # C has no price -> not ranked
    c = TestClient(create_app(container=Container.from_repo(Partial())))
    r = c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "insured", "insurance": {
        "payer": "Univera", "deductible_remaining": 0, "coinsurance_pct": 100, "oop_max_remaining": 50}}).json()
    h = by_name(r)
    assert h["C"]["rank"] is None and h["C"]["tied"] is False and r["best_hospitals"] == ["A", "B"]


def test_notice_blames_shared_prices_not_the_cap_when_prices_are_identical():
    class Same(TieRepo):
        PRICES = [rec("A", "77066", "Univera", "PPO", 200), rec("B", "77066", "Univera", "PPO", 200),
                  rec("C", "77066", "Univera", "PPO", 900)]
    c = TestClient(create_app(container=Container.from_repo(Same())))
    r = c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "insured", "insurance": {
        "payer": "Univera", "deductible_remaining": 0, "coinsurance_pct": 20, "oop_max_remaining": 5000}}).json()
    assert r["best_hospitals"] == ["A", "B"] and "identical prices" in r["notice"] and "deductible" not in r["notice"]
