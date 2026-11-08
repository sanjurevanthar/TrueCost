import asyncio
import time

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.services.extraction import TokenUsage
from app.services.transcription import NoteTranscriber
from tests.conftest import FakeRepo
from tests.test_extract_image import PNG


# ---------------------------------------------------------------- the event loop must never be blocked by an LLM call
class SlowReader(NoteTranscriber):
    name = "slow"

    def transcribe(self, data, content_type):
        time.sleep(0.6)                                   # stands in for a slow Gemini vision call
        return "bilateral diagnostic mammogram", TokenUsage(1, 1, 2)


def test_slow_image_read_does_not_freeze_other_requests():
    app = create_app(container=Container.from_repo(FakeRepo(), transcriber=SlowReader()))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            upload = asyncio.create_task(c.post("/api/v1/extract/image", files={"file": ("n.png", PNG, "image/png")}))
            await asyncio.sleep(0.15)                      # the slow read is now in flight
            t0 = time.perf_counter()
            health = await c.get("/health")
            waited = time.perf_counter() - t0
            return health.status_code, waited, await upload

    status, waited, upload = asyncio.run(scenario())
    assert status == 200 and waited < 0.3, f"/health had to wait {waited:.2f}s behind an image read"
    assert upload.status_code == 200 and [x["code"] for x in upload.json()["codes"]] == ["77066"]


def test_image_endpoint_still_returns_everything_after_the_threadpool_change():
    c = TestClient(create_app(container=Container.from_repo(FakeRepo(), transcriber=SlowReader())))
    j = c.post("/api/v1/extract/image", files={"file": ("n.png", PNG, "image/png")}).json()
    assert j["transcript"] == "bilateral diagnostic mammogram" and j["usage"]["total_tokens"] == 4 or j["usage"] is not None
    assert c.get("/api/v1/usage").json()["by_operation"]["read_image"]["calls"] == 1


# ---------------------------------------------------------------- liveness vs readiness
def test_health_is_cheap_and_reports_version():
    j = TestClient(create_app(container=Container.from_repo(FakeRepo()))).get("/health").json()
    assert j["status"] == "ok" and j["version"]


def test_ready_ok_when_database_answers():
    j = TestClient(create_app(container=Container.from_repo(FakeRepo()))).get("/ready").json()
    assert j["status"] == "ready" and j["hospitals"] == 3


def test_ready_is_503_when_database_is_broken():
    class Broken(FakeRepo):
        def list_hospitals(self):
            raise RuntimeError("disk I/O error")
    r = TestClient(create_app(container=Container.from_repo(Broken()))).get("/ready")
    assert r.status_code == 503 and r.json()["status"] == "unavailable"


def test_ready_is_503_when_database_is_empty():
    class Empty(FakeRepo):
        HOSPITALS = []
    r = TestClient(create_app(container=Container.from_repo(Empty()))).get("/ready")
    assert r.status_code == 503 and "empty" in r.json()["detail"]


# ---------------------------------------------------------------- CORS
def app_with(**kw):
    return TestClient(create_app(container=Container.from_repo(FakeRepo()), settings=Settings(None, **kw)))


def preflight(c, origin):
    return c.options("/api/v1/extract", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "authorization,content-type"})


def test_regex_allows_lovable_previews_but_not_other_sites():
    c = app_with(cors_origins=("https://myapp.com",), cors_origin_regex=r"https://.*\.lovable\.app")
    ok = preflight(c, "https://id-preview--abc123.lovable.app")
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://id-preview--abc123.lovable.app"
    assert preflight(c, "https://myapp.com").headers["access-control-allow-origin"] == "https://myapp.com"
    assert "access-control-allow-origin" not in preflight(c, "https://evil.com").headers
    assert "access-control-allow-origin" not in preflight(c, "https://lovable.app.evil.com").headers


def test_authorization_header_is_allowed_cross_origin():
    c = app_with(cors_origins=("https://a.com",))
    assert "authorization" in preflight(c, "https://a.com").headers["access-control-allow-headers"].lower()


def test_cors_wildcard_warns_only_in_container_mode(caplog):
    caplog.set_level("WARNING")
    app_with(cors_origins=("*",), log_format="text")
    assert "CORS_ORIGINS is '*'" not in caplog.text
    app_with(cors_origins=("*",), log_format="json")
    assert "CORS_ORIGINS is '*'" in caplog.text


def test_cors_regex_from_env(monkeypatch):
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    monkeypatch.setenv("CORS_ORIGIN_REGEX", r"https://.*\.lovable\.app")
    assert Settings.from_env().cors_origin_regex == r"https://.*\.lovable\.app"
    monkeypatch.delenv("CORS_ORIGIN_REGEX")
    assert Settings.from_env().cors_origin_regex is None
