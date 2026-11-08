from decimal import Decimal as D

from app.domain.rate_selector import PlanAwareMedianSelector
from tests.conftest import rec

sel = PlanAwareMedianSelector()


def test_no_candidates_returns_none():
    assert sel.select([]) is None


def test_non_dollar_rates_are_ignored():
    pct = rec("A", "582", "Univera", "PPO", None, rate_type="percent_of_charges")
    gross_only = rec("A", "77066", gross=400)
    assert sel.select([pct, gross_only]) is None


def test_single_rate():
    r = sel.select([rec("A", "77066", "Univera", "PPO", 200)])
    assert (r.rate, r.low, r.high, r.candidates, r.plan_matched) == (D("200.00"), D("200"), D("200"), 1, False)


def test_median_resists_outlier():
    cands = [rec("A", "77066", "Univera", p, v) for p, v in (("P1", 100), ("P2", 110), ("P3", 5000))]
    r = sel.select(cands)
    assert r.rate == D("110.00") and (r.low, r.high) == (D("100"), D("5000"))


def test_even_count_median_is_average_rounded_to_cents():
    cands = [rec("A", "77066", "Univera", "P1", 100), rec("A", "77066", "Univera", "P2", "101.01")]
    assert sel.select(cands).rate == D("100.51")  # 100.505 -> half-up


def test_plan_hint_matches_case_insensitive_substring():
    cands = [rec("A", "77066", "Univera", "Access PPO Plan", 300), rec("A", "77066", "Univera", "HMO Gold", 200)]
    r = sel.select(cands, "ppo")
    assert r.rate == D("300.00") and r.plan_matched is True and r.candidates == 1


def test_plan_hint_without_match_falls_back_to_all_plans():
    cands = [rec("A", "77066", "Univera", "PPO", 300), rec("A", "77066", "Univera", "HMO", 200)]
    r = sel.select(cands, "EPO")
    assert r.plan_matched is False and r.candidates == 2 and r.rate == D("250.00")


def test_blank_hint_is_treated_as_no_hint():
    r = sel.select([rec("A", "77066", "Univera", "PPO", 300)], "   ")
    assert r.plan_matched is False


def test_candidates_without_plan_name_never_match_hint():
    r = sel.select([rec("A", "77066", "Univera", None, 300)], "PPO")
    assert r.plan_matched is False and r.rate == D("300.00")


def test_record_is_closest_to_chosen_rate():
    cands = [rec("A", "77066", "Univera", "P1", 100), rec("A", "77066", "Univera", "P2", 210),
             rec("A", "77066", "Univera", "P3", 900)]
    assert sel.select(cands).record.plan == "P2"
