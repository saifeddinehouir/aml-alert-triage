from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .audit import AuditLog
from .lists import load_all
from .llm import AnthropicWriter, MockWriter
from .matching import Screener
from .models import Alert, CaseReport
from .service import Triage

DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
AUDIT_DB = os.environ.get("AUDIT_DB", "audit.sqlite3")

app = FastAPI(title="AML alert triage", version="0.1.0")
_engine: Triage | None = None


def engine() -> Triage:
    global _engine
    if _engine is None:
        writer = AnthropicWriter() if os.environ.get("ANTHROPIC_API_KEY") else MockWriter()
        _engine = Triage(Screener(load_all(DATA_DIR)), AuditLog(AUDIT_DB), writer)
    return _engine


class ScreenRequest(BaseModel):
    name: str
    dob_year: int | None = None
    nationality: str | None = None
    threshold: float = Field(80, ge=50, le=100)


class Decision(BaseModel):
    analyst: str = Field(min_length=2)
    decision: str
    note: str = ""


@app.post("/v1/screen")
def screen(req: ScreenRequest):
    hits = engine().screener.screen(req.name, req.dob_year, req.nationality, threshold=req.threshold)
    return [h.as_evidence() for h in hits]


@app.post("/v1/alerts/triage", response_model=CaseReport)
def triage(alert: Alert):
    return engine().run(alert)


@app.post("/v1/cases/{case_id}/decision", response_model=CaseReport)
def decide(case_id: str, d: Decision):
    eng = engine()
    if case_id not in eng.cases:
        raise HTTPException(404, "unknown case")
    try:
        return eng.decide(case_id, d.analyst, d.decision, d.note)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/v1/cases/{case_id}/audit")
def case_audit(case_id: str):
    return engine().audit.events(case_id)


@app.get("/v1/audit/verify")
def verify():
    return engine().audit.verify()
