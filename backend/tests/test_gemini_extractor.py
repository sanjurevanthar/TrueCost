import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.container import build_extractor
from app.services.claude_extractor import ClaudeCodeExtractor
from app.services.extraction import KeywordCodeExtractor
from app.services.gemini_extractor import GeminiCodeExtractor
from tests.test_claude_extractor import NOTE, Repo, item


def gem_reply(items=None, text=None, finish="STOP", blocked=None):
    body = text if text is not None else json.dumps({"codes": items or []})
    return SimpleNamespace(
        text=body, prompt_feedback=SimpleNamespace(block_reason=blocked) if blocked else None,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=finish))])


class FakeGemini:
    def __init__(self, response=None, error=None):
        self.calls, self._r, self._e = [], response, error
        self.models = self

    def generate_content(self, **kw):
        self.calls.append(kw)
        if self._e:
            raise self._e
        return self._r


def make(client):
    repo = Repo()
    return GeminiCodeExtractor(repo, client=client, fallback=KeywordCodeExtractor(repo))


def test_valid_response_is_mapped_and_validated():
    c = FakeGemini(gem_reply([item("77066", "bilateral diagnostic mammogram"), item("99999", "x"),
                              item("19083", "not in the note at all")]))
    out = make(c).extract(NOTE)
    assert [(x.code, x.confidence) for x in out] == [("77066", 0.9)]


def test_request_uses_json_schema_and_zero_temperature():
    c = FakeGemini(gem_reply())
    make(c).extract(NOTE)
    kw = c.calls[0]
    cfg = kw["config"]
    assert kw["model"] == "gemini-3.5-flash" and cfg.response_mime_type == "application/json"
    assert cfg.temperature == 0 and "untrusted" in cfg.system_instruction
    assert cfg.response_json_schema["properties"]["codes"]["items"]["properties"]["code"]["enum"] == ["77066", "19083", "582"]
    assert "<note>" in kw["contents"] and "<catalog>" in kw["contents"]


@pytest.mark.parametrize("client", [
    FakeGemini(error=TimeoutError("slow")),
    FakeGemini(gem_reply(finish="SAFETY")),
    FakeGemini(gem_reply(finish="MAX_TOKENS")),
    FakeGemini(gem_reply(blocked="PROHIBITED_CONTENT")),
    FakeGemini(gem_reply(text="not json")),
    FakeGemini(gem_reply(text="")),
    FakeGemini(gem_reply(text='{"codes": "x"}')),
    FakeGemini(SimpleNamespace(text=None, prompt_feedback=None, candidates=[])),
])
def test_any_failure_falls_back_to_keyword(client):
    assert [x.code for x in make(client).extract("Order a bilateral diagnostic mammogram.")] == ["77066"]


def test_blank_note_makes_no_call():
    c = FakeGemini(gem_reply())
    assert make(c).extract("  ") == [] and c.calls == []


def test_name_includes_model():
    assert make(FakeGemini(gem_reply())).name == "gemini:gemini-3.5-flash"


# ---------------------------------------------------------------- selection
def build(monkeypatch, gemini=None, anthropic=None, mode="auto"):
    for k, v in (("GEMINI_API_KEY", gemini), ("ANTHROPIC_API_KEY", anthropic)):
        monkeypatch.setenv(k, v) if v else monkeypatch.delenv(k, raising=False)
    return build_extractor(Repo(), Settings(None, ("*",), extractor=mode))


def test_auto_prefers_gemini_then_claude_then_keyword(monkeypatch):
    assert isinstance(build(monkeypatch, "g", "a"), GeminiCodeExtractor)
    assert isinstance(build(monkeypatch, None, "a"), ClaudeCodeExtractor)
    assert isinstance(build(monkeypatch), KeywordCodeExtractor)


def test_explicit_mode_wins(monkeypatch):
    assert isinstance(build(monkeypatch, "g", "a", mode="claude"), ClaudeCodeExtractor)
    assert isinstance(build(monkeypatch, "g", "a", mode="keyword"), KeywordCodeExtractor)
    assert isinstance(build(monkeypatch, "g", "a", mode="gemini"), GeminiCodeExtractor)


def test_gemini_model_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    assert Settings.from_env().gemini_model == "gemini-3.8-flash"
