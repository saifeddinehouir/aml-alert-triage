"""Sanctions list loading.

Three sources share one model:
- data/sample_list.json: fictional entries, used by tests and the demo
- OFAC SDN (sdn.csv + alt.csv), downloaded by tools/fetch_lists.py
- UN Security Council consolidated list (XML), same tool
"""
from __future__ import annotations

import csv
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from pydantic import BaseModel, Field


class ListEntry(BaseModel):
    uid: str  # "OFAC:12345", "UN:QDi.001", "SAMPLE:1"
    source: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    entity_type: str = "individual"  # individual | entity | vessel | aircraft
    dob_years: list[int] = Field(default_factory=list)
    nationalities: list[str] = Field(default_factory=list)
    programs: list[str] = Field(default_factory=list)
    original_script: str | None = None

    def all_names(self) -> list[str]:
        return [self.name, *self.aliases, *([self.original_script] if self.original_script else [])]


def load_sample(path: str | Path) -> list[ListEntry]:
    return [ListEntry(**e) for e in json.loads(Path(path).read_text(encoding="utf-8"))]


def load_ofac(sdn_csv: str | Path, alt_csv: str | Path | None = None) -> list[ListEntry]:
    """OFAC SDN legacy CSV files (no header row). '-0- ' marks an empty field."""
    def clean(v: str) -> str:
        v = (v or "").strip()
        return "" if v == "-0-" else v

    entries: dict[str, ListEntry] = {}
    with open(sdn_csv, newline="", encoding="latin-1") as f:
        for row in csv.reader(f):
            if len(row) < 12:
                continue
            ent_num, name, typ, program, *_rest = row
            remarks = _remark_fields(clean(row[11]))
            typ = clean(typ).lower() or "entity"
            entries[ent_num] = ListEntry(
                uid=f"OFAC:{ent_num}", source="OFAC SDN", name=_ofac_name(clean(name)),
                entity_type=typ,
                # several programs are written "SDGT] [IFSR"
                programs=[p.strip(" []") for p in re.split(r"\]\s*\[|;", clean(program)) if p.strip(" []")],
                dob_years=sorted({y for v in remarks.get("dob", []) for y in _years_in(v)}),
                nationalities=list(dict.fromkeys(remarks.get("nationality", []) + remarks.get("citizen", []))),
            )
    if alt_csv and Path(alt_csv).exists():
        with open(alt_csv, newline="", encoding="latin-1") as f:
            for row in csv.reader(f):
                if len(row) >= 4 and row[0] in entries and clean(row[3]):
                    entries[row[0]].aliases.append(_ofac_name(clean(row[3])))
    return list(entries.values())


def _ofac_name(name: str) -> str:
    # OFAC writes individuals as "LAST, First Middle"
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name


def _remark_fields(remarks: str) -> dict[str, list[str]]:
    """'DOB 1960 to 1962; alt. DOB 1965; nationality Iran; Passport ... expires 2017'
    -> {"dob": ["1960 to 1962", "1965"], "nationality": ["Iran"]}. Only the labelled
    field is read, so passport and ID dates never count as birth years."""
    out: dict[str, list[str]] = {}
    for part in remarks.split(";"):
        m = re.match(r"\s*(?:alt\.\s*)?(DOB|nationality|citizen)\s+(.+)", part, re.I)
        if m:
            out.setdefault(m.group(1).lower(), []).append(m.group(2).strip(" ."))
    return out


def _years_in(text: str) -> list[int]:
    """Years in a date, or every year of a range like '1960 to 1962'."""
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text)]
    if len(years) == 2 and " to " in text and 0 < years[1] - years[0] <= 15:
        return list(range(years[0], years[1] + 1))
    return years


def load_un(xml_path: str | Path) -> list[ListEntry]:
    root = ET.parse(xml_path).getroot()
    out: list[ListEntry] = []
    for ind in root.iter("INDIVIDUAL"):
        parts = [(ind.findtext(t) or "").strip() for t in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME")]
        name = " ".join(p for p in parts if p)
        if not name:
            continue
        aliases = [(a.findtext("ALIAS_NAME") or "").strip() for a in ind.findall("INDIVIDUAL_ALIAS")]
        years: list[int] = []
        for d in ind.findall("INDIVIDUAL_DATE_OF_BIRTH"):
            if (d.findtext("YEAR") or "").strip().isdigit():
                years.append(int(d.findtext("YEAR")))
            span = (d.findtext("FROM_YEAR") or "").strip(), (d.findtext("TO_YEAR") or "").strip()
            if all(s.isdigit() for s in span):
                years += _years_in(f"{span[0]} to {span[1]}")
            else:
                years += [int(s) for s in span if s.isdigit()]
            date = (d.findtext("DATE") or "")[:4]
            if date.isdigit():
                years.append(int(date))
        out.append(ListEntry(
            uid=f"UN:{(ind.findtext('REFERENCE_NUMBER') or '').strip()}", source="UN Security Council",
            name=name, aliases=[a for a in aliases if a], dob_years=sorted(set(years)),
            nationalities=[(v.text or "").strip() for v in ind.findall("NATIONALITY/VALUE") if v.text],
            programs=[(ind.findtext("UN_LIST_TYPE") or "").strip()],
            original_script=(ind.findtext("NAME_ORIGINAL_SCRIPT") or "").strip() or None,
        ))
    for ent in root.iter("ENTITY"):
        name = (ent.findtext("FIRST_NAME") or "").strip()
        if name:
            out.append(ListEntry(
                uid=f"UN:{(ent.findtext('REFERENCE_NUMBER') or '').strip()}", source="UN Security Council",
                name=name, entity_type="entity",
                aliases=[(a.findtext("ALIAS_NAME") or "").strip() for a in ent.findall("ENTITY_ALIAS") if a.findtext("ALIAS_NAME")],
                programs=[(ent.findtext("UN_LIST_TYPE") or "").strip()],
            ))
    return out


def load_all(data_dir: str | Path) -> list[ListEntry]:
    """Real lists if they were downloaded, otherwise the fictional sample."""
    d = Path(data_dir)
    entries: list[ListEntry] = []
    if (d / "sdn.csv").exists():
        entries += load_ofac(d / "sdn.csv", d / "alt.csv")
    if (d / "un_consolidated.xml").exists():
        entries += load_un(d / "un_consolidated.xml")
    return entries or load_sample(d / "sample_list.json")
