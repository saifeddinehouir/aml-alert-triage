"""False-alert rate on common Moroccan names against the real OFAC + UN lists.

    python tools/fetch_lists.py              # once
    python tools/false_alert_benchmark.py [--n 500] [--seed 42]

Generates fictional customers (random first name + family name + birth year), so
every alert they raise is a false alert. Compares a plain fuzzy search with this
project's screener, with and without the birth year. To check that fewer alerts
does not just mean a blind screener, it also re-spells real listed individuals
(Muhammad -> Mohamed, Abd al-Rahman -> Abderrahmane...) and counts how many are
still found. No LLM calls.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
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

# Re-spellings applied to real listed names for the detection check (lowercase word -> choices).
RESPELL = {
    "muhammad": ["Mohamed", "Mohammed", "Mohamad"], "mohammad": ["Mohamed", "Muhammad"], "mohammed": ["Mohamed", "Muhammad"],
    "ahmad": ["Ahmed"], "ahmed": ["Ahmad"], "hasan": ["Hassan"], "hassan": ["Hasan"], "husayn": ["Hussein", "Hussain"],
    "husain": ["Hussein"], "hussein": ["Husayn", "Hussain"], "yusuf": ["Youssef", "Yousef"], "umar": ["Omar"],
    "omar": ["Umar"], "said": ["Saeed", "Sayed"], "mustafa": ["Moustafa", "Mostafa"], "ibrahim": ["Brahim", "Ebrahim"],
    "khalid": ["Khaled"], "khaled": ["Khalid"], "abdullah": ["Abdallah", "Abd Allah"], "abdallah": ["Abdullah"],
}


def customers(n: int, rng: random.Random) -> list[tuple[str, int]]:
    out: dict[str, int] = {}
    while len(out) < n:
        pool = rng.choice([MALE, FEMALE])
        first = rng.choice(pool)
        if rng.random() < 0.2:
            first += " " + rng.choice([p for p in pool if p != first])
        out.setdefault(f"{first} {rng.choice(FAMILY)}", rng.randint(1950, 2004))
    return list(out.items())


def respell(name: str, rng: random.Random) -> str:
    def word(m: re.Match) -> str:
        w = m.group(0)
        if w.lower() in RESPELL:
            return rng.choice(RESPELL[w.lower()])
        return w
    s = re.sub(r"[A-Za-z']+", word, name)
    s = re.sub(r"\b[Aa]bd[ -]?al-?(\w)", lambda m: "Abdel" + m.group(1).lower(), s)   # Abd al-Rahman -> Abdelrahman
    s = re.sub(r"\b[Aa][Ll]-", rng.choice(["El ", "Al ", ""]), s)                     # al-X -> El X / X
    return " ".join(w.capitalize() for w in s.split())


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    entries = load_all(ROOT / "data")
    if not any(e.source != "Sample list (fictional)" for e in entries):
        print("real lists not found, run tools/fetch_lists.py first")
        return 1
    screener = Screener(entries)
    naive_set, naive_sort = Naive(entries, fuzz.token_set_ratio), Naive(entries, fuzz.token_sort_ratio)

    def ours(name: str, dob: int | None, nat: str | None = None) -> set[str]:
        return {h.entry.uid for h in screener.screen(name, dob, nat) if not h.discounted}

    configs = {
        "plain fuzzy, token_set_ratio >= 80": lambda n, y, uid=None: naive_set.hits(n),
        "plain fuzzy, token_sort_ratio >= 80": lambda n, y, uid=None: naive_sort.hits(n),
        "normalization + scoring, name only": lambda n, y, uid=None: ours(n, None),
        "normalization + scoring + birth year": lambda n, y, uid=None: ours(n, y),
    }

    t0 = time.time()
    people = customers(args.n, rng)
    print(f"\nFALSE ALERTS: {len(people)} fictional Moroccan customers vs "
          f"{len(entries):,} OFAC + UN entries (every alert is false)\n")
    examples: dict[str, list[str]] = {}
    for label, run in configs.items():
        flagged = 0
        for name, dob in people:
            h = run(name, dob)
            if h:
                flagged += 1
                examples.setdefault(label, []).append(f"{name} ({dob}) -> {len(h)} hit(s)")
        print(f"  {label:42} {flagged:4}/{len(people)}  {100 * flagged / len(people):5.1f}%")

    # Detection: real listed individuals with Arabic names and a birth year, re-spelled.
    arabic = {"muhammad", "ahmad", "abd", "hasan", "husayn", "ali", "umar", "yusuf", "ibrahim", "khalid", "said", "mustafa"}
    pool = [e for e in entries if e.entity_type == "individual" and e.dob_years
            and arabic & set(normalize(e.name).split())]
    rng.shuffle(pool)
    probes = []
    for e in pool:
        alt = respell(e.name, rng)
        if Naive.clean(alt) != Naive.clean(e.name):
            probes.append((e, alt))
        if len(probes) == 300:
            break
    print(f"\nDETECTION: {len(probes)} real listed individuals with Arabic names, re-spelled "
          f"(e.g. '{probes[0][0].name}' -> '{probes[0][1]}'), with their listed birth year\n")
    for label, run in configs.items():
        found = sum(e.uid in run(alt, e.dob_years[0]) for e, alt in probes)
        print(f"  {label:42} {found:4}/{len(probes)}  {100 * found / len(probes):5.1f}%")

    print("\nexamples of remaining false alerts:")
    for label in list(configs)[-1:]:
        for x in examples.get(label, [])[:8]:
            print("  ", x)
    print(f"\n({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
