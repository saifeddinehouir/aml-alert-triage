# AML Alert Triage

A first-pass assistant for AML analysts. It takes a transaction-monitoring alert,
screens the customer and every counterparty against sanctions lists, runs the
red-flag rules, and drafts a case summary for a human to decide on. Every step
is written to a tamper-evident audit log.

The design rule: **rules decide, the LLM only explains.** Sanctions hits, risk
levels and red flags are computed deterministically. The model writes the
narrative, must cite the evidence it uses, and its answer is rejected if it
cites evidence that does not exist, recommends closing a case with critical
evidence, or states that someone *is* sanctioned. Nothing leaves "pending
review" without a named analyst.

```
alert ─► screen customer + counterparties (OFAC SDN, UN consolidated list)
      ─► red-flag rules (structuring, pass-through, high-risk countries, profile)
      ─► evidence E1..En  ─► LLM case summary ─► validator ─┬─ ok ─► case (pending review)
                                                            └─ retry, then template fallback
analyst decision ─► audit log (hash-chained, append-only)
```

## What's in it

- **Name screening built for Arabic names** (`triage/names.py`, `triage/matching.py`).
  Romanization variants are folded to one form (Mohamed / Muhammad / Mohd,
  Abdelrahman / Abd al-Rahman, Youssef / Yusuf, Ben Saleh / bin Saleh),
  particles and company suffixes are ignored, and names in Arabic script match
  the lists' original-script field. A first name alone never triggers a hit.
- **Built for full-size lists.** Name parts are weighted by how rare they are in the
  loaded list, so "Mohamed Ali" does not hit every listed person whose longer name
  contains both words, and "Ali Reza Ahmadi" does not hit "Ali Reza Zahedi". OFAC
  birth years and nationalities are read only from their labelled remark fields, so
  passport expiry dates never count as a birth year.
- **Explainable scores.** Each hit lists its name similarity and every
  adjustment: birth year match +5 or mismatch -20, nationality +3 or -10.
  Strong name matches pushed under the threshold by identifiers are kept as
  "discounted" evidence, so the file shows the namesake was considered and why
  it was ruled out.
- **Red-flag rules** (`triage/rules.py`): cash structuring under the reporting
  threshold, funds passed straight through, payments to FATF call-for-action
  countries, high-risk onboarding rating. All thresholds are constants at the top
  of the file.
- **Validated LLM output** (`triage/llm.py`): Pydantic schema, citation checks,
  consistency checks, one retry, then a deterministic template. Works without
  an API key (mock writer) for demos and tests.
- **Audit log** (`triage/audit.py`): SQLite, each row hashed with the previous
  one. UPDATE and DELETE are blocked by triggers; if someone edits the file
  anyway, `GET /v1/audit/verify` reports the first broken row. Model answers,
  rejections, retries and analyst overrides are all logged.
- **Four-eyes on risky closes**: closing a critical case requires a written
  justification; a decided case can't be decided again.

## Results on the bundled test set

20 hand-written alerts in `data/alerts.jsonl` against a **fictional** sample list
(`data/sample_list.json`), covering romanization variants, Arabic script,
namesakes, company-suffix and typo variants, structuring, pass-through and
high-risk-country payments.

```
critical sanctions hits: TP=10 FP=0 FN=0 TN=10
recommended disposition matches label: 19/20
```

The one miss (A03): an exact name match with a birth year 25 years off. The
system recommends closing it as a namesake; the label says "request
information". Both are defensible. This is a sanity check written by the same
person who wrote the rules, not a benchmark.

## False alerts on common Moroccan names, real lists

5,000 fictional customers (random combinations of common Moroccan first and family
names, random birth year, nationality Morocco) screened against the full OFAC SDN +
UN lists (20,602 entries). Since they are invented, every alert is a false alert.
95% intervals in brackets.

| | false alerts |
|---|---|
| plain fuzzy search, `token_set_ratio` >= 80 | 21.8% [20.7-22.9] |
| plain fuzzy search, `token_sort_ratio` >= 80 (best simple method) | 5.9% [5.3-6.6] |
| normalization + scoring, name only | 1.2% [1.0-1.6] |
| normalization + scoring + birth year | 0.4% [0.2-0.6] |
| normalization + scoring + birth year + nationality | 0.2% [0.1-0.3] |

