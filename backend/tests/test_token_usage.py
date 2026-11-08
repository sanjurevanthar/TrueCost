"""Token accounting for the AI features: per-call usage in responses, GET /usage totals, fallback flag."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.main import create_app
from app.services.claude_extractor import ClaudeCodeExtractor
from app.services.extraction import ExtractionResult, KeywordCodeExtractor, TokenUsage
from app.services.gemini_extractor import GeminiCodeExtractor, gemini_usage
from app.usage import UsageTracker
from tests.conftest import FakeRepo
from tests.test_claude_extractor import FakeClient, Repo, item
from tests.test_extract_image import PNG, FakeTranscriber
from tests.test_gemini_extractor import FakeGemini, gem_reply

NOTE = "Plan: bilateral diagnostic mammogram."


def claude_reply(items, inp=1000, out=200):
    return SimpleNamespace(stop_reason="end_turn", usage=SimpleNamespace(input_tokens=inp, output_tokens=out),
                           content=[SimpleNamespace(type="text", text=json.dumps({"codes": items}))])


def gem_with_usage(items, prompt=800, cand=50, thoughts=150, total=1000):
    r = gem_reply(items)
    r.usage_metadata = SimpleNamespace(prompt_token_count=prompt, candidates_token_count=cand,
                                       thoughts_token_count=thoughts, total_token_count=total)
    return r


# ---------------------------------------------------------------- reading usage from providers
def test_gemini_usage_adds_thinking_tokens_to_output():
    u = gemini_usage(gem_with_usage([], prompt=800, cand=50, thoughts=150, total=1000))
    assert (u.input_tokens, u.output_tokens, u.total_tokens) == (800, 200, 1000)


def test_gemini_usage_handles_missing_fields():
    r = gem_reply([])
    assert gemini_usage(r) is None
    r.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=None,
                                       thoughts_token_count=None, total_token_count=None)
    assert gemini_usage(r) == TokenUsage(10, 0, 10)


def test_claude_extractor_reports_usage():
    ex = ClaudeCodeExtractor(Repo(), client=FakeClient(claude_reply([item("77066", "bilateral diagnostic mammogram")])))
    res = ex.extract_with_usage(NOTE)
    assert res.usage == TokenUsage(1000, 200, 1200) and res.fallback_used is False
    assert [c.code for c in res.codes] == ["77066"]


def test_gemini_extractor_reports_usage():
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    assert ex.extract_with_usage(NOTE).usage == TokenUsage(800, 200, 1000)


def test_extract_still_returns_plain_list():
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    assert [c.code for c in ex.extract(NOTE)] == ["77066"]


def test_keyword_extractor_has_no_usage():
    res = KeywordCodeExtractor(Repo()).extract_with_usage(NOTE)
    assert res.usage is None and res.fallback_used is False


def test_blank_note_costs_nothing():
    c = FakeGemini(gem_with_usage([]))
    res = GeminiCodeExtractor(Repo(), client=c).extract_with_usage("  ")
    assert res == ExtractionResult([]) and c.calls == []


def test_fallback_after_api_call_still_counts_the_tokens():
    bad = gem_with_usage([])
    bad.text = "not json"
    res = GeminiCodeExtractor(Repo(), client=FakeGemini(bad)).extract_with_usage("Order a bilateral diagnostic mammogram.")
    assert res.fallback_used is True and res.usage == TokenUsage(800, 200, 1000)
    assert [c.code for c in res.codes] == ["77066"]


def test_fallback_before_any_tokens_has_no_usage():
    res = GeminiCodeExtractor(Repo(), client=FakeGemini(error=TimeoutError())).extract_with_usage(
        "Order a bilateral diagnostic mammogram.")
    assert res.fallback_used is True and res.usage is None


# ---------------------------------------------------------------- tracker
def test_tracker_accumulates_per_operation():
    t = UsageTracker()
    t.record("extract", TokenUsage(100, 20, 120))
    t.record("extract", TokenUsage(50, 10, 60), fallback_used=True)
    t.record("read_image", TokenUsage(300, 100, 400))
    t.record("extract", None)           # keyword answer: a call, but no tokens
    s = t.snapshot()
    assert s["by_operation"]["extract"] == {"calls": 3, "fallbacks": 1, "input_tokens": 150,
                                            "output_tokens": 30, "total_tokens": 180}
    assert s["total"] == {"calls": 4, "fallbacks": 1, "input_tokens": 450, "output_tokens": 130, "total_tokens": 580}


def test_tracker_is_thread_safe():
    import threading
    t = UsageTracker()
    ts = [threading.Thread(target=lambda: [t.record("x", TokenUsage(1, 1, 2)) for _ in range(500)]) for _ in range(8)]
    [x.start() for x in ts]
    [x.join() for x in ts]
    assert t.snapshot()["total"]["total_tokens"] == 8 * 500 * 2


# ---------------------------------------------------------------- over HTTP
def app_with(extractor, transcriber=None):
    return TestClient(create_app(container=Container.from_repo(Repo(), extractor=extractor, transcriber=transcriber)))


def test_extract_response_includes_usage_and_usage_endpoint_totals():
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    c = app_with(ex)
    for _ in range(2):
        r = c.post("/api/v1/extract", json={"note": NOTE}).json()
    assert r["usage"] == {"input_tokens": 800, "output_tokens": 200, "total_tokens": 1000}
    assert r["fallback_used"] is False
    u = c.get("/api/v1/usage").json()
    assert u["total"]["total_tokens"] == 2000 and u["by_operation"]["extract"]["calls"] == 2


def test_extract_reports_fallback_flag():
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(error=TimeoutError()))
    r = app_with(ex).post("/api/v1/extract", json={"note": "Order a bilateral diagnostic mammogram."}).json()
    assert r["fallback_used"] is True and r["usage"] is None and [c["code"] for c in r["codes"]] == ["77066"]


def test_keyword_mode_usage_is_null_and_totals_zero():
    c = app_with(KeywordCodeExtractor(Repo()))
    assert c.post("/api/v1/extract", json={"note": NOTE}).json()["usage"] is None
    assert c.get("/api/v1/usage").json()["total"]["total_tokens"] == 0


def test_image_endpoint_sums_reading_and_extraction_tokens():
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    c = app_with(ex, FakeTranscriber(text=NOTE))       # FakeTranscriber reports 300 in / 120 out / 420 total
    r = c.post("/api/v1/extract/image", files={"file": ("n.png", PNG, "image/png")}).json()
    assert r["usage"] == {"input_tokens": 1100, "output_tokens": 320, "total_tokens": 1420}
    ops = c.get("/api/v1/usage").json()["by_operation"]
    assert ops["read_image"]["total_tokens"] == 420 and ops["extract"]["total_tokens"] == 1000
