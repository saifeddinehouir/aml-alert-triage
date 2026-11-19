from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


class Customer(BaseModel):
    name: str
    dob_year: int | None = None
    nationality: str | None = None
    occupation: str | None = None
    risk_rating: Literal["low", "medium", "high"] = "medium"


class Transaction(BaseModel):
    id: str
    date: date
    amount: float
    currency: str = "USD"
    direction: Literal["in", "out"]
    channel: Literal["cash", "wire", "card", "transfer"] = "transfer"
    counterparty_name: str | None = None
    counterparty_country: str | None = None


class Alert(BaseModel):
    alert_id: str
    trigger: str
    customer: Customer
    transactions: list[Transaction] = Field(default_factory=list)


class Evidence(BaseModel):
    id: str  # E1, E2 ... referenced by the case summary
    kind: Literal["sanctions_hit", "high_risk_country", "structuring", "pass_through", "profile"]
    severity: Literal["critical", "high", "medium", "low"]
    detail: str
    data: dict = Field(default_factory=dict)


Disposition = Literal["escalate", "close", "request_information"]


class CaseSummary(BaseModel):
    """What the LLM must return. Validated before anything is stored."""
    summary: str = Field(min_length=20, max_length=1500)
    recommended_disposition: Disposition
    rationale: list[str] = Field(min_length=1, max_length=8)
    cited_evidence: list[str]
    missing_information: list[str] = Field(default_factory=list)


class CaseReport(BaseModel):
    case_id: str
    alert_id: str
    risk_level: Literal["critical", "high", "medium", "low"]
    evidence: list[Evidence]
    summary: CaseSummary
    summary_source: str  # "anthropic:<model>", "mock", "fallback"
    status: Literal["pending_review", "escalated", "closed", "information_requested"] = "pending_review"
