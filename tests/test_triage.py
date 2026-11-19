from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from triage import api
from triage.audit import AuditLog
from triage.lists import load_sample
from triage.llm import validate
from triage.matching import Screener
from triage.models import Alert
from triage.names import normalize
from triage.rules import detect
from triage.service import Triage

DATA = Path(__file__).resolve().parent.parent / "data"
SCREENER = Screener(load_sample(DATA / "sample_list.json"))
ALERTS = {json.loads(l)["alert"]["alert_id"]: json.loads(l) for l in open(DATA / "alerts.jsonl", encoding="utf-8")}


def alert(aid: str) -> Alert:
    return Alert(**ALERTS[aid]["alert"])


class ScriptedWriter:
    name = "scripted"

    def __init__(self, answers):
        self.answers = list(answers)

    def write(self, alert, evidence):
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


# --- names and screening ------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Mohamed Abdelrahman", "Muhammad Abd al-Rahman"),
    ("Youssef", "Yusuf"),
    ("Khaled Ben Saleh", "Khalid bin Saleh"),
    ("Golden Crescent Trading LLC", "golden crescent trading"),
])
def test_romanizations_normalize_to_the_same_form(a, b):
    assert normalize(a) == normalize(b)


def test_birth_year_discounts_a_namesake():
    hits = SCREENER.screen("Elena Marchenko", dob_year=1995, nationality="Ukraine")
    assert not [h for h in hits if not h.discounted]


def test_first_name_alone_is_not_a_hit():
    assert not [h for h in SCREENER.screen("Muhammad") if not h.discounted]


def test_arabic_script_matches_original_script_name():
    hits = SCREENER.screen("محمد عبد الرحمن القاسمي")
    assert hits and hits[0].entry.uid == "SAMPLE:1"


# --- rules --------------------------------------------------------------------

def test_structuring_detected():
    kinds = [e.kind for e in detect(alert("A07"), SCREENER)]
    assert "structuring" in kinds


def test_counterparty_hit_carries_transaction_ids():
    ev = [e for e in detect(alert("A09"), SCREENER) if e.kind == "sanctions_hit"]
    assert ev and ev[0].data["subject"] == "counterparty" and ev[0].data["transaction_ids"] == ["T1"]


# --- LLM output validation ----------------------------------------------------

def _answer(**kw):
    base = {"summary": "Customer resembles a listed person, see evidence.", "recommended_disposition": "escalate",
            "rationale": ["Strong name and birth-year match [E1]"], "cited_evidence": ["E1"], "missing_information": []}
    base.update(kw)
    return json.dumps(base)


def test_validator_rejects_invented_evidence():
    ev = detect(alert("A01"), SCREENER)
    s, problems = validate(_answer(rationale=["see [E9]"], cited_evidence=["E9"]), ev)
    assert s is None and any("does not exist" in p for p in problems)


def test_validator_rejects_closing_a_critical_case():
    ev = detect(alert("A01"), SCREENER)
    s, problems = validate(_answer(recommended_disposition="close"), ev)
    assert s is None and any("critical" in p for p in problems)


def test_validator_rejects_asserting_sanctioned_status():
    ev = detect(alert("A01"), SCREENER)
    s, problems = validate(_answer(summary="The customer is a sanctioned terrorist financier."), ev)
    assert s is None


# --- service: retries, fallback, human decision -------------------------------

def test_bad_answer_then_good_answer(tmp_path):
    eng = Triage(SCREENER, AuditLog(str(tmp_path / "a.db")), ScriptedWriter(["not json", _answer()]))
    rep = eng.run(alert("A01"))
    assert rep.summary_source == "scripted" and rep.summary.recommended_disposition == "escalate"
    actions = [e["action"] for e in eng.audit.events(rep.case_id)]
    assert actions.count("llm_answer") == 2


def test_provider_down_falls_back_to_template(tmp_path):
    eng = Triage(SCREENER, AuditLog(str(tmp_path / "a.db")), ScriptedWriter([RuntimeError("503"), RuntimeError("503")]))
    rep = eng.run(alert("A07"))
    assert rep.summary_source == "fallback" and rep.summary.recommended_disposition == "escalate"


def test_closing_critical_case_needs_justification(tmp_path):
    eng = Triage(SCREENER, AuditLog(str(tmp_path / "a.db")))
    rep = eng.run(alert("A01"))
    with pytest.raises(ValueError):
        eng.decide(rep.case_id, "analyst1", "close", "fp")
    eng.decide(rep.case_id, "analyst1", "close", "Passport checked: different person, DOB 12/03/1971 vs ID 1974.")
    with pytest.raises(ValueError):
        eng.decide(rep.case_id, "analyst2", "escalate", "")


# --- audit log ----------------------------------------------------------------

def test_audit_log_detects_tampering(tmp_path):
    db = str(tmp_path / "a.db")
    log = AuditLog(db)
    for i in range(5):
        log.append("system", "event", {"i": i})
    assert log.verify()["ok"]
    with sqlite3.connect(db) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE audit_log SET payload='{}' WHERE seq=2")
        c.execute("DROP TRIGGER audit_no_update")  # an attacker with file access
        c.execute("UPDATE audit_log SET payload='{\"i\": 99}' WHERE seq=2")
    assert log.verify() == {"ok": False, "broken_at_seq": 2, "events_checked": 1}


# --- API ----------------------------------------------------------------------

def test_api_flow(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_engine", Triage(SCREENER, AuditLog(str(tmp_path / "a.db"))))
    c = TestClient(api.app)
    rep = c.post("/v1/alerts/triage", json=ALERTS["A09"]["alert"]).json()
    assert rep["risk_level"] == "critical" and rep["status"] == "pending_review"
    r = c.post(f"/v1/cases/{rep['case_id']}/decision", json={"analyst": "sara", "decision": "escalate", "note": "SAR filed"})
    assert r.status_code == 200 and r.json()["status"] == "escalated"
    assert [e["action"] for e in c.get(f"/v1/cases/{rep['case_id']}/audit").json()][-1] == "decision"
    assert c.get("/v1/audit/verify").json()["ok"]
