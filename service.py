"""
Revenue Captain Service
-----------------------
Normalizes applicant data, detects issues (duplicates, stale records,
missing evidence, bounced routes), and produces a ranked action queue.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

# ── Constants ──────────────────────────────────────────────────────────────────

STALE_DAYS = 14
STATE_FILE = Path("state.json")
DATA_DIR   = Path("data")

# Evidence weights — resume is critical, others are supporting
EVIDENCE_WEIGHTS = {
    "resume":      2,
    "github":      1,
    "cover_note":  1,
}
EVIDENCE_MAX     = sum(EVIDENCE_WEIGHTS.values())   # 4
EVIDENCE_MIN_PASS = 3   # need at least resume + one other to pass

# Action priority — lower = more urgent
PRIORITY = {
    "human_review":      1,
    "fix_bounced_route": 2,
    "request_evidence":  3,
    "follow_up":         4,
    "advance":           5,
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_json(path: Path) -> list | dict:
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def days_since(ts_str: str) -> float:
    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    return (now_utc() - ts).total_seconds() / 86400


def fingerprint(app: dict) -> str:
    """Stable identity hash based on email + phone."""
    key = f"{app['email'].lower().strip()}|{app.get('phone', '').strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def evidence_score(evidence: dict) -> tuple[int, list[str]]:
    """
    Weighted score + list of what's missing.
    Returns (score, missing_fields).
    """
    score   = sum(EVIDENCE_WEIGHTS.get(k, 0) for k, v in evidence.items() if v)
    missing = [k for k, v in evidence.items() if not v]
    return score, missing


# ── Step 1: Normalize ──────────────────────────────────────────────────────────

def normalize(applications: list, events: list, routes: list) -> dict:
    """
    Build a clean state model keyed by app_id.
    """
    route_map = {r["id"]: r for r in routes}

    event_map: dict[str, list] = {}
    for evt in events:
        event_map.setdefault(evt["app_id"], []).append(evt)

    state: dict[str, dict] = {}

    for app in applications:
        app_id  = app["id"]
        fp      = fingerprint(app)
        ev_list = event_map.get(app_id, [])
        route   = route_map.get(app["school_route_id"])

        timestamps    = [e["timestamp"] for e in ev_list] + [app["submitted_at"]]
        last_activity = max(timestamps)

        has_email_bounce = any(e["type"] == "email_bounced" for e in ev_list)
        route_inactive   = (not route["active"]) if route else True

        ev_score, missing_fields = evidence_score(app.get("evidence", {}))

        # Engagement: count positive signals (opened, replied, etc.)
        positive_events = [
            e["type"] for e in ev_list
            if e["type"] not in ("email_sent", "email_bounced")
        ]

        state[app_id] = {
            "id":              app_id,
            "name":            app["name"],
            "email":           app["email"],
            "fingerprint":     fp,
            "submitted_at":    app["submitted_at"],
            "last_activity":   last_activity,
            "days_inactive":   round(days_since(last_activity), 1),
            "evidence_score":  ev_score,
            "evidence_max":    EVIDENCE_MAX,
            "missing_evidence": missing_fields,
            "evidence":        app.get("evidence", {}),
            "route_inactive":  route_inactive,
            "email_bounced":   has_email_bounce,
            "engagement":      positive_events,
            "events":          [e["type"] for e in ev_list],
            "status":          app.get("status", "pending"),
            "duplicate_of":    None,   # filled in detect_issues
            "flags":           [],
        }

    return state


# ── Step 2: Detect issues ──────────────────────────────────────────────────────

def detect_issues(state: dict) -> dict:
    """
    Flag each record. Higher-priority flags suppress lower-priority ones
    so we don't pile on noise.

    Priority order of flags:
      duplicate > bounced_route > missing_evidence > stale
    """
    # Find duplicate fingerprints — keep earliest submission as primary
    fp_groups: dict[str, list] = {}
    for app_id, rec in state.items():
        fp_groups.setdefault(rec["fingerprint"], []).append(app_id)

    # Mark non-primary duplicates
    duplicate_secondaries = set()
    for fp, ids in fp_groups.items():
        if len(ids) > 1:
            ids_sorted = sorted(ids, key=lambda x: state[x]["submitted_at"])
            primary    = ids_sorted[0]
            for secondary in ids_sorted[1:]:
                duplicate_secondaries.add(secondary)
                state[secondary]["duplicate_of"] = primary

    for app_id, rec in state.items():
        flags = []

        # 1. Duplicate (non-primary)
        if app_id in duplicate_secondaries:
            flags.append("duplicate")
            rec["flags"] = flags
            continue   # skip lower checks — duplicates go straight to human_review

        # 2. Primary of a duplicate group
        fp          = rec["fingerprint"]
        group_ids   = fp_groups[fp]
        if len(group_ids) > 1:
            flags.append("duplicate_primary")

        # 3. Bounced route or email bounce
        if rec["route_inactive"]:
            flags.append("route_inactive")
        if rec["email_bounced"]:
            flags.append("email_bounced")

        bounced = "route_inactive" in flags or "email_bounced" in flags

        # 4. Missing evidence — only add if not already blocked by bounce
        if not bounced and rec["evidence_score"] < EVIDENCE_MIN_PASS:
            flags.append("missing_evidence")

        # 5. Stale — only add if no higher-priority issue exists
        if not flags and rec["days_inactive"] > STALE_DAYS:
            flags.append("stale")

        rec["flags"] = flags

    return state


# ── Step 3: Rank action queue ──────────────────────────────────────────────────

def build_action_queue(state: dict) -> list[dict]:
    """
    One entry per fingerprint group (duplicates collapsed).
    Sorted by priority, then oldest submission first.
    """
    queue = []

    for app_id, rec in state.items():
        flags = rec["flags"]

        # Skip non-primary duplicates — they're referenced from the primary entry
        if "duplicate" in flags:
            continue

        action, reason_code, explanation = _decide_action(rec, flags)

        queue.append({
            "app_id":        app_id,
            "name":          rec["name"],
            "action":        action,
            "reason_code":   reason_code,
            "explanation":   explanation,
            "priority":      PRIORITY.get(action, 99),
            "flags":         flags,
            "days_inactive": rec["days_inactive"],
            "engagement":    rec["engagement"],
            "submitted_at":  rec["submitted_at"],
            "duplicate_of":  rec.get("duplicate_of"),
        })

    queue.sort(key=lambda x: (x["priority"], x["submitted_at"]))
    return queue


def _decide_action(rec: dict, flags: list) -> tuple[str, str, str]:
    name = rec["name"]

    # Duplicate primary — has other submissions under same identity
    if "duplicate_primary" in flags:
        return (
            "human_review",
            "DUPLICATE_APPLICATION",
            f"{name} has multiple submissions with the same email/phone. "
            f"Keep the earliest record, merge or discard the rest."
        )

    # Route inactive (school-side problem, needs manual fix)
    if "route_inactive" in flags:
        return (
            "human_review",
            "SCHOOL_ROUTE_INACTIVE",
            f"{name}'s school route is inactive — outreach won't reach the school. "
            f"Update the route contact before any further action."
        )

    # Email bounced (candidate-side problem)
    if "email_bounced" in flags:
        return (
            "fix_bounced_route",
            "EMAIL_BOUNCED",
            f"{name}'s email bounced. Verify the address and retry, "
            f"or find an alternate contact."
        )

    # Missing evidence
    if "missing_evidence" in flags:
        missing = rec.get("missing_evidence", [])
        missing_str = ", ".join(missing) if missing else "unknown fields"
        return (
            "request_evidence",
            "EVIDENCE_INCOMPLETE",
            f"{name} is missing: {missing_str}. "
            f"Evidence score {rec['evidence_score']}/{rec['evidence_max']}. "
            f"Send a targeted request for the specific items listed."
        )

    # Stale
    if "stale" in flags:
        return (
            "follow_up",
            "NO_RECENT_ACTIVITY",
            f"{name} has been inactive for {rec['days_inactive']} days. "
            f"Send a check-in — they may have lost interest or missed earlier outreach."
        )

    # Clean record
    engagement_str = (
        f"Engagement signals: {', '.join(rec['engagement'])}."
        if rec["engagement"] else "No engagement events yet."
    )
    return (
        "advance",
        "READY",
        f"{name} has full evidence (score {rec['evidence_score']}/{rec['evidence_max']}), "
        f"active route, and {rec['days_inactive']} days since last activity. "
        f"{engagement_str} Move to next stage."
    )


# ── Step 4: Idempotent worker ──────────────────────────────────────────────────

def run_worker(
    applications_path: Path = DATA_DIR / "applications.json",
    events_path:       Path = DATA_DIR / "events.json",
    routes_path:       Path = DATA_DIR / "school_routes.json",
    state_path:        Path = STATE_FILE,
) -> tuple[dict, list]:
    """
    Main entry point. Safe to run multiple times — will not corrupt state.
    submitted_at and fingerprint are frozen after first run.
    """
    apps   = load_json(applications_path)
    events = load_json(events_path)
    routes = load_json(routes_path)

    new_state = normalize(apps, events, routes)
    new_state = detect_issues(new_state)

    if state_path.exists():
        old_state = load_json(state_path)
        for app_id, old_rec in old_state.items():
            if app_id in new_state:
                new_state[app_id]["submitted_at"] = old_rec["submitted_at"]
                new_state[app_id]["fingerprint"]  = old_rec["fingerprint"]

    save_json(state_path, new_state)

    queue = build_action_queue(new_state)
    return new_state, queue


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Running Revenue Captain worker...\n")
    state, queue = run_worker()

    print(f"{'─'*60}")
    print(f"  Processed {len(state)} applications → {len(queue)} queue entries")
    print(f"{'─'*60}\n")

    print("ACTION QUEUE (ranked by urgency):\n")
    for i, item in enumerate(queue, 1):
        flags_str = ", ".join(item["flags"]) if item["flags"] else "none"
        eng_str   = ", ".join(item["engagement"]) if item["engagement"] else "none"
        print(f"  {i}. [{item['reason_code']}] {item['name']}")
        print(f"     Action      : {item['action']}")
        print(f"     Flags       : {flags_str}")
        print(f"     Days idle   : {item['days_inactive']}")
        print(f"     Engagement  : {eng_str}")
        print(f"     Why         : {item['explanation']}")
        print()

    print(f"State saved to: {STATE_FILE}")