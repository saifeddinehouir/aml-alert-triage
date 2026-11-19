"""Download the real sanctions lists into data/ (run this on your own machine).

    python tools/fetch_lists.py

After this, the API and screening use OFAC SDN + the UN consolidated list
instead of the fictional sample. Re-run it regularly: both lists change often.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
SOURCES = {
    "sdn.csv": "https://www.treasury.gov/ofac/downloads/sdn.csv",
    "alt.csv": "https://www.treasury.gov/ofac/downloads/alt.csv",
    "un_consolidated.xml": "https://scsanctions.un.org/resources/xml/en/consolidated.xml",
}


def main() -> int:
    DATA.mkdir(exist_ok=True)
    failed = 0
    for name, url in SOURCES.items():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "aml-alert-triage/0.1"})
            with urllib.request.urlopen(req, timeout=120) as r:
                (DATA / name).write_bytes(r.read())
            print(f"ok    {name}  ({(DATA / name).stat().st_size // 1024} KB)")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e}\n      download it manually from {url} into {DATA}/")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
