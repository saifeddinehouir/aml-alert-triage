"""Run the labeled alerts in data/alerts.jsonl and score the system.

    python evaluate.py               # mock writer, no API key needed
    python evaluate.py --llm         # Anthropic writer (needs ANTHROPIC_API_KEY)
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from triage.audit import AuditLog
from triage.lists import load_sample
from triage.llm import AnthropicWriter, MockWriter
from triage.matching import Screener
from triage.models import Alert
from triage.service import Triage

ROOT = Path(__file__).parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the Anthropic writer")
    ap.add_argument("--alerts", default=str(ROOT / "data/alerts.jsonl"))
    args = ap.parse_args()

    writer = AnthropicWriter() if args.llm else MockWriter()
    db = Path(tempfile.mkdtemp()) / "eval_audit.sqlite3"
    eng = Triage(Screener(load_sample(ROOT / "data/sample_list.json")), AuditLog(str(db)), writer)

    rows = [json.loads(l) for l in open(args.alerts, encoding="utf-8") if l.strip()]
    tp = fp = fn = tn = 0
    disp_ok = 0
    fallbacks = 0
    lines = []
    for r in rows:
        alert, exp = Alert(**r["alert"]), r["expected"]
        rep = eng.run(alert)
        crit = sorted({e.data["list_uid"] for e in rep.evidence if e.kind == "sanctions_hit" and e.severity == "critical"})
        want = exp["critical_hit"]
        if want and want in crit: tp += 1
        if want and want not in crit: fn += 1
        if crit and (not want or crit != [want]): fp += 1
        if not want and not crit: tn += 1
        ok = rep.summary.recommended_disposition == exp["disposition"]
        disp_ok += ok
        fallbacks += rep.summary_source == "fallback"
        lines.append(f"{alert.alert_id}  {'OK ' if ok and (crit == ([want] if want else [])) else 'XX '}"
                     f"hit={crit or '-'} want={want or '-'}  disp={rep.summary.recommended_disposition} "
                     f"want={exp['disposition']}  ({exp['note']})")

    n = len(rows)
    print("\n".join(lines))
    print(f"\nalerts: {n}   writer: {writer.name}   fallbacks to template: {fallbacks}")
    print(f"critical sanctions hits: TP={tp} FP={fp} FN={fn} TN={tn}  "
          f"recall={tp / max(tp + fn, 1):.0%}  precision={tp / max(tp + fp, 1):.0%}")
    print(f"recommended disposition matches label: {disp_ok}/{n} ({disp_ok / n:.0%})")
    print(f"audit chain: {eng.audit.verify()}")


if __name__ == "__main__":
    main()
