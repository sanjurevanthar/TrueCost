import pytest
from fastapi.testclient import TestClient

from app.container import Container
from app.exceptions import TranscriptionFailedError
from app.main import create_app
from app.services.extraction import TokenUsage
from app.services.transcription import MAX_BYTES, NoteTranscriber
from tests.conftest import FakeRepo

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 50


class FakeTranscriber(NoteTranscriber):
    name = "fake-vision"

    def __init__(self, text="Plan: bilateral diagnostic mammogram.", error=None):
        self.text, self.error, self.calls = text, error, []

    def transcribe(self, data, content_type):
        self.calls.append((len(data), content_type))
        if self.error:
            raise self.error
        return self.text, TokenUsage(300, 120, 420)


def client(tr):
    return TestClient(create_app(container=Container.from_repo(FakeRepo(), transcriber=tr)))


def up(c, data=PNG, ctype="image/png", name="note.png"):
    return c.post("/api/v1/extract/image", files={"file": (name, data, ctype)})


def test_image_is_read_then_codes_extracted():
    tr = FakeTranscriber()
    r = up(client(tr))
    assert r.status_code == 200
    j = r.json()
    assert j["transcript"] == "Plan: bilateral diagnostic mammogram." and j["reader"] == "fake-vision"
    assert [c["code"] for c in j["codes"]] == ["77066"]
    assert tr.calls == [(len(PNG), "image/png")]


@pytest.mark.parametrize("ctype", ["image/jpeg", "image/webp", "application/pdf"])
def test_other_allowed_types(ctype):
    assert up(client(FakeTranscriber()), ctype=ctype).status_code == 200


def test_unsupported_type_415_and_no_transcribe_call():
    tr = FakeTranscriber()
    r = up(client(tr), data=b"hello", ctype="text/plain", name="n.txt")
    assert r.status_code == 415 and "Unsupported" in r.json()["detail"] and tr.calls == []


def test_empty_file_422():
    assert up(client(FakeTranscriber()), data=b"").status_code == 422


def test_too_large_413():
    assert up(client(FakeTranscriber()), data=b"0" * (MAX_BYTES + 1)).status_code == 413


def test_missing_file_422():
    assert client(FakeTranscriber()).post("/api/v1/extract/image").status_code == 422


def test_no_gemini_key_gives_clear_503():
    r = up(client(None))
    assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"]


def test_read_failure_is_502_with_hint():
    r = up(client(FakeTranscriber(error=TranscriptionFailedError("timeout"))))
    assert r.status_code == 502 and "paste the text" in r.json()["detail"]


def test_unreadable_image_returns_empty_codes_not_error():
    j = up(client(FakeTranscriber(text=""))).json()
    assert j["transcript"] == "" and j["codes"] == []


def test_swagger_shows_file_upload_field():
    spec = client(None).get("/openapi.json").json()
    body = spec["paths"]["/api/v1/extract/image"]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in body
