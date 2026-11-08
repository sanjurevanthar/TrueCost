import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container
from app.main import create_app
from app.security import RateLimiter
from app.sessions import InvalidSession, SessionManager
from app.services.extraction import TokenUsage
from tests.conftest import FakeRepo

NOTE = {"note": "Order a bilateral diagnostic mammogram."}


# ---------------------------------------------------------------- SessionManager
def test_issue_and_verify_roundtrip():
    sm = SessionManager("secret-secret-secret", 3600)
    token, s = sm.issue()
    assert sm.verify(token) == s and len(s.sid) == 16 and s.expires_at - s.issued_at == 3600


def test_expired_token_is_rejected():
    now = [1000.0]
    sm = SessionManager("k" * 32, 60, clock=lambda: now[0])
    token, _ = sm.issue()
    now[0] += 59
    sm.verify(token)
    now[0] += 2
    with pytest.raises(InvalidSession) as e:
        sm.verify(token)
    assert e.value.reason == "expired"


def test_tampered_payload_or_signature_rejected():
    sm = SessionManager("k" * 32, 60)
    token, _ = sm.issue()
    payload, sig = token.split(".")
    for bad in (payload + "." + sig[:-2] + "AA", payload[:-2] + "AA." + sig, "x." + sig, payload + "."):
        with pytest.raises(InvalidSession):
            sm.verify(bad)


def test_token_from_another_secret_rejected():
    token, _ = SessionManager("a" * 32, 60).issue()
    with pytest.raises(InvalidSession) as e:
        SessionManager("b" * 32, 60).verify(token)
    assert e.value.reason == "bad_signature"


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "." , "x" * 600, "!!!.???", None])
def test_garbage_is_malformed_not_a_crash(junk):
    with pytest.raises(InvalidSession):
        SessionManager("k" * 32).verify(junk)


def test_ephemeral_secret_flag_and_uniqueness():
    a, b = SessionManager(), SessionManager()
    assert a.ephemeral_secret and not SessionManager("x" * 32).ephemeral_secret
    t, _ = a.issue()
    with pytest.raises(InvalidSession):
        b.verify(t)                         # different random secrets


def test_each_session_gets_a_new_id():
    sm = SessionManager("k" * 32)
    assert sm.issue()[1].sid != sm.issue()[1].sid


# ---------------------------------------------------------------- HTTP flow
def app(require=True, token=None, limit=0, session_limit=0, ttl=3600, secret="s" * 32):
    c = Container.from_repo(FakeRepo())
    c.require_session, c.access_token = require, token
    c.sessions = SessionManager(secret, ttl)
    c.limiter, c.session_limiter = RateLimiter(limit), RateLimiter(session_limit)
    return TestClient(create_app(container=c))


def mint(c):
    r = c.post("/api/v1/session")
    assert r.status_code == 200
    return r.json()


def test_session_endpoint_shape():
    j = mint(app(ttl=900))
    assert j["token_type"] == "Bearer" and j["expires_in"] == 900 and j["token"].count(".") == 1


def test_ai_endpoint_needs_a_session_when_required():
    r = app().post("/api/v1/extract", json=NOTE)
    assert r.status_code == 401 and "POST /api/v1/session" in r.json()["detail"]


def test_session_unlocks_extract_and_image_endpoints():
    c = app()
    h = {"Authorization": f"Bearer {mint(c)['token']}"}
    assert c.post("/api/v1/extract", json=NOTE, headers=h).status_code == 200
    r = c.post("/api/v1/extract/image", files={"file": ("n.png", b"x", "image/png")}, headers=h)
    assert r.status_code == 503 and "GEMINI" in r.json()["detail"]      # got past auth; no Gemini key in this fixture


def test_expired_session_gets_401_telling_client_to_remint():
    now = [time.time()]
    c = Container.from_repo(FakeRepo())
    c.require_session, c.sessions = True, SessionManager("s" * 32, 60, clock=lambda: now[0])
    cl = TestClient(create_app(container=c))
    h = {"Authorization": f"Bearer {mint(cl)['token']}"}
    now[0] += 120
    r = cl.post("/api/v1/extract", json=NOTE, headers=h)
    assert r.status_code == 401 and "expired" in r.json()["detail"].lower()


