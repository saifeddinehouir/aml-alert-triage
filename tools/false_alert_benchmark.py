"""False-alert rate on common Moroccan names against the real OFAC + UN lists.

    python tools/fetch_lists.py              # once
    python tools/false_alert_benchmark.py [--n 5000] [--seed 42]

Generates fictional Moroccan customers (random first name + family name + birth
year), so every alert they raise is a false alert. Compares plain fuzzy search with
this project's screener, with and without birth year and nationality.

To check that fewer alerts does not just mean a blind screener, it also alters the
names of real listed individuals in ways the normalizer was never taught (typos,
dropped middle name, reversed word order, transliterations missing from its
variant table) and counts how many are still found. No LLM calls.
"""
from __future__ import annotations

import argparse
import math
import os
import random
import re
import string
import sys
import time
from multiprocessing import Pool
from pathlib import Path

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from triage.lists import ListEntry, load_all  # noqa: E402
from triage.matching import Screener  # noqa: E402
from triage.names import normalize, strip_accents  # noqa: E402

MALE = ["Mohamed", "Mohammed", "Ahmed", "Youssef", "Hamza", "Ayoub", "Omar", "Othmane", "Mehdi", "Amine",
        "Abdelilah", "Abderrahim", "Abdellah", "Rachid", "Said", "Hassan", "Hicham", "Karim", "Khalid",
        "Mustapha", "Driss", "Brahim", "Noureddine", "Abdelaziz", "Soufiane", "Zakaria", "Ilyas", "Anas",
        "Badr", "Yassine", "Tarik", "Jamal", "Aziz", "Lahcen", "Hamid", "Abdelkader", "Redouane", "Adil",
        "Nabil", "Ismail", "Ali", "Hussein", "Abdelhak", "Mounir", "Younes"]
FEMALE = ["Fatima", "Fatima Zahra", "Khadija", "Aicha", "Meryem", "Salma", "Imane", "Hajar", "Sara", "Nadia",
          "Houda", "Karima", "Latifa", "Zineb", "Ghizlane", "Asmae", "Siham", "Hanane", "Loubna", "Najat",
          "Samira", "Btissam", "Nora", "Laila", "Rajae", "Kenza", "Oumaima", "Chaimae", "Hafsa", "Wafae"]
FAMILY = ["Alaoui", "El Alaoui", "Bennani", "Tazi", "Berrada", "Chraibi", "Benjelloun", "El Fassi", "Lahlou",
          "Amrani", "El Amrani", "Ouazzani", "Benkirane", "Sqalli", "Kettani", "El Mansouri", "Bouzidi",
          "Zniber", "Idrissi", "El Idrissi", "Benali", "Benmoussa", "Cherkaoui", "Filali", "Hajji", "Naciri",
          "Rhazi", "Sefrioui", "Slaoui", "Tahiri", "Ziani", "Bakkali", "Boukhari", "El Ouafi", "Ait Ali",
          "Ait Lahcen", "Ouali", "Belhaj", "Benabdellah", "Benchekroun", "Bensouda", "Bouazzaoui", "Chafik",
          "Daoudi", "El Khattabi", "Essaidi", "Fikri", "Guessous", "Hamdaoui", "Jebli", "Kabbaj", "Lamrani",
          "Mernissi", "Mouline", "Nejjari", "Saadi", "Skalli", "Yacoubi", "Zemmouri", "Benhaddou",
          "El Hassani", "Bensaid", "Ben Omar", "Ait Brahim", "Mansouri", "Hassani", "Ben Ali", "Bouchama"]

# Transliterations seen in the wild, keyed by the normalizer's canonical token. A probe
# only counts as "unseen" if the result normalizes differently from the original.
TRANSLIT = {
    "muhammad": ["Mouhamad", "Mouhammed", "Mohamud", "Mahomed"], "ahmad": ["Ehmed", "Achmad"],
    "hasan": ["Hassen", "Hacen"], "husayn": ["Houcine", "Hocine", "Husseyn"],
    "yusuf": ["Youssouf", "Yousuf", "Yusif"], "umar": ["Oumar"], "ibrahim": ["Ibrahima", "Ebrahem"],
    "khalid": ["Khaleid", "Kaled"], "said": ["Saeid", "Saiid"], "mustafa": ["Mostapha", "Mustafah"],
    "qasim": ["Kacem", "Gassim"], "rahman": ["Rahmen"],
}
# Written after the screener was tuned on TRANSLIT, and never used to tune it (--holdout).
TRANSLIT_HOLDOUT = {
    "muhammad": ["Mohummed", "Muhamet", "Mukhammad"], "ahmad": ["Ahmet", "Akhmed"],
    "hasan": ["Hasen", "Hassane"], "husayn": ["Housseine", "Huseyin"],
    "yusuf": ["Iusuf", "Yousouf"], "umar": ["Oumer", "Omer"], "ibrahim": ["Brahim", "Ebrahim"],
    "khalid": ["Halid", "Khaleed"], "said": ["Sayeed", "Saaid"], "mustafa": ["Moustafa", "Mostefa"],
    "qasim": ["Kassem", "Qassem"], "rahman": ["Rehman", "Rachman"],
}
NATIONALITY = "Morocco"
_translit = TRANSLIT


