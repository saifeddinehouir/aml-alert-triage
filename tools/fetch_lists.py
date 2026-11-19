"""Download the real sanctions lists into data/ (run this on your own machine).

    python tools/fetch_lists.py

After this, the API and screening use OFAC SDN + the UN consolidated list
instead of the fictional sample. The API server also refreshes them by itself
every LIST_REFRESH_HOURS (default 24).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from triage.lists import SOURCES, download_lists  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"


def main() -> int:
    failed = download_lists(DATA)
    for name, url in SOURCES.items():
        if name in failed:
            print(f"FAIL  {name}: {failed[name]}\n      download it manually from {url} into {DATA}/")
        else:
            print(f"ok    {name}  ({(DATA / name).stat().st_size // 1024} KB)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
