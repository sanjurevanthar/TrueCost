"""Thin HTTP layer: validate input, call a service, shape the output. No business logic here."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from ..container import Container
from ..domain.models import Payer
from ..exceptions import (AIBudgetExceededError, EmptyUploadError, ImageTooLargeError, TranscriptionUnavailableError,
                          UnsupportedImageError)
from ..security import authenticate, client_key, require_admin
from ..services.extraction import ExtractionResult
from ..services.transcription import ALLOWED_TYPES, MAX_BYTES
from ..schemas import (CodeOut, EstimateRequest, EstimateResponse, ExtractedCodeOut, ExtractRequest,
                       ExtractImageResponse, ExtractResponse, HospitalOut, SessionOut, TokenUsageOut)

router = APIRouter(prefix="/api/v1")
log = logging.getLogger(__name__)


def _usage_out(u):
    return None if u is None else TokenUsageOut(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                                                total_tokens=u.total_tokens)


def get_container(request: Request) -> Container:
    return request.app.state.container


def guard_ai(request: Request, c: Container = Depends(get_container)):
    """Who is calling (session / API key) + rate limits (per IP and per session) for endpoints that spend LLM tokens."""
    ident = authenticate(request, c.sessions, c.access_token, c.require_session)
    c.limiter.check(client_key(request))
    if ident:
        c.limiter.check(ident, count_global=False)   # one session cannot hog the shared allowance either


@router.get("/codes", response_model=List[CodeOut], tags=["reference"])
def list_codes(phase: Optional[str] = None, c: Container = Depends(get_container)):
    """Billing codes the app supports. phase: diagnostic | treatment | diagnosis."""
    return [CodeOut(**vars(x)) for x in c.repo.list_codes(phase)]


@router.get("/hospitals", response_model=List[HospitalOut], tags=["reference"])
def list_hospitals(c: Container = Depends(get_container)):
    return [HospitalOut(hospital=h.name, address=h.address, lat=h.lat, lng=h.lng) for h in c.repo.list_hospitals()]


@router.get("/plans", response_model=List[str], tags=["reference"])
def list_plans(payer: Payer, c: Container = Depends(get_container)):
    """Plan names found in the price files for an insurer (for a dropdown)."""
    return c.repo.list_plans(payer.value)


@router.post("/extract", response_model=ExtractResponse, tags=["ai"], dependencies=[Depends(guard_ai)])
def extract_codes(body: ExtractRequest, c: Container = Depends(get_container)):
    """Suggest billing codes from a doctor's note. The patient must confirm them."""
    notice = None
    if c.budget_exceeded():
        log.warning("daily token budget reached: answering with the basic matcher",
                    extra={"budget": c.daily_token_budget, "used_today": c.usage.tokens_today()})
        res = c.fallback_extractor.extract_with_usage(body.note)
        res = ExtractionResult(res.codes, None, fallback_used=True)
        notice = "AI is paused for today (daily limit reached); suggestions come from a simpler matcher. Please double-check them."
    else:
        res = c.extractor.extract_with_usage(body.note)
    c.usage.record("extract", res.usage, res.fallback_used)
    log.info("extract", extra={"extractor": c.extractor.name, "note_chars": len(body.note),
                              "codes": [x.code for x in res.codes], "fallback_used": res.fallback_used,
                              "tokens": res.usage.total_tokens if res.usage else 0})
    return ExtractResponse(extractor=c.extractor.name, codes=[ExtractedCodeOut(**vars(x)) for x in res.codes],
                           fallback_used=res.fallback_used, usage=_usage_out(res.usage), notice=notice)


@router.post("/estimate", response_model=EstimateResponse, tags=["estimate"])
def estimate(body: EstimateRequest, c: Container = Depends(get_container)):
    """Rank hospitals by the patient's estimated out-of-pocket cost."""
    return c.estimator.estimate(body)


@router.post("/extract/image", response_model=ExtractImageResponse, tags=["ai"], dependencies=[Depends(guard_ai)])
async def extract_codes_from_image(file: UploadFile = File(..., description="Photo, scan or PDF of the doctor's note (png, jpeg, webp, pdf; max 10 MB)"),
                                   c: Container = Depends(get_container)):
    """Read a doctor's note from an image/PDF, then suggest billing codes like /extract."""
    if file.content_type not in ALLOWED_TYPES:
        raise UnsupportedImageError(file.content_type, ALLOWED_TYPES)
    if c.transcriber is None:
        raise TranscriptionUnavailableError()
    if c.budget_exceeded():
        log.warning("daily token budget reached: image reading paused", extra={"budget": c.daily_token_budget})
        raise AIBudgetExceededError()
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImageTooLargeError(MAX_BYTES // (1024 * 1024))
    if not data:
        raise EmptyUploadError()
    log.info("image upload", extra={"content_type": file.content_type, "bytes": len(data), "reader": c.transcriber.name})
    # Both calls are slow, blocking network requests to the LLM provider. Run them in a worker thread so the
    # event loop keeps answering other requests (and /health) meanwhile.
    def read_then_extract():
        text, usage = c.transcriber.transcribe(data, file.content_type)
        c.usage.record("read_image", usage)
        found = c.extractor.extract_with_usage(text) if len(text) >= 3 else ExtractionResult([])
        return text, usage, found

    transcript, read_usage, res = await run_in_threadpool(read_then_extract)
    c.usage.record("extract", res.usage, res.fallback_used)
    total = (read_usage + res.usage) if read_usage and res.usage else (read_usage or res.usage)
    log.info("extract from image", extra={"transcript_chars": len(transcript), "codes": [x.code for x in res.codes],
                                         "fallback_used": res.fallback_used, "tokens": total.total_tokens if total else 0})
    return ExtractImageResponse(extractor=c.extractor.name, reader=c.transcriber.name, transcript=transcript,
                                codes=[ExtractedCodeOut(**vars(x)) for x in res.codes],
                                fallback_used=res.fallback_used, usage=_usage_out(total))


@router.get("/usage", tags=["ai"])
def token_usage(request: Request, c: Container = Depends(get_container)):
    """LLM tokens used (since start, and today in UTC) plus the daily budget. Admin only: needs X-API-Key in production."""
    require_admin(request, c.access_token, c.require_session)
    snap = c.usage.snapshot()
    used = snap["today_utc"]["tokens"]
    snap["daily_budget"] = {"limit": c.daily_token_budget or None,
                            "remaining": max(c.daily_token_budget - used, 0) if c.daily_token_budget else None}
    return snap


@router.post("/session", response_model=SessionOut, tags=["auth"])
def create_session(request: Request, c: Container = Depends(get_container)):
    """Get an anonymous, short-lived token for the AI endpoints. No login, nothing stored.
    Send it as `Authorization: Bearer <token>`; when it expires (401), call this again."""
    c.session_limiter.check(client_key(request))
    token, s = c.sessions.issue()
    log.info("session issued", extra={"sid": s.sid, "ttl_s": c.sessions.ttl})
    return SessionOut(token=token, expires_in=c.sessions.ttl)
