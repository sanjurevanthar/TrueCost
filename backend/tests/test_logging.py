import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from app import logging_config as lc
from app.container import Container
from app.main import create_app
from app.security import RateLimiter
from app.services.extraction import KeywordCodeExtractor
from app.services.gemini_extractor import GeminiCodeExtractor
from tests.conftest import FakeRepo
from tests.test_gemini_extractor import FakeGemini
from tests.test_token_usage import gem_with_usage
from tests.test_claude_extractor import Repo, item

SECRET_NOTE = "Patient Jane Roe SSN 123-45-6789 needs a bilateral diagnostic mammogram."


@pytest.fixture
def stream(monkeypatch):
    """Capture what the app's own log handler writes (pytest swaps sys.stdout, so redirect the handler instead)."""
    buf = io.StringIO()
    real_setup = lc.setup_logging

    def setup_and_redirect(level, fmt):
        real_setup(level, fmt)
        for h in logging.getLogger().handlers:
            if getattr(h, lc._HANDLER_TAG, False):
                h.setStream(buf)

    monkeypatch.setattr("app.main.setup_logging", setup_and_redirect)
    yield buf
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, lc._HANDLER_TAG, False):
            root.removeHandler(h)


def client(extractor=None, fmt="json", level="DEBUG", limit=0):
    from app.config import Settings
    c = Container.from_repo(FakeRepo(), extractor=extractor)
    c.limiter = RateLimiter(limit)
    return TestClient(create_app(container=c, settings=Settings(None, ("*",), log_level=level, log_format=fmt)))


def lines(buf):
    return [json.loads(x) for x in buf.getvalue().splitlines() if x.startswith("{")]


def find(buf, msg):
    return [l for l in lines(buf) if l["msg"] == msg]


def test_json_lines_are_valid_and_have_required_fields(stream):
    client().get("/api/v1/hospitals")
    recs = lines(stream)
    assert recs and all({"ts", "level", "logger", "msg", "request_id"} <= set(r) for r in recs)


def test_request_line_has_method_path_status_ms_client(stream):
    client().get("/api/v1/hospitals", headers={"X-Forwarded-For": "9.9.9.9, 1.1.1.1"})
    r = find(stream, "request")[-1]
    assert (r["method"], r["path"], r["status"], r["client"]) == ("GET", "/api/v1/hospitals", 200, "9.9.9.9")
    assert isinstance(r["ms"], float)


def test_request_id_is_generated_echoed_and_shared_by_all_lines_of_the_request(stream):
    c = client()
    r = c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"})
    rid = r.headers["x-request-id"]
    assert rid and rid != "-"
    mine = [l for l in lines(stream) if l["request_id"] == rid]
    assert {"estimate", "request"} <= {l["msg"] for l in mine}      # service log + middleware log, same id


def test_incoming_request_id_is_honoured(stream):
    r = client().get("/api/v1/hospitals", headers={"X-Request-ID": "trace-abc"})
    assert r.headers["x-request-id"] == "trace-abc"
    assert find(stream, "request")[-1]["request_id"] == "trace-abc"


def test_health_checks_only_log_at_debug(stream):
    client(level="INFO").get("/health")
    assert not find(stream, "request")
    client(level="DEBUG").get("/health")
    assert find(stream, "request")


def test_startup_log_reports_config_booleans_but_never_key_values(stream, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyDUMMY-key-value-1234567890")
    client()
    start = find(stream, "startup")[0]
    assert start["gemini_key_set"] is True and "extractor" in start and "db_exists" in start
    assert "AIzaSyDUMMY" not in stream.getvalue()


def test_secrets_are_redacted_even_if_they_leak_into_a_message(stream, monkeypatch):
    monkeypatch.setenv("API_ACCESS_TOKEN", "super-secret-token-xyz")
    client()
    logging.getLogger("app.test").warning("oops token=super-secret-token-xyz in a message")
    out = stream.getvalue()
    assert "super-secret-token-xyz" not in out and "token=***" in out


def test_note_text_and_pii_are_never_logged_even_at_debug(stream):
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    r = client(ex).post("/api/v1/extract", json={"note": SECRET_NOTE})
    assert r.status_code == 200
    out = stream.getvalue()
    for leaked in ("Jane Roe", "123-45-6789", "needs a bilateral"):
        assert leaked not in out
    ex_log = find(stream, "extract")[0]
    assert ex_log["note_chars"] == len(SECRET_NOTE) and ex_log["codes"] == ["77066"]


def test_llm_call_logs_tokens_and_timing(stream):
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(gem_with_usage([item("77066", "bilateral diagnostic mammogram")])))
    client(ex).post("/api/v1/extract", json={"note": SECRET_NOTE})
    ok = find(stream, "llm call ok")[0]
    assert ok["in_tokens"] == 800 and ok["out_tokens"] == 200 and ok["total_tokens"] == 1000 and "ms" in ok


