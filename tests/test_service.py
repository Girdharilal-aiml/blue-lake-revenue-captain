"""
Tests for Revenue Captain Service
----------------------------------
Covers: duplicates, stale candidates, missing evidence,
        bounced routes, and idempotency (rerun safety).
"""

import json
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from service import (
    normalize,
    detect_issues,
    build_action_queue,
    run_worker,
    fingerprint,
    evidence_score,
    STALE_DAYS,
    EVIDENCE_MIN_PASS,
    EVIDENCE_MAX,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

def make_app(
    app_id="app_001",
    name="Test User",
    email="test@example.com",
    phone="+1-000-000-0000",
    route_id="route_A",
    submitted_at=None,
    evidence=None,
):
    if submitted_at is None:
        submitted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if evidence is None:
        evidence = {"resume": True, "github": "https://github.com/x", "cover_note": True}
    return {
        "id": app_id, "name": name, "email": email, "phone": phone,
        "school_route_id": route_id, "submitted_at": submitted_at,
        "evidence": evidence, "status": "pending",
    }


def make_route(route_id="route_A", active=True):
    return {
        "id": route_id, "school": "Test School", "region": "Test City",
        "contact_email": "test@school.edu", "active": active,
    }


def make_event(app_id="app_001", event_type="email_sent", timestamp=None):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"id": "evt_x", "app_id": app_id, "type": event_type, "timestamp": timestamp, "meta": {}}


def old_ts(days_ago=20):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat().replace("+00:00", "Z")


# ── Tests: fingerprint ─────────────────────────────────────────────────────────

def test_fingerprint_same_for_same_email_phone():
    a = make_app(app_id="app_001", email="sara@test.com", phone="+1-312-0000")
    b = make_app(app_id="app_002", email="sara@test.com", phone="+1-312-0000")
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_for_different_email():
    a = make_app(email="a@test.com")
    b = make_app(email="b@test.com")
    assert fingerprint(a) != fingerprint(b)


# ── Tests: evidence score ──────────────────────────────────────────────────────

def test_evidence_score_full():
    ev = {"resume": True, "github": "https://github.com/x", "cover_note": True}
    score, missing = evidence_score(ev)
    assert score == EVIDENCE_MAX
    assert missing == []


def test_evidence_score_partial():
    ev = {"resume": True, "github": None, "cover_note": False}
    score, missing = evidence_score(ev)
    assert score == 2   # resume=2, others=0
    assert "github" in missing
    assert "cover_note" in missing


def test_evidence_score_empty():
    score, missing = evidence_score({"resume": False, "github": None, "cover_note": False})
    assert score == 0
    assert len(missing) == 3


# ── Tests: duplicate detection ────────────────────────────────────────────────

def test_duplicate_secondary_flagged():
    """Later submission gets 'duplicate' flag and is skipped from the queue."""
    apps = [
        make_app(app_id="app_001", email="dup@test.com", phone="+1-111-1111", submitted_at=old_ts(5)),
        make_app(app_id="app_002", email="dup@test.com", phone="+1-111-1111", submitted_at=old_ts(2)),
    ]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)

    # Earlier submission is primary, later is secondary
    assert "duplicate_primary" in state["app_001"]["flags"]
    assert "duplicate" in state["app_002"]["flags"]
    assert state["app_002"]["duplicate_of"] == "app_001"


def test_duplicate_secondary_excluded_from_queue():
    """Non-primary duplicates must not appear in the action queue."""
    apps = [
        make_app(app_id="app_001", email="dup@test.com", submitted_at=old_ts(5)),
        make_app(app_id="app_002", email="dup@test.com", submitted_at=old_ts(2)),
    ]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)

    ids_in_queue = [x["app_id"] for x in queue]
    assert "app_002" not in ids_in_queue   # secondary skipped
    assert "app_001" in ids_in_queue       # primary included


def test_no_duplicate_when_unique():
    apps   = [make_app(app_id="app_001", email="a@test.com", phone="+1-111-0001")]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "duplicate" not in state["app_001"]["flags"]
    assert "duplicate_primary" not in state["app_001"]["flags"]


# ── Tests: stale candidate ────────────────────────────────────────────────────

def test_stale_flagged_when_old_and_clean():
    """Stale flag only appears when no higher-priority issue exists."""
    apps   = [make_app(submitted_at=old_ts(STALE_DAYS + 5))]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "stale" in state["app_001"]["flags"]


def test_stale_suppressed_by_bounced_route():
    """If route is inactive, stale is irrelevant — don't add noise."""
    apps   = [make_app(app_id="app_001", route_id="route_B", submitted_at=old_ts(30))]
    routes = [make_route(route_id="route_B", active=False)]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "stale" not in state["app_001"]["flags"]
    assert "route_inactive" in state["app_001"]["flags"]


def test_not_stale_when_recent():
    apps   = [make_app(submitted_at=old_ts(2))]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "stale" not in state["app_001"]["flags"]


def test_recent_event_prevents_stale():
    """Old submission but recent reply — should NOT be stale."""
    apps   = [make_app(app_id="app_001", submitted_at=old_ts(30))]
    events = [make_event(app_id="app_001", event_type="email_replied", timestamp=old_ts(1))]
    routes = [make_route()]
    state  = normalize(apps, events, routes)
    state  = detect_issues(state)
    assert "stale" not in state["app_001"]["flags"]


# ── Tests: missing evidence ────────────────────────────────────────────────────