def customers(n: int, rng: random.Random) -> list[tuple[str, int]]:
    out: dict[str, int] = {}
    while len(out) < n:
        pool = rng.choice([MALE, FEMALE])
        first = rng.choice(pool)
        if rng.random() < 0.2:
            first += " " + rng.choice([p for p in pool if p != first])
        out.setdefault(f"{first} {rng.choice(FAMILY)}", rng.randint(1950, 2004))
    return list(out.items())


# --- alterations the normalizer was not built for ---------------------------------

def typo(name: str, rng: random.Random) -> str:
    words = name.split()
    i = rng.choice([k for k, w in enumerate(words) if len(w) >= 4] or [0])
    w, p = words[i], rng.randrange(len(words[i]))
    op = rng.choice(["insert", "delete", "swap"])
    if op == "insert":
        w = w[:p] + rng.choice(string.ascii_lowercase) + w[p:]
    elif op == "delete":
        w = w[:p] + w[p + 1:]
    elif p < len(w) - 1:
        w = w[:p] + w[p + 1] + w[p] + w[p + 2:]
    words[i] = w
    return " ".join(words)


_JOINS_NEXT = {"abd", "abdul", "abdel", "abu", "bin", "ben", "ibn", "bint", "al", "el"}


def name_parts(name: str) -> list[str]:
    """Words grouped into whole name parts: 'Abd al-Salam', 'Nur al-Din', 'bin Laden' stay together."""
    parts: list[str] = []
    for w in name.split():
        if parts and (parts[-1].split()[-1].lower() in _JOINS_NEXT or re.match(r"(?i)^(al|ad|ud|el|ed)-?din$", w)):
            parts[-1] += " " + w
        else:
            parts.append(w)
    return parts


def drop_middle(name: str, rng: random.Random) -> str:
    parts = name_parts(name)
    if len(parts) < 3:
        return name
    del parts[rng.randrange(1, len(parts) - 1)]
    return " ".join(parts)


def reverse(name: str, rng: random.Random) -> str:
    return " ".join(reversed(name_parts(name)))


def translit(name: str, rng: random.Random) -> str:
    # "Nur al-Din" -> "Noureddine"-style fusion, "Abd al-Salam" -> "Abdessalam" (sun-letter assimilation)
    s = re.sub(r"\b([A-Za-z]+)[ -](?:al|ad|ud|el|ed)-?[ -]?din\b", lambda m: m.group(1).capitalize() + "eddine", name, flags=re.I)
    s = re.sub(r"\babd[ -]?(?:al|el|ul)[ -]?-?([tdrzsn])([a-z]+)", lambda m: "Abde" + m.group(1).lower() * 2 + m.group(2).lower(), s, flags=re.I)

    def word(m: re.Match) -> str:
        canon = normalize(m.group(0))
        return rng.choice(_translit[canon]) if canon in _translit else m.group(0)
    return re.sub(r"[A-Za-z']+", word, s)


ALTERATIONS = {"typo (1 letter added, dropped or swapped)": typo, "middle name dropped": drop_middle,
               "word order reversed": reverse, "transliteration not in the variant table": translit}


class Naive:
    """Plain fuzzy search: lowercase, no accents or punctuation, one rapidfuzz scorer, threshold 80."""

    def __init__(self, entries: list[ListEntry], scorer):
        self.scorer, self.names, self.owner = scorer, [], []
        for e in entries:
            for n in e.all_names():
                c = self.clean(n)
                if c:
                    self.names.append(c)
                    self.owner.append(e.uid)

    @staticmethod
    def clean(s: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", strip_accents(s).lower()).split())

    def hits(self, name: str) -> set[str]:
        return {self.owner[i] for _, _, i in process.extract(self.clean(name), self.names, scorer=self.scorer,
                                                             score_cutoff=80, limit=None)}


def wilson(k: int, n: int) -> tuple[float, float]:
    """95% confidence interval for a proportion."""
    if n == 0:
        return 0.0, 0.0
    z, p = 1.96, k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, c - h), min(1.0, c + h)