def test_llm_failure_is_logged_as_warning_and_marks_fallback(stream):
    ex = GeminiCodeExtractor(Repo(), client=FakeGemini(error=TimeoutError("slow")))
    client(ex).post("/api/v1/extract", json={"note": SECRET_NOTE})
    w = [l for l in lines(stream) if l["level"] == "WARNING" and "extraction failed" in l["msg"]]
    assert w and "TimeoutError" in w[0]["msg"]
    assert find(stream, "extract")[0]["fallback_used"] is True


def test_domain_errors_logged_as_warning_without_traceback(stream):
    client().post("/api/v1/estimate", json={"codes": ["99999"], "mode": "self_pay"})
    unk = find(stream, "unknown codes requested")[0]
    assert unk["codes"] == ["99999"] and unk["level"] == "WARNING"
    rej = [l for l in lines(stream) if l["msg"].startswith("rejected")][0]
    assert rej["status"] == 404 and "exc" not in rej
    assert find(stream, "request")[-1]["level"] == "WARNING"


def test_validation_errors_log_field_names_not_values(stream):
    client().post("/api/v1/estimate", json={"codes": ["77066"], "mode": "insured",
                                            "insurance": {"payer": "Univera", "deductible_remaining": 1,
                                                          "coinsurance_pct": 777, "oop_max_remaining": 1}})
    v = find(stream, "invalid request")[0]
    assert any("coinsurance_pct" in e["loc"] for e in v["errors"])
    assert "777" not in stream.getvalue()


def test_rate_limit_and_auth_failures_are_logged(stream):
    c = client(limit=1)
    c.post("/api/v1/extract", json={"note": "bilateral diagnostic mammogram"})
    c.post("/api/v1/extract", json={"note": "bilateral diagnostic mammogram"})
    assert find(stream, "rate limit hit")[0]["scope"] == "client"
    c2 = Container.from_repo(FakeRepo())
    c2.access_token = "tok"
    from app.config import Settings
    TestClient(create_app(container=c2, settings=Settings(None, ("*",), log_format="json"))).post(
        "/api/v1/extract", json={"note": "abc"})
    assert find(stream, "bad or missing X-API-Key")


def test_unhandled_exception_returns_500_with_request_id_and_logs_traceback(stream):
    class Boom(KeywordCodeExtractor):
        def extract_with_usage(self, note):
            raise RuntimeError("kaboom")
    app_client = client(Boom(FakeRepo()))
    app_client = TestClient(app_client.app, raise_server_exceptions=False)
    r = app_client.post("/api/v1/extract", json={"note": "abc def"})
    assert r.status_code == 500 and r.json()["request_id"] == r.headers["x-request-id"]
    err = find(stream, "unhandled error")[0]
    assert err["level"] == "ERROR" and "kaboom" in err["exc"] and err["path"] == "/api/v1/extract"
    assert "abc def" not in stream.getvalue()


def test_text_format_is_human_readable(stream):
    client(fmt="text").get("/api/v1/hospitals")
    line = next(l for l in stream.getvalue().splitlines() if "request" in l and "/api/v1/hospitals" in l)
    assert "INFO" in line and "app.request" in line and "status=200" in line and not line.startswith("{")


def test_setup_is_idempotent_no_duplicate_lines(stream):
    for _ in range(3):
        client()
    stream.truncate(0); stream.seek(0)
    client().get("/api/v1/hospitals")
    assert len(find(stream, "request")) == 1


def test_log_level_filters(stream):
    client(level="WARNING").get("/api/v1/hospitals")
    assert not find(stream, "request")


def test_settings_log_env(monkeypatch):
    from app.config import Settings
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    monkeypatch.setenv("LOG_FORMAT", "JSON")
    s = Settings.from_env()
    assert (s.log_level, s.log_format) == ("DEBUG", "json")


@pytest.mark.parametrize("bad", ["x" * 65, "a b", "id\x1b[31mred", "../../etc/passwd", "id;drop", '{"a":1}', ""])
def test_unsafe_incoming_request_ids_are_replaced(stream, bad):
    r = client().get("/api/v1/hospitals", headers={"X-Request-ID": bad})
    rid = r.headers["x-request-id"]
    assert rid != bad and len(rid) == 12 and rid.isalnum()
    assert find(stream, "request")[-1]["request_id"] == rid
