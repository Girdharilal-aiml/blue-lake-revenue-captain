# Revenue Captain Service

A pipeline that normalizes applicant data, flags problems, and produces a ranked action queue for the hiring captain.

---

## How to run

```bash
pip install pytest

# Run the worker
python service.py

# Run tests
python -m pytest tests/ -v
```

No external dependencies beyond pytest.

---

## Project structure

```
revenue-captain/
├── service.py              # Core logic: normalize → detect → rank → save
├── data/
│   ├── applications.json
│   ├── events.json
│   └── school_routes.json
├── tests/
│   └── test_service.py     # 27 tests
└── README.md
```

---

## Architecture

Four steps:

**1. Normalize**
Reads all three input files and builds a unified state model per applicant. Each record gets:
- A fingerprint (SHA-256 of email + phone) for duplicate detection
- A weighted evidence score (resume=2, github=1, cover_note=1, max=4)
- A list of exactly which evidence fields are missing
- Last activity timestamp (most recent event, or submission time if no events)
- Days inactive (computed at run time)
- Engagement signals: positive events like `email_replied`, `email_opened` — separate from bounce/sent

**2. Detect issues**
Flags are applied in priority order. Higher-priority flags suppress lower ones — no noise stacking:

```
duplicate → route_inactive / email_bounced → missing_evidence → stale
```

- `duplicate` — non-primary submission (same email+phone fingerprint). Earliest submission is primary.
- `duplicate_primary` — the kept record when duplicates exist
- `route_inactive` — school route is marked inactive
- `email_bounced` — an `email_bounced` event exists for this applicant
- `missing_evidence` — weighted evidence score below threshold (only if no bounce)
- `stale` — no activity in 14+ days (only if no other flag exists)

**3. Build action queue**
One entry per fingerprint group — duplicate secondaries are skipped entirely. Sorted by urgency, then oldest submission first within the same tier.

| Priority | Action | Reason code | When |
|----------|--------|-------------|------|
| 1 | `human_review` | `DUPLICATE_APPLICATION` | Primary of a duplicate group |
| 1 | `human_review` | `SCHOOL_ROUTE_INACTIVE` | Route is marked inactive |
| 2 | `fix_bounced_route` | `EMAIL_BOUNCED` | Email bounced, but route itself is fine |
| 3 | `request_evidence` | `EVIDENCE_INCOMPLETE` | Missing specific fields (listed explicitly) |
| 4 | `follow_up` | `NO_RECENT_ACTIVITY` | Gone quiet, no other issues |
| 5 | `advance` | `READY` | Clean record, ready for next stage |

Each queue entry includes `days_inactive`, `engagement` signals, and a plain-English explanation naming the exact issue.

**4. Idempotent worker**
`run_worker()` is safe to call repeatedly. On rerun, `submitted_at` and `fingerprint` are frozen from the existing state file — they never get overwritten. Flags recompute fresh from source data each time, so fixing a route or adding evidence reflects immediately on next run.

---

## Key design decisions

**Weighted evidence over flat count.**
Resume is critical — you can't evaluate someone without it. GitHub and cover note are supporting. So resume=2, others=1. Threshold is 3 (resume + at least one other). A GitHub-only submission without a resume correctly fails.

**Duplicate collapsing, not listing.**
Both submissions of a duplicate pair appearing in the queue is confusing for the captain. Instead, the earliest submission becomes primary (marked `duplicate_primary`), later ones are skipped from the queue entirely but kept in state with a `duplicate_of` reference so nothing is lost.

**Route inactive vs email bounce are different problems.**
Route inactive means the school-side contact is broken — the captain needs to fix it manually, hence `human_review`. Email bounce means the candidate's address is bad — that's actionable with a retry or alternate contact, hence `fix_bounced_route`. Treating them the same loses that distinction.

**Flag suppression.**
Piling up flags like `stale + missing_evidence + bounced_route` on the same record adds noise. The captain only needs to know the most urgent thing to fix. Lower-priority flags are suppressed when a higher one already exists.

**File-based state.**
`state.json` is fine for this scale. In production I'd use SQLite for single-node or Postgres for multi-worker — mainly because concurrent workers would have a write race on the current file approach.

---

## What I'd add for production

- Postgres/SQLite instead of file state
- Dead-letter queue for records that fail normalization
- Structured JSON logging (not print statements) so you can trace a record end-to-end
- Config file for thresholds — stale days, evidence weights, priority rules shouldn't be hardcoded
- Simple HTTP endpoint so the captain can trigger a run or pull the queue without touching CLI
- CI running the test suite on every push

---

## Tests

27 tests covering every required case:

- Fingerprint stability (same identity = same hash)
- Weighted evidence scoring (full, partial, empty)
- Duplicate detection — secondary flagged, excluded from queue, primary kept
- Stale detection — positive and negative, including stale suppressed by bounce
- Missing evidence — flagged with specific field names, suppressed by bounce
- Bounced route — route inactive vs email bounce handled separately
- Action queue priority ordering
- Days inactive and engagement signals in queue output
- Idempotency: three reruns produce identical state and queue order

```bash
python -m pytest tests/ -v
# 27 passed
```