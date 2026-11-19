"""Parsers for the real list formats, on tiny hand-written files in the official layouts."""
from __future__ import annotations

import time

from triage.lists import ListEntry, load_ofac, load_un
from triage.matching import Screener

SDN = '''36,"AERO-CARIBBEAN","-0- ","CUBA","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- "
7001,"DOE TEST, John Middle","individual","SDGT","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","-0- ","DOB 12 Mar 1971; nationality Testland."
'''
ALT = '''7001,101,"aka","DOE, Johnny",-0-
'''
UN = '''<?xml version="1.0" encoding="UTF-8"?>
<CONSOLIDATED_LIST><INDIVIDUALS><INDIVIDUAL>
<REFERENCE_NUMBER>QDi.999</REFERENCE_NUMBER><UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE>
<FIRST_NAME>TEST</FIRST_NAME><SECOND_NAME>PERSON</SECOND_NAME><THIRD_NAME>NAME</THIRD_NAME>
<NAME_ORIGINAL_SCRIPT>اختبار</NAME_ORIGINAL_SCRIPT>
<NATIONALITY><VALUE>Testland</VALUE></NATIONALITY>
<INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Tester Abu Test</ALIAS_NAME></INDIVIDUAL_ALIAS>
<INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><DATE>1980-01-01</DATE></INDIVIDUAL_DATE_OF_BIRTH>
</INDIVIDUAL></INDIVIDUALS><ENTITIES><ENTITY><REFERENCE_NUMBER>QDe.999</REFERENCE_NUMBER>
<UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE><FIRST_NAME>TEST FOUNDATION</FIRST_NAME></ENTITY></ENTITIES></CONSOLIDATED_LIST>
'''


def test_ofac_csv(tmp_path):
    (tmp_path / "sdn.csv").write_text(SDN, encoding="latin-1")
    (tmp_path / "alt.csv").write_text(ALT, encoding="latin-1")
    entries = {e.uid: e for e in load_ofac(tmp_path / "sdn.csv", tmp_path / "alt.csv")}
    john = entries["OFAC:7001"]
    assert john.name == "John Middle DOE TEST" and john.dob_years == [1971]
    assert john.aliases == ["Johnny DOE"] and john.programs == ["SDGT"]
    assert entries["OFAC:36"].entity_type == "entity"


def test_un_xml(tmp_path):
    (tmp_path / "un.xml").write_text(UN, encoding="utf-8")
    entries = {e.uid: e for e in load_un(tmp_path / "un.xml")}
    p = entries["UN:QDi.999"]
    assert p.name == "TEST PERSON NAME" and p.dob_years == [1980]
    assert p.aliases == ["Tester Abu Test"] and p.original_script == "اختبار"
    assert entries["UN:QDe.999"].entity_type == "entity"


def test_screening_stays_fast_on_a_full_size_list():
    import random
    random.seed(1)
    syll = ["ka", "ri", "mo", "ha", "sa", "lu", "de", "na", "to", "vi", "be", "ra"]
    word = lambda: "".join(random.choice(syll) for _ in range(3))
    entries = [ListEntry(uid=f"X:{i}", source="x", name=f"{word()} {word()} {word()}") for i in range(20000)]
    s = Screener(entries)
    t = time.perf_counter()
    for _ in range(20):
        s.screen("Muhammad Abd al-Rahman Qasimi")
    assert (time.perf_counter() - t) / 20 < 0.5  # seconds per screening
