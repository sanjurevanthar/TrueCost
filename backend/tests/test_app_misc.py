import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.exceptions import DomainError, InvalidInsuranceError, MissingInsuranceError, UnknownCodeError
from app.main import create_app
from tests.conftest import FakeRepo


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    s = Settings.from_env()
    assert s.db_path.name == "truecost.db" and s.cors_origins == ("*",)


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("DB_PATH", "/tmp/x.db")
    monkeypatch.setenv("CORS_ORIGINS", "http://a.com,http://b.com")
    s = Settings.from_env()
    assert str(s.db_path) == "/tmp/x.db" and s.cors_origins == ("http://a.com", "http://b.com")


def test_exception_status_codes():
    assert DomainError.status_code == 400
    assert UnknownCodeError(["1"]).status_code == 404 and "1" in str(UnknownCodeError(["1"]))
    assert InvalidInsuranceError().status_code == MissingInsuranceError().status_code == 422


def test_container_uses_keyword_extractor_by_default():
    assert Container.from_repo(FakeRepo()).extractor.name == "keyword-fallback"


def test_cors_headers_present(client):
    r = client.options("/api/v1/codes", headers={"Origin": "http://lovable.app", "Access-Control-Request-Method": "GET"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] in ("*", "http://lovable.app")


def test_cors_restricted_origins(monkeypatch):
    app = create_app(container=Container.from_repo(FakeRepo()), settings=Settings(db_path=None, cors_origins=("http://ok.com",)))
    r = TestClient(app).get("/health", headers={"Origin": "http://evil.com"})
    assert "access-control-allow-origin" not in r.headers


def test_unknown_plans_payer_is_422(client):
    assert client.get("/api/v1/plans?payer=Aetna").status_code == 422
    assert client.get("/api/v1/plans").status_code == 422


@pytest.mark.parametrize("body", [{}, {"note": ""}, {"note": "ab"}, {"note": "x" * 20001}])
def test_extract_validation(client, body):
    assert client.post("/api/v1/extract", json=body).status_code == 422


def test_extract_reports_extractor_name_and_empty_result(client):
    r = client.post("/api/v1/extract", json={"note": "Patient has a cold and cough."}).json()
    assert r["extractor"] == "keyword-fallback" and r["codes"] == []


def test_estimate_rejects_too_many_codes(client):
    body = {"codes": [str(i) for i in range(21)], "mode": "self_pay"}
    assert client.post("/api/v1/estimate", json=body).status_code == 422


def test_estimate_rejects_bad_coordinates(client):
    body = {"codes": ["77066"], "mode": "self_pay", "patient_lat": 123, "patient_lng": 0}
    assert client.post("/api/v1/estimate", json=body).status_code == 422


def test_estimate_self_pay_needs_no_insurance(client):
    assert client.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"}).status_code == 200


def test_unknown_route_404(client):
    assert client.get("/nope").status_code == 404