**Detection costs.** 1,200 real listed individuals with Arabic names, altered in
ways the normalizer was never taught, screened with their listed birth year:

| alteration | best plain fuzzy (`token_sort`) | normalization + scoring + birth year |
|---|---|---|
| typo (one letter added, dropped or swapped) | 94.0% | 80.3% |
| middle name dropped | 83.0% | 86.7% |
| word order reversed | 100% | 100% |
| transliteration not in the variant table (Hacen, Ehmed, Mouhamad...) | 87.0% | 43.7% |
| **all** | **91.0%** | **77.7%** |

So: false alerts drop from 5.9% to 0.4%, but on spellings it was not built for,
the screener finds 77.7% of listed people against 91.0% for the best plain fuzzy
search. Known cause: a word that appears nowhere in the list (a typo, an unknown
transliteration) gets the highest rarity weight, so the coverage rule treats it as
a missing distinctive name part and rejects the match.

The remaining false alerts are near-exact namesakes. Example: "Noureddine Idrissi",
born 1965, against the UN's "Drissi Noureddine", born 1964. The nationality mismatch
(Morocco vs Tunisia) costs 10 points but the score stays at 90, so the analyst still
sees it.

Limits: the names are invented; the matching thresholds were tuned on similar names,
so this is not an independent test set.

```bash
python tools/false_alert_benchmark.py --n 5000 --seed 42   # ~15 min on 7 cores, no API key needed
```

## Run it

```bash
pip install -e ".[dev]"
pytest                      # 22 tests, no API key needed
python evaluate.py          # scores the 20 alerts with the mock writer
python evaluate.py --llm    # same, with Claude writing the summaries (needs ANTHROPIC_API_KEY)

python tools/fetch_lists.py # download OFAC SDN + UN lists into data/ (optional)
uvicorn triage.api:app --port 8000
```

```bash
curl -s localhost:8000/v1/screen -H 'content-type: application/json' \
     -d '{"name": "Mohamed Abdelrahman Kasimi", "dob_year": 1971}'

curl -s localhost:8000/v1/alerts/triage -H 'content-type: application/json' \
     -d "$(sed -n 9p data/alerts.jsonl | python -c 'import sys,json;print(json.dumps(json.load(sys.stdin)["alert"]))')"
```

| Endpoint | |
|---|---|
| `POST /v1/screen` | screen one name, with optional birth year and nationality |
| `POST /v1/alerts/triage` | full triage, returns the draft case |
| `POST /v1/cases/{id}/decision` | analyst decision: escalate, close, request_information |
| `GET /v1/cases/{id}/audit` | every logged event for a case |
| `GET /v1/lists` | loaded list sizes, their age, refresh interval |
| `GET /v1/audit/verify` | checks the whole hash chain |

| Variable | Default | |
|---|---|---|
| `ANTHROPIC_API_KEY` | | enables the Claude writer; mock otherwise |
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | |
| `DATA_DIR` | `./data` | real lists are used when present, the sample otherwise |
| `AUDIT_DB` | `audit.sqlite3` | |
| `LIST_REFRESH_HOURS` | `24` | the server re-downloads OFAC + UN lists when older than this and swaps them in without a restart; failures keep the old lists and retry after an hour; `0` turns it off |

Settings can also go in a `.env` file in the project root (git-ignored).

## Not done yet

- Adverse-media search (news screening) as an extra evidence source.
- Case storage is in memory; the audit log is the persistent record.
- The high-risk country list is a constant and must be reviewed after each FATF plenary.
- Arabic-script names only match list entries that publish an original-script
  name (the UN list does, OFAC mostly doesn't); transliterating Arabic script to
  Latin before matching is the next step.

## Layout

```
triage/
  names.py      normalization, romanization variants
  matching.py   screening and explainable scores
  lists.py      OFAC SDN / UN XML / sample loaders
  rules.py      red-flag rules, risk level
  llm.py        prompt, writers, output validator
  service.py    triage pipeline, analyst decisions
  audit.py      hash-chained audit log
  api.py        FastAPI app
evaluate.py     scores data/alerts.jsonl
tools/fetch_lists.py
tools/false_alert_benchmark.py
```

All people and companies in `data/` are fictional.

MIT licensed.
