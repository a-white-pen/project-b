"""
DB layer for the day-of strength planner. Reads today's planned strength intent (exercise.strength_plan
+ the day's note) and writes the generated plan back to strength_plan.plan (+ garmin_workout_id, meta).
Mirrors run_planner.persistence. NOT unit-tested (no DB — house rule).

Trigger-safety: a strength_plan row may only exist when daily_plan.activity_type contains 'strength'
(assert_activity trigger). The Sunday scaffold creates the row (status 'planned', plan NULL) AFTER
writing the spine, so the day-of planner only ever UPDATEs an existing row — never INSERTs a satellite —
so it can never trip the trigger. The UPDATE is forward-only (WHERE status='planned'): a done/skipped
day is left untouched.

Functions:
  read_strength_day(plan_date) -> dict|None    # {status, plan, note} or None (no strength planned)
  save_strength_plan(plan_date, plan, garmin_workout_id=None, meta=None) -> None
"""

import logging

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)


def _active_note(notes) -> str | None:
    active = [n for n in (notes or []) if isinstance(n, dict) and n.get("active") and n.get("text")]
    return active[-1]["text"] if active else None


# Reads today's planned strength: the strength_plan row + the day's active note (a pin like "go easy"
# rides to the planner as context). Returns None when there's no strength_plan row (not a strength day)
# — the trigger guarantees a row only exists when the day's activity_type includes 'strength'. status
# lets the caller skip a done/skipped day.
def read_strength_day(plan_date) -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT sp.status, sp.plan, dp.notes "
                    "FROM exercise.strength_plan sp "
                    "LEFT JOIN health_agent.daily_plan dp ON dp.plan_date = sp.plan_date "
                    "WHERE sp.plan_date = %s", (plan_date,))
                row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    status, plan, notes = row
    return {"status": status, "plan": plan, "note": _active_note(notes)}


# Writes the generated plan to strength_plan.plan (+ garmin_workout_id + meta), forward-only (only a
# 'planned' row). COALESCE on garmin_workout_id keeps a prior push id if this re-plan didn't push.
# Input: plan = the canonical plan jsonb; garmin_workout_id (set when pushed); meta = provenance.
def save_strength_plan(plan_date, plan: dict, garmin_workout_id: str | None = None,
                       meta: dict | None = None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE exercise.strength_plan SET plan = %s, "
                    "  garmin_workout_id = COALESCE(%s, garmin_workout_id), "
                    "  meta = %s, updated_at = now() "
                    "WHERE plan_date = %s AND status = 'planned'",
                    (psycopg2.extras.Json(plan), garmin_workout_id,
                     psycopg2.extras.Json(meta or {}), plan_date))
    finally:
        conn.close()
    log_event(logger, logging.INFO, "strength_plan_saved", plan_date=str(plan_date),
              focus=plan.get("focus"), exercises=len(plan.get("exercises", [])),
              pushed=garmin_workout_id is not None)
