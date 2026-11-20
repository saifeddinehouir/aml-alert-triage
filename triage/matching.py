"""Fuzzy sanctions screening with explainable scores.

Score = name similarity (0-100), then adjusted by secondary identifiers:
date of birth and nationality. Every adjustment is recorded so an analyst
can see exactly why a hit scored the way it did.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from rapidfuzz import fuzz, process
from rapidfuzz.distance import DamerauLevenshtein

from .lists import ListEntry
from .names import normalize, tokens

# A name only matches if the screened name's distinctive parts are all found (no
# missing surname) and most of the listed name is covered (a missing middle name is fine).
MIN_QUERY_COVERAGE = 0.8
MIN_LIST_COVERAGE = 0.6


@dataclass
class Hit:
    entry: ListEntry
    matched_name: str
    name_score: float
    score: float
    reasons: list[str] = field(default_factory=list)
    discounted: bool = False

    def as_evidence(self) -> dict:
        return {
            "list_uid": self.entry.uid,
            "source": self.entry.source,
            "listed_name": self.entry.name,
            "matched_name": self.matched_name,
            "name_score": round(self.name_score, 1),
            "score": round(self.score, 1),
            "programs": self.entry.programs,
            "listed_dob_years": self.entry.dob_years,
            "listed_nationalities": self.entry.nationalities,
            "reasons": self.reasons,
            "discounted": self.discounted,
        }


class Screener:
    def __init__(self, entries: list[ListEntry]):
        self.entries = entries
        self._names: list[str] = []
        self._owner: list[int] = []
        self._raw: list[str] = []
        self._skel: list[str] = []
        df: Counter[str] = Counter()
        for i, e in enumerate(entries):
            for n in e.all_names():
                norm = normalize(n)
                if norm:
                    self._names.append(norm)
                    self._owner.append(i)
                    self._raw.append(n)
                    self._skel.append(_skeleton_name(norm))
                    df.update(set(norm.split()))
        # Rare tokens identify a person, common ones ("muhammad", "ali", "trading") barely do.
        n_names = len(self._names)
        self._weight = lambda t: math.log(1 + n_names / (1 + df[t]))

    def screen(self, name: str, dob_year: int | None = None, nationality: str | None = None,
               threshold: float = 80, limit: int = 5) -> list[Hit]:
        query = normalize(name)
        if not query:
            return []
        # Fast C-level prefilter over every list name, then the stricter Python scorer on the shortlist.
        # Common names tie at 100 with hundreds of longer list names, so the shortlist is generous.
        # A second pass on consonant skeletons catches romanizations the variant table doesn't know.
        shortlist = {idx for _, _, idx in process.extract(query, self._names, scorer=fuzz.token_set_ratio,
                                                         limit=200, score_cutoff=threshold - 15)}
        shortlist |= {idx for _, _, idx in process.extract(_skeleton_name(query), self._skel, scorer=fuzz.token_set_ratio,
                                                          limit=200, score_cutoff=threshold - 5)}
        best: dict[int, Hit] = {}
        for idx in shortlist:
            cand = self._names[idx]
            e = self.entries[self._owner[idx]]
            pairs = _align(query, cand)
            close = {t: u for t, u in pairs.items() if _close_token(t, u)}
            ns = _name_score(" ".join(close.get(t, t) for t in query.split()), cand)
            cov_q, cov_l = _coverage(query, cand, pairs, self._weight)
            # Every part of the screened name is spelled as in the listed name, only one listed part
            # is missing, and the birth year agrees: that is a dropped middle name, not another person.
            dob_ok = bool(dob_year and e.dob_years and any(abs(dob_year - y) <= 1 for y in e.dob_years))
            missing = [u for u in cand.split() if u not in pairs.values()]
            exact = all(pairs.get(t) == t for t in query.split())
            middle_missing = (exact and len(set(query.split())) >= 2 and dob_ok
                              and (len(missing) <= 1 or (len(missing) == 2 and missing[0] == "abd")))
            if cov_q < MIN_QUERY_COVERAGE or (cov_l < MIN_LIST_COVERAGE and not middle_missing):
                ns *= min(cov_q, cov_l)
            if ns < threshold - 10:
                continue
            hit = Hit(entry=e, matched_name=self._raw[idx], name_score=ns, score=ns,
                      reasons=[f"name similarity {ns:.0f}/100 ('{name}' vs '{self._raw[idx]}')"])
            variants = [f"{t}~{u}" for t, u in close.items() if t != u]
            if variants:
                hit.reasons.append("spelling variants or typos accepted: " + ", ".join(variants))
            if cov_l < 1:
                hit.reasons.append(f"distinctive name parts covered: {cov_q:.0%} of screened name, {cov_l:.0%} of listed name"
                                   + (" (accepted: birth year matches)" if cov_l < MIN_LIST_COVERAGE else ""))
            _adjust(hit, dob_year, nationality)
            prev = best.get(self._owner[idx])
            if prev is None or hit.score > prev.score:
                best[self._owner[idx]] = hit
        # Keep strong name matches that birth year or nationality pushed under the threshold:
        # the analyst should see that they were considered and why they were discounted.
        hits = sorted((h for h in best.values() if h.score >= threshold or h.name_score >= 90), key=lambda h: -h.score)
        for h in hits:
            h.discounted = h.score < threshold
        return hits[:limit]


def _name_score(a: str, b: str, **_) -> float:
    """Order-insensitive, and tolerant of a missing middle name, but not of a missing surname."""
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return 0.0
    sort_score = fuzz.token_sort_ratio(a, b)
    set_score = fuzz.token_set_ratio(a, b)
    # token_set_ratio rewards subsets ("muhammad" vs "muhammad ali hasan" = 100); only
    # trust it when the shorter name still has at least two meaningful tokens.
    shorter = min(len(ta), len(tb))
    return max(sort_score, set_score * (0.95 if shorter >= 2 else 0.6))


def _skeleton(tok: str) -> str:
    """Consonant skeleton: what survives the vowel and spelling choices of romanization.

    Hacen/Hasan/Hassen -> hsn, Youcef/Yusuf -> ysf, Kacem/Qasim -> ksm, Ehmed/Ahmad -> ahmd."""
    t = tok.replace("ou", "u").replace("ph", "f")
    t = re.sub(r"c(?=[eiy])", "s", t).replace("c", "k").replace("q", "k")
    t = re.sub(r"([kdtg])h", r"\1", t)
    lead = "a" if t[:1] in ("a", "e", "i", "o", "u") else t[:1] if t[:1] in ("y", "w") else ""
    # a final -i/-y is the family-name suffix: Hassani is not Hasan, Alusi is not Ilyas
    tail = "i" if len(t) > 1 and t[-1] in ("i", "y") else ""
    t = re.sub(r"[aeiouyw]", "", t)
    return lead + re.sub(r"(.)\1+", r"\1", t) + tail


def _skeleton_name(name: str) -> str:
    return " ".join(_skeleton(t) for t in name.split())


def _same_token(a: str, b: str) -> bool:
    # spellings ("zawahry"/"zawahiri"), fused particles ("saleh"/"bensaleh")
    return (_close_token(a, b) or fuzz.ratio(a, b) >= 80
            or (min(len(a), len(b)) >= 5 and (a.endswith(b) or b.endswith(a))))


def _close_token(a: str, b: str) -> bool:
    """Same name part, differently written: close enough to score the name as if spelled the same."""
    if a == b:
        return True
    # one typo (letter added, dropped or swapped) in a name part long enough to stay distinctive
    if min(len(a), len(b)) >= 4 and DamerauLevenshtein.distance(a, b) <= 1:
        return True
    # romanizations missing from the variant table, when there are enough consonants to be distinctive
    sa = _skeleton(a)
    consonants = sa.removeprefix("a").removesuffix("i")
    return len(consonants) >= 3 and sa == _skeleton(b)


def _align(a: str, b: str) -> dict[str, str]:
    """Each token of `a` mapped to the token of `b` it matches, if any (exact matches first)."""
    tb = b.split()
    out: dict[str, str] = {}
    for t in a.split():
        if t in tb:
            out[t] = t
        else:
            m = max((u for u in tb if _same_token(t, u)), key=lambda u: fuzz.ratio(t, u), default=None)
            if m is not None:
                out[t] = m
    return out


def _coverage(a: str, b: str, pairs: dict[str, str], weight: Callable[[str], float]) -> tuple[float, float]:
    """Share of each name's identifying weight that the other name matches.

    "muhammad ali" sits inside thousands of list names; on its own token_set_ratio
    calls all of them 100. Weighting tokens by rarity in the list shows that only
    the common half of "issam ali muhammad abd allah" was matched. A matched token
    weighs what its listed counterpart weighs: a typo does not make a name rare."""
    ta, tb = a.split(), b.split()
    if not ta or not tb:
        return 0.0, 0.0
    w_a = [weight(pairs.get(t, t)) for t in ta]
    in_a = sum(w for t, w in zip(ta, w_a) if t in pairs)
    matched = set(pairs.values())
    in_b = sum(weight(u) for u in tb if u in matched)
    return in_a / sum(w_a), in_b / sum(weight(u) for u in tb)


def _adjust(hit: Hit, dob_year: int | None, nationality: str | None) -> None:
    e = hit.entry
    if dob_year and e.dob_years:
        if any(abs(dob_year - y) <= 1 for y in e.dob_years):
            hit.score = min(100, hit.score + 5)
            hit.reasons.append(f"birth year {dob_year} matches listed {e.dob_years} (+5)")
        else:
            hit.score -= 20
            hit.reasons.append(f"birth year {dob_year} does not match listed {e.dob_years} (-20)")
    if nationality and e.nationalities:
        if any(nationality.lower() in n.lower() or n.lower() in nationality.lower() for n in e.nationalities):
            hit.score = min(100, hit.score + 3)
            hit.reasons.append(f"nationality {nationality} matches (+3)")
        else:
            hit.score -= 10
            hit.reasons.append(f"nationality {nationality} not in listed {e.nationalities} (-10)")


__all__ = ["Screener", "Hit", "tokens"]
