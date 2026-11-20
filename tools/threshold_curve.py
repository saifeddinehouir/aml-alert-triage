"""Detection vs false alerts across thresholds, for plain fuzzy search and this screener.

    python tools/threshold_curve.py [--n 5000] [--seed 7] [--holdout]

Same customers and altered listed names as false_alert_benchmark.py (same seed, same
draws). Each name is screened once at the lowest threshold; the best score is kept and
the threshold is swept afterwards, so both methods are compared at the same detection
rate instead of each at its own default threshold. No tuning, no LLM calls.
"""
from __future__ import annotations

import argparse
import csv
import os
import random
import sys
import time
from multiprocessing import Pool
from pathlib import Path

from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).resolve().parent))
import false_alert_benchmark as fab  # noqa: E402
from false_alert_benchmark import ROOT, NATIONALITY, Naive, Screener, load_all, normalize  # noqa: E402

LOWEST = 50
THRESHOLDS = list(range(60, 96))
METHODS = ["plain fuzzy, token_set_ratio", "plain fuzzy, token_sort_ratio",
           "ours + birth year", "ours + birth year + nationality"]
_worker: dict = {}


def _init_worker() -> None:
    entries = load_all(ROOT / "data")
    _worker.update(screener=Screener(entries), naive_set=Naive(entries, fuzz.token_set_ratio),
                   naive_sort=Naive(entries, fuzz.token_sort_ratio), by_uid={e.uid: e for e in entries})


def _naive_best(naive: Naive, name: str, target: str | None) -> float:
    q = naive.clean(name)
    if target is None:
        best = process.extractOne(q, naive.names, scorer=naive.scorer)
        return best[1] if best else 0.0
    names = [naive.clean(n) for n in _worker["by_uid"][target].all_names()]
    return max((naive.scorer(q, n) for n in names if n), default=0.0)


def _ours_scores(name: str, dob: int | None, nat: str | None) -> dict[str, float]:
    # Scores don't depend on the threshold; screening at the lowest one keeps every candidate.
    return {h.entry.uid: h.score for h in _worker["screener"].screen(name, dob, nat, threshold=LOWEST, limit=1000)}


def _best(job: tuple[str, int | None, str | None, str | None]) -> list[float]:
    """Per method: the best score of any list entry (customers) or of the target entry (probes)."""
    name, dob, nat, target = job
    ours = [_ours_scores(name, dob, None), _ours_scores(name, dob, nat)]
    pick = (lambda s: max(s.values(), default=0.0)) if target is None else (lambda s: s.get(target, 0.0))
    return [_naive_best(_worker["naive_set"], name, target), _naive_best(_worker["naive_sort"], name, target),
            *map(pick, ours)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--probes", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--csv", type=Path, default=ROOT / "docs" / "threshold_curve.csv")
    args = ap.parse_args()
    fab._translit = fab.TRANSLIT_HOLDOUT if args.holdout else fab.TRANSLIT

    # Same draws, in the same order, as false_alert_benchmark.py.
    rng = random.Random(args.seed)
    entries = load_all(ROOT / "data")
    people = fab.customers(args.n, rng)
    arabic = {"muhammad", "ahmad", "abd", "hasan", "husayn", "ali", "umar", "yusuf", "ibrahim", "khalid", "said", "mustafa"}
    listed = [e for e in entries if e.entity_type == "individual" and e.dob_years
              and arabic & set(normalize(e.name).split())]
    probes = []
    for alter in fab.ALTERATIONS.values():
        rng.shuffle(listed)
        kept = []
        for e in listed:
            altered = alter(e.name, rng)
            changed = normalize(altered) != normalize(e.name)
            if alter is fab.reverse:
                changed = altered != e.name and len(e.name.split()) >= 2
            if changed:
                kept.append((e, altered))
            if len(kept) == args.probes:
                break
        probes += kept

    jobs = [(name, dob, NATIONALITY, None) for name, dob in people]
    jobs += [(a, e.dob_years[0], e.nationalities[0] if e.nationalities else None, e.uid) for e, a in probes]
    t0 = time.time()
    with Pool(args.workers, _init_worker) as pool:
        scores = []
        for i, r in enumerate(pool.imap(_best, jobs, chunksize=10), 1):
            scores.append(r)
            if i % 500 == 0 or i == len(jobs):
                print(f"  .. {i}/{len(jobs)}", file=sys.stderr, flush=True)
    cust, prob = scores[:len(people)], scores[len(people):]

    rows = []
    for t in THRESHOLDS:
        for k, m in enumerate(METHODS):
            fa = sum(s[k] >= t for s in cust) / len(cust)
            det = sum(s[k] >= t for s in prob) / len(prob)
            rows.append({"threshold": t, "method": m, "detection": round(det, 4), "false_alerts": round(fa, 4)})
    args.csv.parent.mkdir(exist_ok=True)
    with open(args.csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\n{len(people)} fictional customers, {len(prob)} altered listed names, seed {args.seed}"
          f"{' (held-out transliterations)' if args.holdout else ''}\n")
    print("threshold  " + "  ".join(f"{m:>32}" for m in METHODS))
    print("           " + "  ".join(f"{'detection / false alerts':>32}" for _ in METHODS))
    for t in THRESHOLDS:
        cells = [r for r in rows if r["threshold"] == t]
        print(f"{t:9}  " + "  ".join(f"{100 * r['detection']:21.1f}% / {100 * r['false_alerts']:5.1f}%" for r in cells))

    # Same detection: the plain fuzzy default (token_sort >= 80) vs the lowest false-alert
    # rate each other method reaches while detecting at least as much.
    ref = next(r for r in rows if r["method"] == METHODS[1] and r["threshold"] == 80)
    print(f"\nat the detection of plain token_sort >= 80 ({100 * ref['detection']:.1f}%, "
          f"{100 * ref['false_alerts']:.1f}% false alerts):")
    for m in METHODS:
        ok = [r for r in rows if r["method"] == m and r["detection"] >= ref["detection"]]
        if ok:
            r = min(ok, key=lambda r: r["false_alerts"])
            print(f"  {m:34} threshold {r['threshold']}: detection {100 * r['detection']:.1f}%, "
                  f"false alerts {100 * r['false_alerts']:.1f}%")
        else:
            print(f"  {m:34} never reaches it between {THRESHOLDS[0]} and {THRESHOLDS[-1]}")

    # And the other way: plain fuzzy set to detect as much as this screener at its default threshold.
    for m in METHODS[2:]:
        ours = next(r for r in rows if r["method"] == m and r["threshold"] == 80)
        print(f"\nat the detection of {m} >= 80 ({100 * ours['detection']:.1f}%, "
              f"{100 * ours['false_alerts']:.1f}% false alerts):")
        for base in METHODS[:2]:
            r = min((r for r in rows if r["method"] == base and r["detection"] >= ours["detection"]),
                    key=lambda r: r["false_alerts"])
            print(f"  {base:34} threshold {r['threshold']}: detection {100 * r['detection']:.1f}%, "
                  f"false alerts {100 * r['false_alerts']:.1f}%")
    print(f"\n({time.time() - t0:.0f}s, {args.workers} workers; table written to {args.csv})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
