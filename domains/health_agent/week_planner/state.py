"""
Builds the `state` dict the week scaffold feeds to planner.plan_week (the Gemini Pro proposal).

Reads: the latest weekly_reflections (target + carry-forward directives), the current 7d-avg weight,
recent training load (cardio_activities + strength_sessions — for the LLM's fatigue/spacing sense),
B's active PINS (locked days from daily_plan.notes), and the existing daily_plan rows in the horizon
(for the re-plan diff). NOT unit-tested here (no DB — house rule); reviewed adversarially + run live.

state shape:
  {today, horizon: [date], weekly_target, weight_kg, directives, recent_training,
   done_this_week: {cardio, strength}, pins, existing}

Functions:
  build_week_state(today, tz_name, horizon_days) -> dict
"""

import logging
from datetime import timedelta

from system.db import get_connection
from domains.health_agent.goals import nutrition_config
from system.logging import log_event

logger = logging.getLogger(__name__)


# Active pin = a daily_plan.notes entry with kind='pin' and active truthy.
def _has_active_pin(notes) -> bool:
    return any(isinstance(n, dict) and n.get("active") and n.get("kind") == "pin" for n in (notes or []))


def _pin_text(notes) -> str | None:
    pins = [n for n in (notes or []) if isinstance(n, dict) and n.get("active") and n.get("kind") == "pin"]
    return pins[-1].get("text") if pins else None


# Builds the planning state for [today, today+horizon_days-1]. tz_name is the IANA tz for local-day
# math on the activity reads. Output: the state dict for planner.plan_week.
def build_week_state(today, tz_name: str, horizon_days: int = 8) -> dict:
    horizon = [today + timedelta(days=i) for i in range(horizon_days)]
    end = horizon[-1]
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # latest reflection target + directives (fallback to the cold-start seed target).
                cur.execute(
                    "SELECT target_kcal, directives FROM health_agent.weekly_reflections "
                    "WHERE target_kcal IS NOT NULL ORDER BY iso_week DESC LIMIT 1")
                row = cur.fetchone()
                if row and row[0] is not None:
                    weekly_target, directives = int(row[0]), (row[1] or {})
                else:
                    n = nutrition_config()
                    weekly_target, directives = int(n["seed_maintenance"]) - int(n["DEFICIT"]), {}

                # current ~7d-avg weight.
                cur.execute(
                    "SELECT AVG(weight_kg) FROM b.weight_measurements "
                    "WHERE (measured_at AT TIME ZONE %s)::date > %s",
                    (tz_name, today - timedelta(days=7)))
                w = cur.fetchone()[0]
                weight_kg = float(w) if w is not None else None

                # recent training: days-since-last + last-7d counts, for runs and strength.
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

                # Sessions ALREADY done THIS calendar week before today (Mon..today-1) — the gap-to-2+2:
                # a mid-week roll only needs to fill the remainder of the week's 2+2.
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

                # existing daily_plan rows in the horizon + their planned run_type; flag pins.
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
    log_event(logger, logging.INFO, "week_state_built",
              weekly_target=weekly_target, has_weight=weight_kg is not None,
              pins=len(pins), existing=len(existing), done_this_week=done_this_week)
    return {
        "today": today,
        "horizon": horizon,
        "weekly_target": weekly_target,
        "weight_kg": weight_kg,
        "directives": directives,
        "recent_training": recent_training,
        "done_this_week": done_this_week,
        "pins": pins,
        "existing": existing,
    }
