from __future__ import annotations

import uuid

from .audit import AuditLog
from .llm import MockWriter, Writer, template_summary, validate
from .matching import Screener
from .models import Alert, CaseReport
from .rules import detect, overall_risk

DECISION_STATUS = {"escalate": "escalated", "close": "closed", "request_information": "information_requested"}


class Triage:
    def __init__(self, screener: Screener, audit: AuditLog, writer: Writer | None = None, max_attempts: int = 2):
        self.screener = screener
        self.audit = audit
        self.writer = writer or MockWriter()
        self.max_attempts = max_attempts
        self.cases: dict[str, CaseReport] = {}

    def run(self, alert: Alert) -> CaseReport:
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        self.audit.append("system", "alert_received", alert.model_dump(mode="json"), case_id)
        evidence = detect(alert, self.screener)
        self.audit.append("system", "evidence_computed", {"evidence": [e.model_dump() for e in evidence]}, case_id)

        summary, source = None, "fallback"
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = self.writer.write(alert, evidence)
            except Exception as e:  # provider error: log it, then retry or fall back
                self.audit.append("system", "llm_error", {"attempt": attempt, "error": str(e)[:300]}, case_id)
                continue
            summary, problems = validate(raw, evidence)
            self.audit.append("system", "llm_answer", {"attempt": attempt, "writer": self.writer.name,
                                                        "accepted": summary is not None, "problems": problems,
                                                        "raw": raw[:4000]}, case_id)
            if summary:
                source = self.writer.name
                break
        if summary is None:
            summary = template_summary(alert, evidence)

        report = CaseReport(case_id=case_id, alert_id=alert.alert_id, risk_level=overall_risk(evidence),
                            evidence=evidence, summary=summary, summary_source=source)
        self.cases[case_id] = report
        self.audit.append("system", "case_drafted", {"risk_level": report.risk_level,
                                                      "recommended": summary.recommended_disposition,
                                                      "summary_source": source}, case_id)
        return report

    def decide(self, case_id: str, analyst: str, decision: str, note: str) -> CaseReport:
        """Only a named human can move a case out of pending_review."""
        case = self.cases[case_id]
        if case.status != "pending_review":
            raise ValueError(f"case already decided ({case.status})")
        if decision not in DECISION_STATUS:
            raise ValueError(f"unknown decision {decision}")
        if decision == "close" and case.risk_level == "critical" and len(note.strip()) < 30:
            raise ValueError("closing a critical case needs a written justification of at least 30 characters")
        case.status = DECISION_STATUS[decision]
        self.audit.append(f"analyst:{analyst}", "decision", {"decision": decision, "note": note,
                                                              "recommended": case.summary.recommended_disposition,
                                                              "overrode_recommendation": decision != case.summary.recommended_disposition},
                          case_id)
        return case
