"""Thin HTTP layer: validate input, call a service, shape the output. No business logic here."""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile

from ..container import Container
from ..domain.models import Payer
from ..exceptions import (EmptyUploadError, ImageTooLargeError, TranscriptionUnavailableError,
                          UnsupportedImageError)
from ..security import check_token, client_key
from ..services.extraction import ExtractionResult
from ..services.transcription import ALLOWED_TYPES, MAX_BYTES
from ..schemas import (CodeOut, EstimateRequest, EstimateResponse, ExtractedCodeOut, ExtractRequest,
                       ExtractImageResponse, ExtractResponse, HospitalOut, TokenUsageOut)

router = APIRouter(prefix="/api/v1")
log = logging.getLogger(__name__)


def _usage_out(u):
    return None if u is None else TokenUsageOut(input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                                                total_tokens=u.total_tokens)


def get_container(request: Request) -> Container:
    return request.app.state.container


def guard_ai(request: Request, c: Container = Depends(get_container)):
    """Access token (if configured) + per-client rate limit for endpoints that spend LLM tokens."""
    check_token(request, c.access_token)
    c.limiter.check(client_key(request))


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
    res = c.extractor.extract_with_usage(body.note)
    c.usage.record("extract", res.usage, res.fallback_used)
    log.info("extract", extra={"extractor": c.extractor.name, "note_chars": len(body.note),
                              "codes": [x.code for x in res.codes], "fallback_used": res.fallback_used,
                              "tokens": res.usage.total_tokens if res.usage else 0})
    return ExtractResponse(extractor=c.extractor.name, codes=[ExtractedCodeOut(**vars(x)) for x in res.codes],
                           fallback_used=res.fallback_used, usage=_usage_out(res.usage))


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
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImageTooLargeError(MAX_BYTES // (1024 * 1024))
    if not data:
        raise EmptyUploadError()
    log.info("image upload", extra={"content_type": file.content_type, "bytes": len(data), "reader": c.transcriber.name})
    transcript, read_usage = c.transcriber.transcribe(data, file.content_type)
    c.usage.record("read_image", read_usage)
    res = c.extractor.extract_with_usage(transcript) if len(transcript) >= 3 else ExtractionResult([])
    c.usage.record("extract", res.usage, res.fallback_used)
    total = (read_usage + res.usage) if read_usage and res.usage else (read_usage or res.usage)
    log.info("extract from image", extra={"transcript_chars": len(transcript), "codes": [x.code for x in res.codes],
                                         "fallback_used": res.fallback_used, "tokens": total.total_tokens if total else 0})
    return ExtractImageResponse(extractor=c.extractor.name, reader=c.transcriber.name, transcript=transcript,
                                codes=[ExtractedCodeOut(**vars(x)) for x in res.codes],
                                fallback_used=res.fallback_used, usage=_usage_out(total))


@router.get("/usage", tags=["ai"])
def token_usage(c: Container = Depends(get_container)):
    """LLM tokens used since the server started (resets on restart), per operation."""
    return c.usage.snapshot()
