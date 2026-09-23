"""Matches planned cardio and strength days to completed activities.

Functions:
  plan_reconciliation — decides which plans are done, skipped, or unplanned
  _read_inputs — reads plans and completed activities for a date range
  _apply — applies reconciliation changes in one transaction
  reconcile_exercise — reads actuals and applies reconciliation changes
  week_tally — counts completed cardio and strength sessions in a date range
"""

import logging
from datetime import datetime, time, timedelta, timezone

from system.db import get_connection
from system.logging import log_event
from system.timezone import get_timezone

logger = logging.getLogger(__name__)

# Marks an unfinished plan skipped after this local hour.
_CUTOFF_HOUR = 22
# Maps each activity kind to its plan table and completed-activity column.
_SAT = {
    "cardio": ("exercise.cardio_plan", "completed_cardio_activity_id"),
    "strength": ("exercise.strength_plan", "completed_strength_session_id"),
}


# Decides which existing plans are done or skipped and which actuals were unplanned.
# Returns separate lists for each change type without writing to the database.
def plan_reconciliation(existing: dict, cardio_by_date: dict, strength_by_date: dict,
                        today, cutoff_passed: bool) -> dict:
    actuals = {"cardio": cardio_by_date, "strength": strength_by_date}
    done, skipped, unplanned = [], [], []

    # A late activity can change a skipped plan to done.
    for (kind, date), status in existing.items():
        ids = actuals[kind].get(date)
        if status == "planned":
            if ids:
                done.append((kind, date, ids[0]))
            elif date < today or (date == today and cutoff_passed):
                skipped.append((kind, date))
            # Leaves today's plan open until the cutoff.
        elif status == "skipped" and ids:
            done.append((kind, date, ids[0]))

    # Creates an unplanned record when an activity has no plan of the same kind.
    for kind in ("cardio", "strength"):
        for date, ids in actuals[kind].items():
            if date <= today and ids and (kind, date) not in existing:
                unplanned.append((kind, date, ids[0]))

    return {"done": done, "skipped": skipped, "unplanned": unplanned}


# Reads plan statuses and completed activities for the date range using the caller's cursor.
def _read_inputs(cur, start, end, tz_name: str):
    cur.execute(
        "SELECT 'cardio' AS kind, plan_date, status FROM exercise.cardio_plan "
        "WHERE plan_date BETWEEN %s AND %s "
        "UNION ALL "
        "SELECT 'strength', plan_date, status FROM exercise.strength_plan "
        "WHERE plan_date BETWEEN %s AND %s",
        (start, end, start, end))
    existing = {(k, d): s for k, d, s in cur.fetchall()}

    cur.execute(
        "SELECT (started_at AT TIME ZONE %s)::date AS d, cardio_activity_id "
        "FROM exercise.cardio_activities "
        "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s ORDER BY started_at",
        (tz_name, tz_name, start, end))
    cardio_by_date: dict = {}
    for d, aid in cur.fetchall():
        cardio_by_date.setdefault(d, []).append(aid)

    cur.execute(
        "SELECT (started_at AT TIME ZONE %s)::date AS d, strength_session_id "
        "FROM exercise.strength_sessions "
        "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s ORDER BY started_at",
        (tz_name, tz_name, start, end))
    strength_by_date: dict = {}
    for d, sid in cur.fetchall():
        strength_by_date.setdefault(d, []).append(sid)
    return existing, cardio_by_date, strength_by_date


# Applies reconciliation changes using the caller's transaction.
# Adds an activity to the daily plan before inserting an unplanned detail row.
def _apply(cur, decisions: dict) -> None:
    for kind, date, actual_id in decisions["done"]:
        table, col = _SAT[kind]
        cur.execute(
            f"UPDATE {table} SET status='done', {col}=%s, updated_at=now() "
            "WHERE plan_date=%s AND status IN ('planned', 'skipped')",
            (actual_id, date))
    for kind, date in decisions["skipped"]:
        table, _ = _SAT[kind]
        cur.execute(
            f"UPDATE {table} SET status='skipped', updated_at=now() "
            "WHERE plan_date=%s AND status='planned'",
            (date,))
    for kind, date, actual_id in decisions["unplanned"]:
        table, col = _SAT[kind]
        # Adds the activity to the daily plan before the database trigger checks it.
        cur.execute("SELECT activity_type FROM health_agent.daily_plan WHERE plan_date=%s", (date,))
        row = cur.fetchone()
        if row:
            new_at = sorted({a for a in (row[0] or []) if a != "rest"} | {kind})
            cur.execute(
                "UPDATE health_agent.daily_plan SET activity_type=%s, updated_at=now() "
                "WHERE plan_date=%s", (new_at, date))
        else:
            cur.execute(
                "INSERT INTO health_agent.daily_plan (plan_date, activity_type, meta) "
                "VALUES (%s, %s, '{\"source\":\"reconcile\"}'::jsonb)", (date, [kind]))
        cur.execute(
            f"INSERT INTO {table} (plan_date, status, {col}) VALUES (%s, 'unplanned', %s) "
            "ON CONFLICT (plan_date) DO NOTHING", (date, actual_id))


# Reconciles recent plans and actual activities in one transaction.
# Returns counts for done, skipped, and unplanned changes.
def reconcile_exercise(now_utc: datetime | None = None, lookback_days: int = 9) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    cutoff_passed = now_local.time() >= time(_CUTOFF_HOUR, 0)
    start = today - timedelta(days=lookback_days)
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                existing, cardio_by_date, strength_by_date = _read_inputs(cur, start, today, str(tz))
                decisions = plan_reconciliation(existing, cardio_by_date, strength_by_date,
                                                today, cutoff_passed)
                _apply(cur, decisions)
    finally:
        conn.close()
    summary = {k: len(v) for k, v in decisions.items()}
    log_event(logger, logging.INFO, "exercise_reconciled", today=str(today),
              cutoff_passed=cutoff_passed, **summary)
    return summary


# Counts completed cardio and strength sessions by local date.
def week_tally(start_date, end_date, tz_name: str) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM exercise.cardio_activities "
                    "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, start_date, end_date))
                cardio = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM exercise.strength_sessions "
                    "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, start_date, end_date))
                strength = cur.fetchone()[0]
    finally:
        conn.close()
    return {"cardio": cardio or 0, "strength": strength or 0}
