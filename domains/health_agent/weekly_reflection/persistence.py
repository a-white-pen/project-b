"""Database reads and writes for the weekly reflection.

Functions:
  read_weight_band_status — reads the recent average weight
  upsert_weekly_reflection — saves one weekly reflection
  _to_kg — converts a stored strength load to kilograms
  read_latest_quality_run — reads the latest completed quality or fartlek run
  read_strength_sets — reads normalized strength sets for a date range
  read_egg_count — counts logged eggs for a date range
  read_meal_spend — reads planned, eaten, and priced meal totals
  read_fish_count — counts logged fish entries for a date range
  read_goal_inputs — combines training and habit inputs for the reflection
"""

import logging
from datetime import date, timedelta

import psycopg2.extras

from domains.health_agent.meal_planner.persistence import read_protein_tally
from domains.health_agent.weekly_reflection import goal_progress as gp
from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)

_LB_TO_KG = 0.45359237
# Uses equal 28-day periods for strength comparisons.
_STRENGTH_WINDOW_DAYS = 28


# Reads the mean of the last seven local-day weight averages.
# Returns kilograms, or None when no weight was logged.
def read_weight_band_status(today: date, tz_name: str) -> float | None:
    window_start = today - timedelta(days=6)
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT AVG(day_weight) FROM ("
                    "  SELECT (measured_at AT TIME ZONE %s)::date AS d, AVG(weight_kg) AS day_weight "
                    "  FROM b.weight_measurements "
                    "  WHERE (measured_at AT TIME ZONE %s)::date BETWEEN %s AND %s "
                    "  GROUP BY d"
                    ") daily",
                    (tz_name, tz_name, window_start, today),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    avg7 = float(row[0]) if row and row[0] is not None else None
    log_event(logger, logging.INFO, "weight_band_status_read", has_weight=avg7 is not None)
    return avg7


# Saves the narrative and carry-forward guidance for one ISO week.
def upsert_weekly_reflection(iso_week: str, narrative: str | None = None,
                             directives: dict | None = None) -> None:
    directives = directives or {}
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO health_agent.weekly_reflections "
                    "(iso_week, narrative, directives, updated_at) "
                    "VALUES (%s, %s, %s, now()) "
                    "ON CONFLICT (iso_week) DO UPDATE SET "
                    # Keeps the previous narrative and guidance when new narration fails.
                    "  narrative = COALESCE(EXCLUDED.narrative, health_agent.weekly_reflections.narrative), "
                    "  directives = CASE WHEN EXCLUDED.narrative IS NULL "
                    "               THEN health_agent.weekly_reflections.directives ELSE EXCLUDED.directives END, "
                    "  updated_at = now()",
                    (iso_week, narrative, psycopg2.extras.Json(directives)),
                )
    finally:
        conn.close()
    log_event(logger, logging.INFO, "weekly_reflection_upserted", iso_week=iso_week)


# Converts a stored strength load to kilograms.
def _to_kg(weight, unit):
    if weight is None:
        return None
    w = float(weight)
    if unit and unit.strip().lower().startswith("lb"):
        return round(w * _LB_TO_KG, 2)
    return w


# Reads the most recent completed quality or fartlek run.
# Returns distance and duration, or None when no matching run exists.
def read_latest_quality_run():
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ca.distance_m, ca.duration_seconds "
                    "FROM exercise.cardio_plan cp "
                    "JOIN exercise.cardio_activities ca "
                    "  ON ca.cardio_activity_id = cp.completed_cardio_activity_id "
                    "WHERE cp.run_type IN ('quality', 'fartlek') "
                    "  AND cp.status = 'done' "
                    "  AND cp.completed_cardio_activity_id IS NOT NULL "
                    "ORDER BY ca.started_at DESC LIMIT 1"
                )
                row = cur.fetchone()
    finally:
        conn.close()
    if not row or row[0] is None or row[1] is None:
        return None
    return float(row[0]), float(row[1])


