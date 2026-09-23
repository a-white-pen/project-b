"""Builds the current data packet used to plan a training week.

Functions:
  _has_active_pin — checks for an active pinned instruction
  _pin_text — reads the latest active pin text
  build_week_state — reads directives, recent training, pins, and existing plans
"""

import logging
from datetime import timedelta

from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)


# Checks whether the notes contain an active pinned instruction.
def _has_active_pin(notes) -> bool:
    return any(isinstance(n, dict) and n.get("active") and n.get("kind") == "pin" for n in (notes or []))


# Returns the text from the latest active pinned instruction.
def _pin_text(notes) -> str | None:
    pins = [n for n in (notes or []) if isinstance(n, dict) and n.get("active") and n.get("kind") == "pin"]
    return pins[-1].get("text") if pins else None


# Builds the weekly planning state for the requested local-date horizon.
def build_week_state(today, tz_name: str, horizon_days: int = 8) -> dict:
    horizon = [today + timedelta(days=i) for i in range(horizon_days)]
    end = horizon[-1]
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Reads the latest guidance carried forward from a weekly reflection.
                cur.execute(
                    "SELECT directives FROM health_agent.weekly_reflections "
                    "ORDER BY iso_week DESC LIMIT 1")
                row = cur.fetchone()
                directives = (row[0] or {}) if row else {}

                # Reads training recency and counts for fatigue and spacing decisions.
                cur.execute(
                    "SELECT MAX((started_at AT TIME ZONE %s)::date), "
                    "       COUNT(*) FILTER (WHERE (started_at AT TIME ZONE %s)::date > %s) "
                    "FROM exercise.cardio_activities "
                    "WHERE activity_category = 'run' AND started_at > now() - interval '21 days'",
                    (tz_name, tz_name, today - timedelta(days=7)))
                run_last, runs_7d = cur.fetchone()
                cur.execute(
                    "SELECT MAX((started_at AT TIME ZONE %s)::date), "
                    "       COUNT(*) FILTER (WHERE (started_at AT TIME ZONE %s)::date > %s) "
                    "FROM exercise.strength_sessions "
                    "WHERE started_at > now() - interval '21 days'",
                    (tz_name, tz_name, today - timedelta(days=7)))
                str_last, strength_7d = cur.fetchone()

                # Counts sessions already completed earlier in the current calendar week.
                monday = today - timedelta(days=today.isoweekday() - 1)
                yesterday = today - timedelta(days=1)
                cur.execute(
                    "SELECT count(*) FROM exercise.cardio_activities "
                    "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, monday, yesterday))
                done_cardio = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM exercise.strength_sessions "
                    "WHERE (started_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, monday, yesterday))
                done_strength = cur.fetchone()[0]

                # Reads existing plans and run types so changes and pins can be preserved.
                cur.execute(
                    "SELECT plan_date, activity_type, notes FROM health_agent.daily_plan "
                    "WHERE plan_date BETWEEN %s AND %s ORDER BY plan_date", (today, end))
                spine = cur.fetchall()
                cur.execute(
                    "SELECT plan_date, run_type FROM exercise.cardio_plan "
                    "WHERE plan_date BETWEEN %s AND %s", (today, end))
                run_types = {pd: rt for pd, rt in cur.fetchall()}
    finally:
        conn.close()

    existing, pins = [], []
    for plan_date, activity_type, notes in spine:
        existing.append({"date": plan_date, "activity_type": activity_type,
                         "run_type": run_types.get(plan_date)})
        if _has_active_pin(notes):
            pins.append({"date": plan_date, "activity_type": activity_type,
                         "run_type": run_types.get(plan_date), "note": _pin_text(notes)})

    recent_training = {
        "days_since_last_run": (today - run_last).days if run_last else None,
        "days_since_last_strength": (today - str_last).days if str_last else None,
        "runs_last_7d": runs_7d or 0,
        "strength_last_7d": strength_7d or 0,
    }
    done_this_week = {"cardio": done_cardio or 0, "strength": done_strength or 0}
    log_event(logger, logging.INFO, "week_state_built", pins=len(pins), existing=len(existing),
              done_this_week=done_this_week)
    return {
        "today": today,
        "horizon": horizon,
        "directives": directives,
        "recent_training": recent_training,
        "done_this_week": done_this_week,
        "pins": pins,
        "existing": existing,
    }
