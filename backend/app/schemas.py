"""API contracts (Pydantic). These are what the frontend and AI teammates code against."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .domain.models import CoverageMode, Payer


class InsuranceIn(BaseModel):
    payer: Payer
    plan: Optional[str] = Field(None, description="Optional plan name hint, e.g. 'PPO'. Narrows the rate used.")
    deductible_remaining: float = Field(..., ge=0, description="USD still to meet this year")
    coinsurance_pct: float = Field(..., ge=0, le=100, description="Patient's share after deductible, %")
    oop_max_remaining: float = Field(..., ge=0, description="USD left until out-of-pocket max")


class EstimateRequest(BaseModel):
    codes: List[str] = Field(..., min_length=1, max_length=20,
                             description="CPT/MS-DRG codes in the order services will happen (order uses up the deductible)")
    mode: CoverageMode = CoverageMode.INSURED
    insurance: Optional[InsuranceIn] = None
    patient_lat: Optional[float] = Field(None, ge=-90, le=90)
    patient_lng: Optional[float] = Field(None, ge=-180, le=180)

    @field_validator("codes")
    @classmethod
    def _clean(cls, v: List[str]) -> List[str]:
        out = list(dict.fromkeys(c.strip() for c in v if c.strip()))
        if not out:
            raise ValueError("At least one code is required.")
        return out

    @model_validator(mode="after")
    def _need_insurance(self):
        if self.mode == CoverageMode.INSURED and self.insurance is None:
            raise ValueError("insurance is required when mode is 'insured'")
        return self


class LineEstimate(BaseModel):
    code: str
    description: Optional[str]
    status: Literal["priced", "unavailable"]
    price_basis: Optional[Literal["negotiated", "cash", "gross"]] = None
    allowed_amount: Optional[float] = None
    rate_low: Optional[float] = None
    rate_high: Optional[float] = None
    plan_used: Optional[str] = None
    plan_matched: Optional[bool] = None
    deductible_paid: Optional[float] = None
    coinsurance_paid: Optional[float] = None
    patient_pays: Optional[float] = None
    insurer_pays: Optional[float] = None
    hit_oop_max: Optional[bool] = None
    reason: Optional[str] = Field(None, description="Why a price is unavailable")
    warning: Optional[str] = Field(None, description="Set when the price looks unreliable - show it next to the number")


class HospitalEstimate(BaseModel):
    hospital: str
    address: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    distance_miles: Optional[float]
    rank: Optional[int] = Field(None, description="1 = cheapest for the patient. Hospitals costing the patient the same share a rank (1, 1, 3). Null if some codes could not be priced.")
    tied: bool = Field(False, description="True when other hospitals cost the patient exactly the same")
    complete: bool
    coverage: str = Field(..., description="e.g. '3/4' codes priced")
    total_allowed: float
    total_patient_pays: float
    savings_vs_most_expensive: Optional[float] = None
    lines: List[LineEstimate]


class EstimateResponse(BaseModel):
    mode: CoverageMode
    payer: Optional[Payer]
    codes: List[str]
    hospitals: List[HospitalEstimate]
    best_hospital: Optional[str]
    best_hospitals: List[str] = Field(default_factory=list, description="Every hospital sharing the lowest patient cost (more than one = a tie)")
    max_savings: Optional[float]
    notice: Optional[str] = Field(None, description="Plain-language note to show above the results, e.g. when hospitals tie")
    disclaimers: List[str]


class CodeOut(BaseModel):
    code: str
    code_type: str
    description: str
    phase: str


class HospitalOut(BaseModel):
    hospital: str
    address: Optional[str]
    lat: Optional[float]
    lng: Optional[float]


class ExtractRequest(BaseModel):
    note: str = Field(..., min_length=3, max_length=20000)


class ExtractedCodeOut(BaseModel):
    code: str
    code_type: str
    description: str
    evidence: str
    confidence: float


class TokenUsageOut(BaseModel):
    input_tokens: int
    output_tokens: int = Field(..., description="Includes the model's thinking tokens")
    total_tokens: int


class ExtractResponse(BaseModel):
    extractor: str
    codes: List[ExtractedCodeOut]
    fallback_used: bool = Field(False, description="True if the AI call failed and the keyword matcher answered")
    usage: Optional[TokenUsageOut] = Field(None, description="Tokens used by this request; null if no AI call was made")
    notice: Optional[str] = Field(None, description="Show to the user when set, e.g. the AI budget is spent and a basic matcher answered")


class ExtractImageResponse(ExtractResponse):
    transcript: str = Field(..., description="Text read from the image - show it so the patient can check it")
    reader: str


class SessionOut(BaseModel):
    token: str
    token_type: str = "Bearer"
    expires_in: int = Field(..., description="Seconds until the token stops working; request a new one then")
