import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.security import RateLimiter
from app.services.extraction import TokenUsage
from app.services.gemini_extractor import GeminiCodeExtractor
from app.usage import UsageTracker
from tests.conftest import FakeRepo
from tests.test_claude_extractor import Repo, item
from tests.test_extract_image import PNG, FakeTranscriber
from tests.test_gemini_extractor import FakeGemini
from tests.test_token_usage import gem_with_usage

NOTE = {"note": "Order a bilateral diagnostic mammogram."}


# ---------------------------------------------------------------- UsageTracker daily counter
def test_tokens_today_accumulates_and_rolls_at_utc_midnight():
    now = [1_700_000_000.0]                      # 2023-11-14 22:13 UTC
    t = UsageTracker(clock=lambda: now[0])
    t.record("extract", TokenUsage(10, 5, 15))
    t.record("read_image", TokenUsage(100, 20, 120))
    assert t.tokens_today() == 135
    now[0] += 3 * 3600                            # past midnight UTC
    assert t.tokens_today() == 0
    t.record("extract", TokenUsage(1, 1, 2))
    s = t.snapshot()
    assert s["today_utc"]["tokens"] == 2 and s["total"]["total_tokens"] == 137     # lifetime totals keep counting


def test_calls_without_tokens_do_not_count():
    t = UsageTracker()
    t.record("extract", None)
    assert t.tokens_today() == 0


# ---------------------------------------------------------------- daily budget
def client(budget, extractor=None, transcriber=None, spent=0):
    c = Container.from_repo(Repo(), extractor=extractor, transcriber=transcriber)
    c.daily_token_budget = budget
    c.limiter = RateLimiter(0)
    if spent:
        c.usage.record("extract", TokenUsage(spent, 0, spent))
    return TestClient(create_app(container=c)), c


def llm():
    return GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))


def test_under_budget_uses_the_ai():
    cl, _ = client(5000, llm())
    j = cl.post("/api/v1/extract", json=NOTE).json()
    assert j["extractor"].startswith("gemini") and j["notice"] is None and j["usage"]["total_tokens"] == 1000


def test_budget_runs_out_then_basic_matcher_answers_with_a_notice():
    cl, _ = client(1500, llm())
    first = cl.post("/api/v1/extract", json=NOTE).json()          # 1000 tokens used
    second = cl.post("/api/v1/extract", json=NOTE).json()         # 2000 >= 1500 after this call
    third = cl.post("/api/v1/extract", json=NOTE).json()
    assert first["notice"] is None and second["notice"] is None
    assert third["fallback_used"] is True and third["usage"] is None and "paused for today" in third["notice"]
    assert [c["code"] for c in third["codes"]] == ["77066"]       # still useful: keyword matcher found it


def test_budget_exhausted_does_not_call_the_llm_at_all():
    fake = FakeGemini(gem_with_usage([]))
    cl, _ = client(100, GeminiCodeExtractor(Repo(), client=fake), spent=100)
    cl.post("/api/v1/extract", json=NOTE)
    assert fake.calls == []


def test_image_reading_is_paused_when_budget_spent():
    tr = FakeTranscriber()
    cl, _ = client(100, transcriber=tr, spent=100)
    r = cl.post("/api/v1/extract/image", files={"file": ("n.png", PNG, "image/png")})
    assert r.status_code == 503 and "budget" in r.json()["detail"] and tr.calls == []


def test_zero_budget_means_unlimited():
    cl, c = client(0, spent=10**9)
    assert c.budget_exceeded() is False
    assert cl.post("/api/v1/extract", json=NOTE).json()["notice"] is None


def test_estimates_keep_working_when_ai_budget_is_spent():
    cl, _ = client(1, spent=10)
    assert cl.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"}).status_code == 200


def test_usage_endpoint_reports_budget():
    cl, c = client(5000, spent=1200)
    j = cl.get("/api/v1/usage").json()
    assert j["daily_budget"] == {"limit": 5000, "remaining": 3800} and j["today_utc"]["tokens"] == 1200
    cl2, _ = client(0)
    assert cl2.get("/api/v1/usage").json()["daily_budget"] == {"limit": None, "remaining": None}


# ---------------------------------------------------------------- docs toggle
def app_with(docs):
    return TestClient(create_app(container=Container.from_repo(FakeRepo()),
                                 settings=Settings(None, ("*",), docs_enabled=docs)))


def test_docs_available_in_dev():
    c = app_with(True)
    assert c.get("/docs").status_code == 200 and c.get("/openapi.json").status_code == 200


def test_docs_hidden_in_production():
    c = app_with(False)
    assert [c.get(p).status_code for p in ("/docs", "/redoc", "/openapi.json")] == [404, 404, 404]
    assert c.get("/api/v1/hospitals").status_code == 200


# ---------------------------------------------------------------- security headers
def test_api_responses_carry_security_headers():
    h = app_with(True).get("/api/v1/hospitals").headers
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "strict-transport-security" not in h              # only when the proxy says the request was https


def test_hsts_only_behind_https_proxy():
    h = app_with(True).get("/health", headers={"X-Forwarded-Proto": "https"}).headers
    assert "max-age=31536000" in h["strict-transport-security"]


def test_swagger_page_is_not_blocked_by_csp():
    h = app_with(True).get("/docs").headers
    assert "content-security-policy" not in h and h["x-content-type-options"] == "nosniff"


def test_error_responses_also_get_headers():
    h = app_with(True).get("/api/v1/plans").headers          # 422
    assert h["x-content-type-options"] == "nosniff" and "x-request-id" in h


def test_500_responses_also_get_headers_and_request_id():
    class Boom(Container):
        pass
    c = Container.from_repo(FakeRepo())
    c.extractor = SimpleNamespace(name="boom", extract_with_usage=lambda n: 1 / 0)
    cl = TestClient(create_app(container=c), raise_server_exceptions=False)
    r = cl.post("/api/v1/extract", json=NOTE)
    assert r.status_code == 500 and r.headers["x-frame-options"] == "DENY" and r.json()["request_id"]
