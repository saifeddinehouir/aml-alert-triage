from __future__ import annotations

import collections
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .audit import AuditLog
from .lists import download_lists, lists_age_hours, load_all
from .llm import AnthropicWriter, MockWriter
from .matching import Screener
from .models import Alert, CaseReport
from .service import Triage

DATA_DIR = os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
AUDIT_DB = os.environ.get("AUDIT_DB", "audit.sqlite3")
LIST_REFRESH_HOURS = float(os.environ.get("LIST_REFRESH_HOURS", "24"))  # 0 turns it off
RETRY_AFTER_FAILURE_HOURS = 1

_stop = threading.Event()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if LIST_REFRESH_HOURS > 0:
        threading.Thread(target=_refresh_loop, name="list-refresh", daemon=True).start()
    yield
    _stop.set()


app = FastAPI(title="AML alert triage", version="0.1.0", lifespan=lifespan)
_engine: Triage | None = None


def engine() -> Triage:
    global _engine
    if _engine is None:
        writer = AnthropicWriter() if os.environ.get("ANTHROPIC_API_KEY") else MockWriter()
        _engine = Triage(Screener(load_all(DATA_DIR)), AuditLog(AUDIT_DB), writer)
    return _engine


def refresh_lists() -> bool:
    """Download the lists and swap them into the running screener. Plain HTTP, no LLM calls."""
    eng = engine()
    failed = download_lists(DATA_DIR)
    if failed:
        # the previous lists stay in use
        eng.audit.append("system", "lists_refresh_failed", {"errors": failed})
        return False
    entries = load_all(DATA_DIR)
    eng.screener = Screener(entries)
    eng.audit.append("system", "lists_refreshed", {"entries": dict(collections.Counter(e.source for e in entries))})
    return True


def _refresh_loop() -> None:
    while not _stop.is_set():
        age = lists_age_hours(DATA_DIR)
        if age is None or age >= LIST_REFRESH_HOURS:
            ok = refresh_lists()
            wait = LIST_REFRESH_HOURS if ok else RETRY_AFTER_FAILURE_HOURS
        else:
            wait = LIST_REFRESH_HOURS - age
        _stop.wait(wait * 3600)


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


@app.get("/v1/lists")
def lists_status():
    entries = engine().screener.entries
    age = lists_age_hours(DATA_DIR)
    return {
        "entries": dict(collections.Counter(e.source for e in entries)),
        "age_hours": None if age is None else round(age, 1),
        "refresh_every_hours": LIST_REFRESH_HOURS or None,
    }


@app.get("/v1/audit/verify")
def verify():
    return engine().audit.verify()