def fmt(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f"{k:5}/{n:<5} {100 * k / n:5.1f}%  [{100 * lo:.1f}-{100 * hi:.1f}]"


CONFIGS = ["plain fuzzy, token_set_ratio >= 80", "plain fuzzy, token_sort_ratio >= 80",
           "ours, name only", "ours + birth year", "ours + birth year + nationality"]
_worker: dict = {}


def _init_worker() -> None:
    entries = load_all(ROOT / "data")
    _worker.update(screener=Screener(entries), naive_set=Naive(entries, fuzz.token_set_ratio),
                   naive_sort=Naive(entries, fuzz.token_sort_ratio))


def _screen_all(job: tuple[str, int | None, str | None]) -> list[list[str]]:
    """Alerting list uids for one (name, birth year, nationality), per config in CONFIGS order."""
    name, dob, nat = job
    s = _worker["screener"]

    def ours(y, c):
        return sorted(h.entry.uid for h in s.screen(name, y, c) if not h.discounted)
    return [sorted(_worker["naive_set"].hits(name)), sorted(_worker["naive_sort"].hits(name)),
            ours(None, None), ours(dob, None), ours(dob, nat)]


def _run(pool: Pool, jobs: list, what: str) -> list[list[list[str]]]:
    out = []
    for i, r in enumerate(pool.imap(_screen_all, jobs, chunksize=10), 1):
        out.append(r)
        if i % 500 == 0 or i == len(jobs):
            print(f"  .. {what} {i}/{len(jobs)}", file=sys.stderr, flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5000)
    ap.add_argument("--probes", type=int, default=300, help="listed individuals per alteration")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--holdout", action="store_true", help="use the transliterations never used for tuning")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    global _translit
    _translit = TRANSLIT_HOLDOUT if args.holdout else TRANSLIT

    entries = load_all(ROOT / "data")
    if not any(e.source != "Sample list (fictional)" for e in entries):
        print("real lists not found, run tools/fetch_lists.py first")
        return 1
    configs = CONFIGS
    t0 = time.time()
    pool = Pool(args.workers, _init_worker)

    people = customers(args.n, rng)
    results = _run(pool, [(name, dob, NATIONALITY) for name, dob in people], "customers")
    print(f"\nFALSE ALERTS: {len(people)} fictional Moroccan customers vs {len(entries):,} OFAC + UN entries"
          f"\n(every alert is false; 95% interval in brackets)\n")
    rates: dict[str, float] = {}
    remaining = [f"{name} ({dob}) -> {', '.join(r[-1])}" for (name, dob), r in zip(people, results) if r[-1]]
    for k, label in enumerate(configs):
        flagged = sum(bool(r[k]) for r in results)
        rates[label] = flagged / len(people)
        print(f"  {label:36} {fmt(flagged, len(people))}")

    best_simple = min(list(configs)[:2], key=rates.get)
    print(f"\n  best plain fuzzy ({best_simple.split(', ')[1]}): {100 * rates[best_simple]:.1f}%"
          f"  ->  ours + birth year: {100 * rates['ours + birth year']:.1f}%"
          f"  ->  + nationality: {100 * rates['ours + birth year + nationality']:.1f}%")

    # Detection on real listed individuals with Arabic names and a birth year.
    arabic = {"muhammad", "ahmad", "abd", "hasan", "husayn", "ali", "umar", "yusuf", "ibrahim", "khalid", "said", "mustafa"}
    listed = [e for e in entries if e.entity_type == "individual" and e.dob_years
            and arabic & set(normalize(e.name).split())]
    print(f"\nDETECTION: real listed individuals with Arabic names, altered in ways the normalizer was"
          f"\nnever taught, screened with their listed birth year (and nationality when listed)\n")
    total_found = {label: 0 for label in configs}
    total = 0
    for alt_label, alter in ALTERATIONS.items():
        rng.shuffle(listed)
        probes = []
        for e in listed:
            altered = alter(e.name, rng)
            changed = normalize(altered) != normalize(e.name)
            if alter is reverse:
                changed = altered != e.name and len(e.name.split()) >= 2
            if changed:
                probes.append((e, altered))
            if len(probes) == args.probes:
                break
        total += len(probes)
        res = _run(pool, [(a, e.dob_years[0], e.nationalities[0] if e.nationalities else None) for e, a in probes],
                   alt_label.split(" (")[0])
        print(f"  {alt_label}, {len(probes)} people, e.g. '{probes[0][0].name}' -> '{probes[0][1]}'")
        for k, label in enumerate(configs):
            found = sum(e.uid in r[k] for (e, _), r in zip(probes, res))
            total_found[label] += found
            print(f"    {label:34} {fmt(found, len(probes))}")
    print(f"\n  all alterations together, {total} probes")
    for label in configs:
        print(f"    {label:34} {fmt(total_found[label], total)}")

    print(f"\nremaining false alerts with birth year + nationality ({len(remaining)}):")
    for x in remaining[:15]:
        print("  ", x)
    pool.close()
    print(f"\n({time.time() - t0:.0f}s, {args.workers} workers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
