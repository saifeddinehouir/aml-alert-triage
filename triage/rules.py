"""Deterministic red-flag detection. The LLM never decides these; it only explains them."""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .matching import Screener
from .models import Alert, Evidence

# FATF "call for action" list as of the last check. Review it after each FATF plenary.
HIGH_RISK_COUNTRIES = {"iran", "north korea", "dprk", "myanmar"}
# Countries under increased monitoring change often; keep this configurable, not hard-coded logic.
MONITORED_COUNTRIES: set[str] = set()

CASH_THRESHOLD = 10_000
STRUCTURING_BAND = 0.80  # deposits between 80% and 100% of the threshold
STRUCTURING_MIN_COUNT = 3
STRUCTURING_WINDOW_DAYS = 7
PASS_THROUGH_DAYS = 3
PASS_THROUGH_RATIO = 0.90

SEVERITY_ORDER = ["low", "medium", "high", "critical"]


def detect(alert: Alert, screener: Screener) -> list[Evidence]:
    ev: list[Evidence] = []

    def add(kind, severity, detail, **data):
        ev.append(Evidence(id=f"E{len(ev) + 1}", kind=kind, severity=severity, detail=detail, data=data))

    c = alert.customer
    for hit in screener.screen(c.name, c.dob_year, c.nationality):
        if hit.discounted:
            add("sanctions_hit", "low", f"Customer name matches {hit.entry.source} entry '{hit.entry.name}' "
                f"(name {hit.name_score:.0f}) but identifiers do not: {'; '.join(hit.reasons[1:])}. Likely a namesake.",
                subject="customer", **hit.as_evidence())
            continue
        sev = "critical" if hit.score >= 90 else "high"
        add("sanctions_hit", sev, f"Customer '{c.name}' resembles {hit.entry.source} entry '{hit.entry.name}' "
            f"(score {hit.score:.0f})", subject="customer", **hit.as_evidence())

    seen_cp: set[str] = set()
    for t in alert.transactions:
        if t.counterparty_name and t.counterparty_name not in seen_cp:
            seen_cp.add(t.counterparty_name)
            for hit in screener.screen(t.counterparty_name):
                if hit.discounted:
                    continue
                sev = "critical" if hit.score >= 90 else "high"
                add("sanctions_hit", sev, f"Counterparty '{t.counterparty_name}' resembles {hit.entry.source} "
                    f"entry '{hit.entry.name}' (score {hit.score:.0f})", subject="counterparty",
                    transaction_ids=[x.id for x in alert.transactions if x.counterparty_name == t.counterparty_name],
                    **hit.as_evidence())

    by_country = defaultdict(list)
    for t in alert.transactions:
        if t.counterparty_country and t.counterparty_country.lower() in HIGH_RISK_COUNTRIES | MONITORED_COUNTRIES:
            by_country[t.counterparty_country].append(t)
    for country, txs in by_country.items():
        sev = "high" if country.lower() in HIGH_RISK_COUNTRIES else "medium"
        add("high_risk_country", sev, f"{len(txs)} transaction(s) with {country}, total "
            f"{sum(t.amount for t in txs):,.0f} {txs[0].currency}", transaction_ids=[t.id for t in txs])

    cash_in = sorted((t for t in alert.transactions if t.channel == "cash" and t.direction == "in"
                      and CASH_THRESHOLD * STRUCTURING_BAND <= t.amount < CASH_THRESHOLD), key=lambda t: t.date)
    for i, first in enumerate(cash_in):
        window = [t for t in cash_in[i:] if t.date - first.date <= timedelta(days=STRUCTURING_WINDOW_DAYS)]
        if len(window) >= STRUCTURING_MIN_COUNT:
            add("structuring", "high", f"{len(window)} cash deposits just under the {CASH_THRESHOLD:,} reporting "
                f"threshold within {STRUCTURING_WINDOW_DAYS} days, total {sum(t.amount for t in window):,.0f}",
                transaction_ids=[t.id for t in window])
            break

    ins = [t for t in alert.transactions if t.direction == "in"]
    outs = [t for t in alert.transactions if t.direction == "out"]
    for t_in in ins:
        quick = [o for o in outs if timedelta(0) <= o.date - t_in.date <= timedelta(days=PASS_THROUGH_DAYS)]
        if quick and sum(o.amount for o in quick) >= PASS_THROUGH_RATIO * t_in.amount:
            add("pass_through", "medium", f"{t_in.amount:,.0f} received ({t_in.id}) and "
                f"{sum(o.amount for o in quick):,.0f} sent out within {PASS_THROUGH_DAYS} days",
                transaction_ids=[t_in.id, *[o.id for o in quick]])
            break

    if c.risk_rating == "high":
        add("profile", "medium", "Customer is rated high risk at onboarding")
    return ev


def overall_risk(evidence: list[Evidence]) -> str:
    if not evidence:
        return "low"
    return max((e.severity for e in evidence), key=SEVERITY_ORDER.index)