def test_missing_evidence_flagged_with_specifics():
    apps   = [make_app(evidence={"resume": False, "github": None, "cover_note": False})]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "missing_evidence" in state["app_001"]["flags"]
    assert len(state["app_001"]["missing_evidence"]) == 3


def test_missing_evidence_lists_correct_fields():
    apps   = [make_app(evidence={"resume": True, "github": None, "cover_note": False})]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    # resume present (weight 2) → score=2, just below EVIDENCE_MIN_PASS=3
    missing = state["app_001"]["missing_evidence"]
    assert "github" in missing
    assert "cover_note" in missing
    assert "resume" not in missing


def test_no_missing_evidence_when_complete():
    apps   = [make_app(evidence={"resume": True, "github": "https://g.com/x", "cover_note": True})]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "missing_evidence" not in state["app_001"]["flags"]


def test_missing_evidence_suppressed_by_bounced_route():
    """If route is bounced, don't also flag missing evidence — fix route first."""
    apps   = [make_app(app_id="app_001", route_id="route_B",
                       evidence={"resume": False, "github": None, "cover_note": False})]
    routes = [make_route(route_id="route_B", active=False)]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "missing_evidence" not in state["app_001"]["flags"]
    assert "route_inactive" in state["app_001"]["flags"]


# ── Tests: bounced route ──────────────────────────────────────────────────────

def test_route_inactive_flagged():
    apps   = [make_app(route_id="route_B")]
    routes = [make_route(route_id="route_B", active=False)]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "route_inactive" in state["app_001"]["flags"]


def test_email_bounced_flagged():
    apps   = [make_app()]
    events = [make_event(event_type="email_bounced")]
    routes = [make_route()]
    state  = normalize(apps, events, routes)
    state  = detect_issues(state)
    assert "email_bounced" in state["app_001"]["flags"]


def test_route_inactive_gets_human_review_action():
    apps   = [make_app(route_id="route_B")]
    routes = [make_route(route_id="route_B", active=False)]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)
    assert queue[0]["action"] == "human_review"
    assert queue[0]["reason_code"] == "SCHOOL_ROUTE_INACTIVE"


def test_email_bounced_gets_fix_bounced_route_action():
    """Email bounce (not route-level) gets fix_bounced_route, not human_review."""
    apps   = [make_app()]
    events = [make_event(event_type="email_bounced")]
    routes = [make_route(active=True)]
    state  = normalize(apps, events, routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)
    assert queue[0]["action"] == "fix_bounced_route"
    assert queue[0]["reason_code"] == "EMAIL_BOUNCED"


def test_no_bounce_when_route_active_and_no_bounce_event():
    apps   = [make_app()]
    routes = [make_route(active=True)]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    assert "route_inactive" not in state["app_001"]["flags"]
    assert "email_bounced" not in state["app_001"]["flags"]


# ── Tests: action queue ───────────────────────────────────────────────────────

def test_human_review_is_highest_priority():
    apps = [
        make_app(app_id="app_001", email="dup@test.com", submitted_at=old_ts(5)),
        make_app(app_id="app_002", email="dup@test.com", submitted_at=old_ts(2)),
        make_app(app_id="app_003", email="stale@test.com", submitted_at=old_ts(30)),
    ]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)

    assert queue[0]["action"] == "human_review"


def test_advance_action_for_clean_record():
    apps   = [make_app()]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)
    assert queue[0]["action"] == "advance"


def test_days_inactive_in_queue():
    apps   = [make_app(submitted_at=old_ts(5))]
    routes = [make_route()]
    state  = normalize(apps, [], routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)
    assert queue[0]["days_inactive"] >= 5


def test_engagement_in_queue():
    apps   = [make_app()]
    events = [
        make_event(event_type="email_sent"),
        make_event(event_type="email_replied"),
    ]
    routes = [make_route()]
    state  = normalize(apps, events, routes)
    state  = detect_issues(state)
    queue  = build_action_queue(state)
    assert "email_replied" in queue[0]["engagement"]


# ── Tests: idempotency / rerun safety ─────────────────────────────────────────

def test_rerun_does_not_corrupt_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copytree("data", tmp / "data")
        state_path = tmp / "state.json"

        state1, queue1 = run_worker(
            applications_path=tmp / "data" / "applications.json",
            events_path=tmp / "data" / "events.json",
            routes_path=tmp / "data" / "school_routes.json",
            state_path=state_path,
        )
        state2, queue2 = run_worker(
            applications_path=tmp / "data" / "applications.json",
            events_path=tmp / "data" / "events.json",
            routes_path=tmp / "data" / "school_routes.json",
            state_path=state_path,
        )

        assert set(state1.keys()) == set(state2.keys())
        for app_id in state1:
            assert state1[app_id]["submitted_at"] == state2[app_id]["submitted_at"]
            assert state1[app_id]["fingerprint"]  == state2[app_id]["fingerprint"]
        assert len(queue1) == len(queue2)


def test_rerun_queue_order_is_stable():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        shutil.copytree("data", tmp / "data")
        state_path = tmp / "state.json"

        kwargs = dict(
            applications_path=tmp / "data" / "applications.json",
            events_path=tmp / "data" / "events.json",
            routes_path=tmp / "data" / "school_routes.json",
            state_path=state_path,
        )
        _, q1 = run_worker(**kwargs)
        _, q2 = run_worker(**kwargs)
        _, q3 = run_worker(**kwargs)

        assert [x["app_id"] for x in q1] == [x["app_id"] for x in q2] == [x["app_id"] for x in q3]