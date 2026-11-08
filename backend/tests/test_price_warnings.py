from tests.conftest import FakeRepo, rec
from tests.test_api import INS, est


class Repo(FakeRepo):
    PRICES = FakeRepo.PRICES + [
        # Alpha/19081 stays normal. Delta: negotiated 10x the listed charge (bundled/case rate) and a very low cash price.
        rec("Delta", "77066", "Univera", "PPO", 3000, cash=40, gross=300),
        rec("Delta", "19081", "Univera", "PPO", 500, cash=400, gross=450),
    ]
    HOSPITALS = FakeRepo.HOSPITALS + [type(FakeRepo.HOSPITALS[0])("Delta", "4 D St", 42.9, -78.9)]


def client_for(repo):
    from fastapi.testclient import TestClient
    from app.container import Container
    from app.main import create_app
    return TestClient(create_app(container=Container.from_repo(repo)))


def line(resp, hospital, code):
    h = next(h for h in resp["hospitals"] if h["hospital"] == hospital)
    return next(l for l in h["lines"] if l["code"] == code)


def test_rate_far_above_listed_charge_gets_warning():
    r = est(client_for(Repo()), codes=["77066", "19081"]).json()
    w = line(r, "Delta", "77066")["warning"]
    assert w and "confirm" in w.lower()


def test_normal_rates_have_no_warning():
    r = est(client_for(Repo()), codes=["77066", "19081"]).json()
    assert line(r, "Delta", "19081")["warning"] is None
    assert line(r, "Alpha", "77066")["warning"] is None


def test_exactly_2x_charge_is_not_flagged():
    class R(FakeRepo):
        PRICES = [rec("Alpha", "77066", "Univera", "PPO", 600, gross=300)]
    assert line(est(client_for(R()), codes=["77066"]).json(), "Alpha", "77066")["warning"] is None


def test_unknown_gross_never_warns():
    class R(FakeRepo):
        PRICES = [rec("Alpha", "77066", "Univera", "PPO", 99999)]
    assert line(est(client_for(R()), codes=["77066"]).json(), "Alpha", "77066")["warning"] is None


def test_self_pay_very_low_cash_price_warns():
    r = est(client_for(Repo()), codes=["77066"], mode="self_pay").json()
    assert line(r, "Delta", "77066")["warning"]                 # cash 40 vs charge 300
    assert line(r, "Alpha", "77066")["warning"] is None          # cash 150 vs 500 (30%) is fine


def test_warning_does_not_change_the_numbers():
    r = est(client_for(Repo()), codes=["77066"]).json()
    l = line(r, "Delta", "77066")
    assert l["allowed_amount"] == 3000.0 and l["patient_pays"] == 600.0     # 20% of 3000
