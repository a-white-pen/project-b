"""
DB layer for the day-of run planner. Reads today's planned run (exercise.cardio_plan + the day's note)
and writes the generated run detail back to cardio_plan.plan. NOT unit-tested (no DB — house rule).

Functions:
  read_run_day(plan_date) -> dict|None    # {run_type, run_surface, status, garmin_workout_id, note} or None
  save_run_plan(plan_date, plan, run_surface, garmin_workout_id) -> None   # write cardio_plan.plan (forward-only)
"""

import logging

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)


def _active_note(notes) -> str | None:
    active = [n for n in (notes or []) if isinstance(n, dict) and n.get("active") and n.get("text")]
    return active[-1]["text"] if active else None


# Reads today's planned run: the cardio_plan row + the day's active note (a pin like "run outdoors"
# rides to the card). Returns None when there's no cardio_plan row (not a run day). status lets the
# caller skip a done/skipped day.
def read_run_day(plan_date) -> dict | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT cp.run_type, cp.run_surface, cp.status, cp.garmin_workout_id, dp.notes "
                    "FROM exercise.cardio_plan cp "
                    "LEFT JOIN health_agent.daily_plan dp ON dp.plan_date = cp.plan_date "
                    "WHERE cp.plan_date = %s", (plan_date,))
                row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    run_type, run_surface, status, garmin_workout_id, notes = row
    return {"run_type": run_type, "run_surface": run_surface, "status": status,
            "garmin_workout_id": garmin_workout_id, "note": _active_note(notes)}


# Writes the generated run detail to cardio_plan.plan (+ run_surface + garmin_workout_id), forward-only
# (only a 'planned' row). garmin_workout_id is SET explicitly (not COALESCEd): quality/fartlek store the
# pushed id; easy/long pass None to CLEAR any prior push (an edit dropped a quality run to steady).
# Input: plan = the run jsonb; run_surface (treadmill/outdoor); garmin_workout_id (quality/fartlek only).
def save_run_plan(plan_date, plan: dict, run_surface: str | None = None,
                  garmin_workout_id: str | None = None) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE exercise.cardio_plan SET plan = %s, "
                    "  run_surface = COALESCE(%s, run_surface), garmin_workout_id = %s, "
                    "  updated_at = now() WHERE plan_date = %s AND status = 'planned'",
                    (psycopg2.extras.Json(plan), run_surface, garmin_workout_id, plan_date))
    finally:
        conn.close()
    log_event(logger, logging.INFO, "run_plan_saved", plan_date=str(plan_date),
              run_type=plan.get("run_type"), pushed=garmin_workout_id is not None)
