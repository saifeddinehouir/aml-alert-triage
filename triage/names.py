"""Name normalization for sanctions screening.

Screening fails most often on spelling, not on logic: "Mohammed Abdel Rahman"
on an alert, "MUHAMMAD ABD AL-RAHMAN" on the list. Everything here turns both
sides into the same comparable form before any fuzzy matching happens.
"""
from __future__ import annotations

import re
import unicodedata

# Common romanizations of Arabic names, mapped to one canonical token.
# Order does not matter: each variant maps straight to its canonical form.
_VARIANTS = {
    "canonical_muhammad": ["mohammed", "mohammad", "mohamed", "muhammed", "mohamad", "muhamad", "mohd", "mhd", "mehmet", "mamadou"],
    "canonical_ahmad": ["ahmed", "ahmet", "ahmd"],
    "canonical_abd": ["abdel", "abdul", "abdal", "abdol", "abdoul", "abdu", "abdou"],
    "canonical_yusuf": ["youssef", "yousef", "yousif", "yusef", "youcef", "joseph"],
    "canonical_husayn": ["hussein", "husein", "hussain", "hosein", "houssein", "husain"],
    "canonical_hasan": ["hassan", "hasane", "hasen"],
    "canonical_umar": ["omar", "omer", "umer"],
    "canonical_ali": ["aly", "alee"],
    "canonical_khalid": ["khaled", "khaleed", "halid"],
    "canonical_said": ["saeed", "sayed", "sayyid", "saied", "sa'id"],
    "canonical_abu": ["abou", "abo"],
    "canonical_ibrahim": ["ebrahim", "ibraheem", "brahim"],
    "canonical_mustafa": ["moustafa", "mostafa", "mustapha", "moustapha"],
    "canonical_rahman": ["rahmane", "rehman", "rahmaan"],
}
_CANON = {v: k.removeprefix("canonical_") for k, vs in _VARIANTS.items() for v in vs}
_CANON.update({k.removeprefix("canonical_"): k.removeprefix("canonical_") for k in _VARIANTS})

# Particles that carry little identity and vary wildly between sources.
_PARTICLES = {"al", "el", "ul", "bin", "ben", "ibn", "bint", "de", "da", "van", "von", "der",
              # company suffixes vary between sources and carry no identity
              "ltd", "limited", "llc", "inc", "co", "company", "corp", "corporation", "sa", "sarl", "gmbh", "plc", "fze", "fzco"}

_ARABIC_RE = re.compile(r"[؀-ۿ]")


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokens(name: str) -> list[str]:
    """Lowercase, accent-free, particle-free canonical tokens."""
    text = strip_accents(name).lower()
    text = text.replace("'", "").replace("`", "")
    # "al-Rahman", "abd-al-rahman" -> separate words
    text = re.sub(r"[^a-z؀-ۿ]+", " ", text)
    out: list[str] = []
    for tok in text.split():
        # "abdelrahman" / "abdulrahman" -> "abd" + "rahman"
        m = re.match(r"^(abd(?:el|ul|al|ol|oul|ou|u)?)(.{3,})$", tok)
        if m and m.group(2) not in _PARTICLES:
            out.append("abd")
            tok = m.group(2)
            tok = re.sub(r"^(el|al|ul|ar|ur|er)", "", tok) or tok
            if tok in ("lah", "llah"):  # "abdullah" lost the "al" of "abd allah" to the prefix
                tok = "allah"
        if tok in _PARTICLES:
            continue
        out.append(_CANON.get(tok, tok))
    return out


def normalize(name: str) -> str:
    return " ".join(tokens(name))


def has_arabic_script(name: str) -> bool:
    return bool(_ARABIC_RE.search(name))