def test_bad_bearer_is_401_even_when_auth_not_enforced():
    r = app(require=False).post("/api/v1/extract", json=NOTE, headers={"Authorization": "Bearer nonsense"})
    assert r.status_code == 401


def test_dev_mode_without_requirement_stays_open():
    assert app(require=False).post("/api/v1/extract", json=NOTE).status_code == 200


def test_api_key_still_works_alongside_sessions():
    c = app(token="adminkey")
    assert c.post("/api/v1/extract", json=NOTE, headers={"X-API-Key": "adminkey"}).status_code == 200
    assert c.post("/api/v1/extract", json=NOTE, headers={"X-API-Key": "nope"}).status_code == 401
    assert c.post("/api/v1/extract", json=NOTE).status_code == 401


def test_per_session_limit_is_separate_from_per_ip():
    c = app(limit=2)
    a = {"Authorization": f"Bearer {mint(c)['token']}"}
    b = {"Authorization": f"Bearer {mint(c)['token']}"}
    codes = [c.post("/api/v1/extract", json=NOTE, headers=a).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    # same IP, different session: the IP bucket (2 per minute) is already used up, so B is limited too
    assert c.post("/api/v1/extract", json=NOTE, headers=b).status_code == 429


def test_one_session_cannot_dodge_the_ip_limit_by_rotating_ips():
    c = app(limit=2)
    h = {"Authorization": f"Bearer {mint(c)['token']}"}
    rs = [c.post("/api/v1/extract", json=NOTE, headers={**h, "X-Forwarded-For": f"9.9.9.{i}"}).status_code
          for i in range(3)]
    assert rs == [200, 200, 429]            # the session bucket still catches it


def test_minting_sessions_is_rate_limited():
    c = app(session_limit=2)
    assert [c.post("/api/v1/session").status_code for _ in range(3)] == [200, 200, 429]


def test_reference_and_estimate_endpoints_need_no_session():
    c = app()
    assert c.get("/api/v1/hospitals").status_code == 200
    assert c.post("/api/v1/estimate", json={"codes": ["77066"], "mode": "self_pay"}).status_code == 200


def test_session_tokens_never_appear_in_logs(caplog):
    caplog.set_level("DEBUG")
    c = app()
    tok = mint(c)["token"]
    c.post("/api/v1/extract", json=NOTE, headers={"Authorization": f"Bearer {tok}"})
    assert tok not in caplog.text and tok.split(".")[1] not in caplog.text


# ---------------------------------------------------------------- admin /usage
def test_usage_is_admin_only_in_production():
    c = app(token="adminkey")
    sess = {"Authorization": f"Bearer {mint(c)['token']}"}
    assert c.get("/api/v1/usage").status_code == 401
    assert c.get("/api/v1/usage", headers=sess).status_code == 401            # a browser session is not enough
    assert c.get("/api/v1/usage", headers={"X-API-Key": "adminkey"}).status_code == 200


def test_usage_disabled_when_prod_but_no_admin_token():
    assert app(token=None).get("/api/v1/usage").status_code == 403


def test_usage_open_in_plain_dev_mode():
    assert app(require=False, token=None).get("/api/v1/usage").status_code == 200


# ---------------------------------------------------------------- settings
def test_settings_session_env(monkeypatch):
    monkeypatch.setenv("TRUECOST_NO_DOTENV", "1")
    s = Settings.from_env()
    assert (s.require_session, s.session_ttl_min, s.daily_token_budget, s.docs_enabled) == (False, 60, 0, True)
    for k, v in {"REQUIRE_SESSION": "true", "SESSION_SECRET": "abc", "SESSION_TTL_MIN": "15",
                 "DAILY_TOKEN_BUDGET": "5000", "DOCS_ENABLED": "false", "SESSION_RATE_LIMIT_PER_MIN": "5"}.items():
        monkeypatch.setenv(k, v)
    s = Settings.from_env()
    assert (s.require_session, s.session_secret, s.session_ttl_min, s.daily_token_budget, s.docs_enabled,
            s.session_rate_limit_per_min) == (True, "abc", 15, 5000, False, 5)
