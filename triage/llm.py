"""Case-summary writers: Anthropic, a deterministic mock, and a template fallback.

The model only writes the narrative. It gets the evidence already computed by
the rules, must cite evidence ids, and its output is checked by validate()
before it is accepted.
"""
from __future__ import annotations

import json
import os
import re
from typing import Protocol

from pydantic import ValidationError

from .models import Alert, CaseSummary, Evidence

SYSTEM = """You are an AML analyst assistant. You write the first draft of a case summary for a human analyst.
Rules:
- Use only the facts in the ALERT and EVIDENCE blocks. Do not invent transactions, people, lists or dates.
- Every rationale point must cite at least one evidence id in square brackets, e.g. [E2].
- recommended_disposition is one of: escalate, close, request_information.
- If any evidence has severity "critical", you must recommend escalate.
- If the evidence is thin or ambiguous (for example a weak name match with no birth year to compare), recommend request_information and list what is missing.
- Never state that a customer IS a sanctioned person; say the record resembles a list entry and give the score.
Return only JSON with keys: summary, recommended_disposition, rationale, cited_evidence, missing_information."""


class Writer(Protocol):
    name: str
    def write(self, alert: Alert, evidence: list[Evidence]) -> str: ...


def build_prompt(alert: Alert, evidence: list[Evidence]) -> str:
    return ("ALERT:\n" + alert.model_dump_json(indent=1) +
            "\n\nEVIDENCE:\n" + json.dumps([e.model_dump() for e in evidence], indent=1, default=str))


class AnthropicWriter:
    def __init__(self, model: str | None = None):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
        self.name = f"anthropic:{self.model}"

    def write(self, alert: Alert, evidence: list[Evidence]) -> str:
        # Thinking is on by default on current models and its tokens count against max_tokens,
        # so the budget must leave room for reasoning plus the JSON answer.
        msg = self.client.messages.create(
            model=self.model, max_tokens=16000, system=SYSTEM,
            messages=[{"role": "user", "content": build_prompt(alert, evidence)}],
        )
        if msg.stop_reason in ("max_tokens", "refusal"):
            raise RuntimeError(f"answer not usable: stop_reason={msg.stop_reason}")
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class MockWriter:
    """Deterministic writer for tests and demos without an API key."""
    name = "mock"

    def write(self, alert: Alert, evidence: list[Evidence]) -> str:
        return template_summary(alert, evidence).model_dump_json()


def template_summary(alert: Alert, evidence: list[Evidence]) -> CaseSummary:
    if not evidence:
        return CaseSummary(
            summary=f"Alert {alert.alert_id} ({alert.trigger}): no red flags found by the automated checks.",
            recommended_disposition="close", rationale=["No sanctions hit, high-risk country, structuring or pass-through pattern detected."],
            cited_evidence=[])
    if all(e.severity == "low" for e in evidence):
        return CaseSummary(
            summary=f"Alert {alert.alert_id} ({alert.trigger}) for {alert.customer.name}: only discounted name "
                    f"matches, ruled out by birth year or nationality.",
            recommended_disposition="close", rationale=[f"{e.detail} [{e.id}]" for e in evidence[:8]],
            cited_evidence=[e.id for e in evidence])
    crit = [e for e in evidence if e.severity == "critical"]
    weak_names = [e for e in evidence if e.kind == "sanctions_hit" and e.severity == "high"]
    evidence = [e for e in evidence if e.severity != "low"] or evidence
    if crit:
        disp = "escalate"
    elif weak_names and len(evidence) == len(weak_names):
        disp = "request_information"
    else:
        disp = "escalate" if any(e.severity == "high" for e in evidence) else "request_information"
    return CaseSummary(
        summary=f"Alert {alert.alert_id} ({alert.trigger}) for {alert.customer.name}: "
                f"{len(evidence)} red flag(s) found, highest severity {max(evidence, key=lambda e: ['low','medium','high','critical'].index(e.severity)).severity}.",
        recommended_disposition=disp,
        rationale=[f"{e.detail} [{e.id}]" for e in evidence[:8]],
        cited_evidence=[e.id for e in evidence],
        missing_information=(["Customer date of birth and ID document to rule out the name match"] if weak_names else []),
    )


def validate(raw: str, evidence: list[Evidence]) -> tuple[CaseSummary | None, list[str]]:
    """Parse and check a model answer. Returns (summary, problems)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None, ["no JSON object in the answer"]
    try:
        cs = CaseSummary.model_validate_json(m.group(0))
    except ValidationError as e:
        return None, [f"schema: {err['loc']} {err['msg']}" for err in e.errors()][:5]
    problems = []
    ids = {e.id for e in evidence}
    cited = set(cs.cited_evidence) | set(re.findall(r"\[(E\d+)\]", " ".join(cs.rationale)))
    if unknown := cited - ids:
        problems.append(f"cites evidence that does not exist: {sorted(unknown)}")
    if evidence and not all(re.search(r"\[E\d+\]", r) for r in cs.rationale):
        problems.append("a rationale point has no evidence citation")
    if any(e.severity == "critical" for e in evidence) and cs.recommended_disposition != "escalate":
        problems.append("critical evidence present but disposition is not escalate")
    if re.search(r"\bis (?:a |the )?sanctioned\b", cs.summary, re.I):
        problems.append("summary asserts the customer is sanctioned instead of describing a resemblance")
    return (cs if not problems else None), problems
