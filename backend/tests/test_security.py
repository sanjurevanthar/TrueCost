import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.security import RateLimiter, RateLimitedError
from tests.conftest import FakeRepo

NOTE = {"note": "Order a bilateral diagnostic mammogram."}


def app(limit=0, token=None):
    c = Container.from_repo(FakeRepo())
    c.limiter, c.access_token = RateLimiter(limit), token
    return TestClient(create_app(container=c))


# ---------------------------------------------------------------- limiter unit
def test_limiter_blocks_after_limit_and_recovers_after_window():
    rl = RateLimiter(3, window=60)
    for t in (0, 1, 2):
        rl.check("a", now=t)
    with pytest.raises(RateLimitedError) as e:
        rl.check("a", now=3)
    assert 55 <= e.value.retry_after <= 60
    rl.check("a", now=61)                       # oldest hit aged out


def test_limiter_is_per_client():
    rl = RateLimiter(1)
    rl.check("a", now=0)
    rl.check("b", now=0)
    with pytest.raises(RateLimitedError):
        rl.check("a", now=1)


def test_limiter_global_cap_is_4x():
    rl = RateLimiter(1)
    for i in range(4):
        rl.check(f"client{i}", now=0)
    with pytest.raises(RateLimitedError):
        rl.check("client5", now=0)


def test_limit_zero_disables():
    rl = RateLimiter(0)
    for _ in range(1000):
        rl.check("a")


# ---------------------------------------------------------------- over HTTP
def test_rate_limit_returns_429_with_retry_after():
    c = app(limit=2)
    assert [c.post("/api/v1/extract", json=NOTE).status_code for _ in range(3)] == [200, 200, 429]
    r = c.post("/api/v1/extract", json=NOTE)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1 and "Too many" in r.json()["detail"]


def test_forwarded_for_separates_clients():
    c = app(limit=1)
    h = lambda ip: {"X-Forwarded-For": ip}
    assert c.post("/api/v1/extract", json=NOTE, headers=h("1.1.1.1")).status_code == 200
    assert c.post("/api/v1/extract", json=NOTE, headers=h("2.2.2.2")).status_code == 200
    assert c.post("/api/v1/extract", json=NOTE, headers=h("1.1.1.1")).status_code == 429


def test_non_ai_endpoints_are_not_limited():
    c = app(limit=1)
    for _ in range(5):
        assert c.get("/api/v1/hospitals").status_code == 200
    assert c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"}).status_code == 200


def test_access_token_required_when_configured():
    c = app(token="s3cret")
    assert c.post("/api/v1/extract", json=NOTE).status_code == 401
    assert c.post("/api/v1/extract", json=NOTE, headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.post("/api/v1/extract", json=NOTE, headers={"X-API-Key": "s3cret"}).status_code == 200


def test_image_endpoint_is_guarded_too():
    c = app(token="s3cret")
    r = c.post("/api/v1/extract/image", files={"file": ("n.png", b"x", "image/png")})
    assert r.status_code == 401


def test_token_does_not_gate_reference_endpoints():
    assert app(token="s3cret").get("/api/v1/hospitals").status_code == 200


def test_cors_allows_the_api_key_header():
    r = app(token="x").options("/api/v1/extract", headers={
        "Origin": "https://a.lovable.app", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "x-api-key,content-type"})
    assert r.status_code == 200 and "x-api-key" in r.headers["access-control-allow-headers"].lower()


# ---------------------------------------------------------------- settings
def test_settings_defaults_and_env(monkeypatch):
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    s = Settings.from_env()
    assert s.ai_rate_limit_per_min == 10 and s.api_access_token is None
    monkeypatch.setenv("AI_RATE_LIMIT_PER_MIN", "3")
    monkeypatch.setenv("API_ACCESS_TOKEN", "abc")
    s = Settings.from_env()
    assert s.ai_rate_limit_per_min == 3 and s.api_access_token == "abc"
    monkeypatch.setenv("API_ACCESS_TOKEN", "")
    assert Settings.from_env().api_access_token is None


def test_cors_origins_are_trimmed(monkeypatch):
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://a.com , https://b.com,, ")
    assert Settings.from_env().cors_origins == ("https://a.com", "https://b.com")
