"""Thin HTTP layer: validate input, call a service, shape the output. No business logic here."""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Request

from ..container import Container
from ..domain.models import Payer
from ..schemas import (CodeOut, EstimateRequest, EstimateResponse, ExtractedCodeOut, ExtractRequest,
                       ExtractResponse, HospitalOut)

router = APIRouter(prefix="/api/v1")


def get_container(request: Request) -> Container:
    return request.app.state.container


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


@router.post("/extract", response_model=ExtractResponse, tags=["ai"])
def extract_codes(body: ExtractRequest, c: Container = Depends(get_container)):
    """Suggest billing codes from a doctor's note. The patient must confirm them."""
    found = c.extractor.extract(body.note)
    return ExtractResponse(extractor=c.extractor.name, codes=[ExtractedCodeOut(**vars(x)) for x in found])


@router.post("/estimate", response_model=EstimateResponse, tags=["estimate"])
def estimate(body: EstimateRequest, c: Container = Depends(get_container)):
    """Rank hospitals by the patient's estimated out-of-pocket cost."""
    return c.estimator.estimate(body)