# Reads strength sets within the local-date range, preferring reported load and reps.
# Returns each exercise with its effective kilograms and reps.
def read_strength_sets(start_date, end_date, tz_name):
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT ss.exercise_name, "
                    "       COALESCE(ss.weight_reported, ss.weight_recorded), "
                    "       CASE WHEN ss.weight_reported IS NOT NULL "
                    "            THEN ss.weight_reported_unit ELSE ss.weight_recorded_unit END, "
                    "       COALESCE(ss.reps_reported, ss.reps_recorded) "
                    "FROM exercise.strength_sets ss "
                    "JOIN exercise.strength_sessions sess "
                    "  ON sess.strength_session_id = ss.strength_session_id "
                    "WHERE (sess.started_at AT TIME ZONE %s)::date >= %s "
                    "  AND (sess.started_at AT TIME ZONE %s)::date < %s",
                    (tz_name, start_date, tz_name, end_date),
                )
                rows = cur.fetchall()
    finally:
        conn.close()
    return [
        {"exercise_name": ex, "weight_kg": _to_kg(w, unit),
         "reps": int(reps) if reps is not None else None}
        for ex, w, unit, reps in rows
    ]


# Counts eggs logged in the local-date range.
# Uses numeric piece-like quantities when present and otherwise counts one egg entry.
def read_egg_count(start_date, end_date, tz_name) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(SUM("
                    "  CASE WHEN (food_meta->'qty'->>'amount') ~ '^[0-9.]+$' "
                    "            AND lower(COALESCE(food_meta->'qty'->>'unit', '')) "
                    "                IN ('', 'piece', 'pieces', 'egg', 'eggs', 'count', 'pcs', 'unit') "
                    "       THEN (food_meta->'qty'->>'amount')::numeric ELSE 1 END), 0) "
                    "FROM nutrition.food_log "
                    "WHERE food_item ILIKE '%%egg%%' "
                    "  AND (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, start_date, end_date),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    return int(round(float(row[0]))) if row and row[0] is not None else 0


# Reads planned and eaten meal counts plus priced eaten items for the week.
# Returns planned, eaten, and spent_thb totals.
def read_meal_spend(week_start, week_end) -> dict:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), COUNT(*) FILTER (WHERE status = 'ate') "
                    "FROM nutrition.meal_plan WHERE plan_date BETWEEN %s AND %s",
                    (week_start, week_end),
                )
                planned, eaten = cur.fetchone()
                cur.execute(
                    "SELECT COALESCE(SUM((it->>'price_thb')::numeric), 0) "
                    "FROM nutrition.meal_plan mp, jsonb_array_elements(mp.items) it "
                    "WHERE mp.status = 'ate' AND mp.plan_date BETWEEN %s AND %s "
                    "  AND (it->>'price_thb') ~ '^[0-9.]+$'",
                    (week_start, week_end),
                )
                spent_thb = cur.fetchone()[0]
    finally:
        conn.close()
    return {"planned": int(planned or 0), "eaten": int(eaten or 0),
            "spent_thb": float(spent_thb or 0)}


# Counts food-log entries marked with fish in the local-date range.
def read_fish_count(start_date, end_date, tz_name) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM nutrition.food_log "
                    "WHERE 'fish' = ANY(protein_source) "
                    "  AND (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s",
                    (tz_name, start_date, end_date),
                )
                row = cur.fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


# Combines run, strength, egg, fish, protein, and meal-spend inputs for the reflection.
# The week range is inclusive from Monday through Sunday.
def read_goal_inputs(today: date, tz_name: str, week_start: date, week_end: date) -> dict:
    qrun = read_latest_quality_run()
    run = gp.ten_k_goal_progress(*qrun) if qrun else None

    this_start = today - timedelta(days=_STRENGTH_WINDOW_DAYS)
    prev_start = today - timedelta(days=_STRENGTH_WINDOW_DAYS * 2)
    this_sets = read_strength_sets(this_start, today, tz_name)
    prev_sets = read_strength_sets(prev_start, this_start, tz_name)
    muscle_deltas = gp.strength_volume_deltas(this_sets, prev_sets)

    eggs = read_egg_count(week_start, week_end, tz_name)
    fish = read_fish_count(week_start, week_end, tz_name)
    meals = read_meal_spend(week_start, week_end)
    # Uses one-week and two-week protein totals for their configured rotation windows.
    protein_1wk = read_protein_tally(week_start, week_end, tz_name)
    protein_2wk = read_protein_tally(week_start - timedelta(days=7), week_end, tz_name)

    log_event(logger, logging.INFO, "reflection_goal_inputs_read",
              has_quality_run=qrun is not None, this_sets=len(this_sets), prev_sets=len(prev_sets),
              eggs=eggs, fish=fish, meals_planned=meals["planned"], meals_eaten=meals["eaten"])
    return {"run": run, "muscle_deltas": muscle_deltas, "eggs": eggs, "fish_count": fish,
            "protein_1wk": protein_1wk, "protein_2wk": protein_2wk, "meals": meals}
