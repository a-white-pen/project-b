"""
Exercise reconciler (BRIEF §6 "Activity lands / reconcile"): matches each PAST planned day to the
device actuals (exercise.cardio_activities / strength_sessions) on the LOCAL date, and captures
unplanned actuals.

  PLANNED satellite + a same-kind actual that day  -> status 'done' + link the actual.
  PLANNED satellite + NO actual, day is over (date < today, or today past the cutoff hour) -> 'skipped'.
  PLANNED satellite + NO actual, still today before the cutoff -> left 'planned' (still time).
  SKIPPED satellite + a same-kind actual appears later (delayed device sync) -> resurrected to 'done'.
  ACTUAL with NO satellite that day (incl. a mismatch — planned strength, did cardio) -> a NEW satellite
    status 'unplanned' (so it counts toward the weekly tally). Trigger-safe: the kind is added to
    daily_plan.activity_type FIRST (assert_activity), then the satellite is inserted.
  'other' (yoga/pilates/climbing) has no satellite — counted toward neither, not touched here.

The decision logic `plan_reconciliation` is PURE + unit-tested; the DB read/apply + orchestration are
exercised live (no DB unit tests — house rule) and reviewed adversarially. Read + decide + apply run in
ONE transaction (no TOCTOU). Idempotent: only 'planned'/'skipped' rows move, and an unplanned satellite
is created only when NO satellite exists for that (kind, date).

NOTE: every exercise.cardio_activities row counts as cardio, INCLUDING plain walks (activity_category
'walk') — the brief's "all cardio kinds count". If casual walks should be excluded from the cardio
tally, add a category filter in _read_inputs + week_tally (a B decision — walks and hikes share 'walk').

Functions:
  plan_reconciliation(existing, cardio_by_date, strength_by_date, today, cutoff_passed) -> dict  # pure
  reconcile_exercise(now_utc=None, lookback_days=9) -> dict   # orchestration; returns counts
  week_tally(start_date, end_date, tz_name) -> dict           # actual sessions by kind (for nudges)
"""

import logging
from datetime import datetime, time, timedelta, timezone

from system.db import get_connection
from system.logging import log_event
from system.timezone import get_timezone

logger = logging.getLogger(__name__)

# "none by 22:00 -> skipped". The cutoff is evaluated in B's LOCAL tz (get_timezone — Bangkok normally),
# the same tz used for the local-date bucketing and by the rest of the system (reflection/state).
_CUTOFF_HOUR = 22
# kind -> (satellite table, the completed-actual FK column)
_SAT = {
    "cardio": ("exercise.cardio_plan", "completed_cardio_activity_id"),
    "strength": ("exercise.strength_plan", "completed_strength_session_id"),
}


# Decides the reconciliation moves. PURE (no DB/clock). Input: existing = {(kind, date): status} for
# every satellite in the window with date <= today; cardio_by_date / strength_by_date = {date:
# [actual_id, ...]} earliest-first; today; cutoff_passed (now-local past the cutoff hour). Output:
# {done: [(kind, date, actual_id)], skipped: [(kind, date)], unplanned: [(kind, date, actual_id)]}.
def plan_reconciliation(existing: dict, cardio_by_date: dict, strength_by_date: dict,
                        today, cutoff_passed: bool) -> dict:
    actuals = {"cardio": cardio_by_date, "strength": strength_by_date}
    done, skipped, unplanned = [], [], []

    # 1) Existing satellites: stamp planned->done/skipped; resurrect a skipped day if an actual landed.
    for (kind, date), status in existing.items():
        ids = actuals[kind].get(date)
        if status == "planned":
            if ids:
                done.append((kind, date, ids[0]))
            elif date < today or (date == today and cutoff_passed):
                skipped.append((kind, date))
            # else: today, not past cutoff -> leave planned
        elif status == "skipped" and ids:
            done.append((kind, date, ids[0]))   # delayed device sync -> the day did happen
        # done / unplanned: already terminal, leave

    # 2) Unplanned actuals: an actual on a day with NO satellite of that kind (covers the mismatch case).
    for kind in ("cardio", "strength"):
        for date, ids in actuals[kind].items():
            if date <= today and ids and (kind, date) not in existing:
                unplanned.append((kind, date, ids[0]))

    return {"done": done, "skipped": skipped, "unplanned": unplanned}


# Reads the reconcile inputs for [start, end] (end is normally today) on the SHARED cursor. Returns
# (existing, cardio_by_date, strength_by_date). existing covers ALL satellite statuses so
# plan_reconciliation can tell a fresh unplanned actual from an already-reconciled one. Actuals are
# bucketed by LOCAL date, earliest-first.
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


# Applies the decisions on the SHARED cursor, trigger-safely. done moves a planned OR skipped row
# (a late sync resurrects a skipped day); skipped moves only a planned row; both forward-only-safe and
# idempotent via the status guard. For an unplanned actual: add the kind to daily_plan.activity_type
# FIRST (creating the row if absent, dropping a bare 'rest'), THEN insert the 'unplanned' satellite.
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
        # spine first (assert_activity): ensure activity_type has the kind, drop a bare 'rest'.
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


# Reconciles the trailing window ending today (read + decide + apply in ONE transaction). Lazy on
# /week, and reusable by the Sun/1pm crons + the device-webhook nudge. Returns {done, skipped, unplanned}.
def reconcile_exercise(now_utc: datetime | None = None, lookback_days: int = 9) -> dict:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    cutoff_passed = now_local.time() >= time(_CUTOFF_HOUR, 0)
    start = today - timedelta(days=lookback_days)   # inclusive window [today-lookback, today]
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


# Weekly 2+2 tally = ACTUAL sessions by kind in [start, end] (planned or not), for nudges only — NOT
# calibration (BRIEF §5/§6). Counts rows in the actuals tables by local date.
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
