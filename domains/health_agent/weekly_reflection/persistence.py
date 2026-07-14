"""
DB I/O for the weekly reflection — calibration inputs, the 3-goal-review reads, and the
weekly_reflections upsert. The deterministic math lives in calibration.py (the shared §7 formula)
and weekly_reflection/goal_progress.py (goal math); this layer only runs SQL and feeds those.

NOT unit-tested in the agent env (no DB connection — house rule); exercised end-to-end via the
Sunday reflection endpoint that B runs. Local-day filters convert the timestamptz to B's tz
(resolved once via system/timezone.get_timezone upstream — never hardcoded).

Functions:
  read_calibration_inputs(today, tz_name, weeks_back) -> (weeks, now_avg7, last_maintenance)
  read_latest_quality_run() -> (distance_m, duration_s) | None
  read_strength_sets(start_date, end_date, tz_name) -> list[{exercise_name, weight_kg, reps}]
  read_egg_count(start_date, end_date, tz_name) -> int
  read_fish_count(start_date, end_date, tz_name) -> int
  read_goal_inputs(today, tz_name, week_start, week_end) -> dict
  upsert_weekly_reflection(iso_week, result, narrative, directives) -> None
"""

import logging
from datetime import date, timedelta

import psycopg2.extras

from domains.health_agent import calibration as cal
from domains.health_agent.meal_planner.persistence import read_protein_tally
from domains.health_agent.weekly_reflection import goal_progress as gp
from system.db import get_connection
from system.logging import log_event

logger = logging.getLogger(__name__)

_LB_TO_KG = 0.45359237
# 4 EQUAL weeks: compares "this 28d" vs "prior 28d" for the build-muscle deltas. Rolling (ends today)
# + equal-length so the volume % is honest (a calendar month would be 28-31d and bias the comparison).
_STRENGTH_WINDOW_DAYS = 28


# ---- calibration inputs + the weekly_reflections write -------------------------------------------

# Reads the calibration inputs for the trailing `weeks_back` ISO weeks ending at `today`.
# Pulls daily intake (SUM kcal/local day) from nutrition.food_log, daily weight (AVG/local day) from
# b.weight_measurements, period dates from b.period_days, and the last stored maintenance from
# health_agent.weekly_reflections; then delegates the bucketing to calibration.py.
# Inputs: today (local date), tz_name (IANA tz for the day boundary), weeks_back.
# Output: (weeks: list[WeekStat], now_avg7: float|None, last_maintenance: int|None).
def read_calibration_inputs(today: date, tz_name: str, weeks_back: int = 8):
    monday = today - timedelta(days=today.isoweekday() - 1)
    # extra 6 days so the oldest week's 7d rolling-avg has its lookback.
    window_start = monday - timedelta(weeks=weeks_back) - timedelta(days=6)
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT (created_at AT TIME ZONE %s)::date AS d, COALESCE(SUM(kcal), 0) "
                    "FROM nutrition.food_log "
                    "WHERE (created_at AT TIME ZONE %s)::date BETWEEN %s AND %s "
                    "GROUP BY d",
                    (tz_name, tz_name, window_start, today),
                )
                daily_intake = {d: float(k) for d, k in cur.fetchall()}

                cur.execute(
                    "SELECT (measured_at AT TIME ZONE %s)::date AS d, AVG(weight_kg) "
                    "FROM b.weight_measurements "
                    "WHERE (measured_at AT TIME ZONE %s)::date BETWEEN %s AND %s "
                    "GROUP BY d",
                    (tz_name, tz_name, window_start, today),
                )
                daily_weight = {d: float(w) for d, w in cur.fetchall()}

                cur.execute(
                    "SELECT period_date FROM b.period_days WHERE period_date >= %s",
                    (window_start,),
                )
                period_dates = {r[0] for r in cur.fetchall()}

                cur.execute(
                    "SELECT maintenance_kcal FROM health_agent.weekly_reflections "
                    "WHERE maintenance_kcal IS NOT NULL ORDER BY iso_week DESC LIMIT 1"
                )
                row = cur.fetchone()
                last_maintenance = int(row[0]) if row else None
    finally:
        conn.close()

    daily_avg7 = cal.rolling_avg7(daily_weight)
    weeks = cal.build_week_stats(daily_intake, daily_avg7, period_dates, today, weeks_back)
    now_avg7 = cal.current_avg7(daily_avg7, today)
    log_event(logger, logging.INFO, "calibration_inputs_read",
              weeks=len(weeks), intake_days=len(daily_intake), weight_days=len(daily_weight),
              period_days=len(period_dates), has_last_maintenance=last_maintenance is not None)
    return weeks, now_avg7, last_maintenance


# Upserts one health_agent.weekly_reflections row for the ISO week (idempotent on iso_week).
# Stores maintenance/target/trend + the prose narrative + carry-forward directives, with the
# calibration audit in meta. Inputs: iso_week, CalibrationResult, narrative text, directives dict.
def upsert_weekly_reflection(iso_week: str, result, narrative: str | None = None,
                             directives: dict | None = None) -> None:
    directives = directives or {}
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO health_agent.weekly_reflections "
                    "(iso_week, maintenance_kcal, target_kcal, weight_trend_kg, narrative, "
                    " directives, meta, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (iso_week) DO UPDATE SET "
                    "  maintenance_kcal = EXCLUDED.maintenance_kcal, "
                    "  target_kcal = EXCLUDED.target_kcal, "
                    "  weight_trend_kg = EXCLUDED.weight_trend_kg, "
                    # numbers always refresh; but if THIS run's LLM failed (narrative NULL) keep the
                    # prior narrative + directives rather than clobbering good prose with nulls.
                    "  narrative = COALESCE(EXCLUDED.narrative, health_agent.weekly_reflections.narrative), "
                    "  directives = CASE WHEN EXCLUDED.narrative IS NULL "
                    "               THEN health_agent.weekly_reflections.directives ELSE EXCLUDED.directives END, "
                    "  meta = EXCLUDED.meta, "
                    "  updated_at = now()",
                    (iso_week, result.maintenance_kcal, result.weekly_target_kcal,
                     result.weight_trend_kg, narrative,
                     psycopg2.extras.Json(directives), psycopg2.extras.Json(result.audit)),
                )
    finally:
        conn.close()
    log_event(logger, logging.INFO, "weekly_reflection_upserted", iso_week=iso_week)


# ---- 3-goal-review reads (spec H) ----------------------------------------------------------------

# Converts a stored weight to kg given its unit (None/'kg' -> as-is; 'lb'/'lbs' -> * 0.45359237).
def _to_kg(weight, unit):
    if weight is None:
        return None
    w = float(weight)
    if unit and unit.strip().lower().startswith("lb"):
        return round(w * _LB_TO_KG, 2)
    return w


# Most recent COMPLETED quality/fartlek run as (distance_m, duration_s), via the cardio_plan link
# (cardio_activities carries no run_type). Carried forward — no date filter. None if never done.
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


# Normalized strength sets in the LOCAL-date half-open window [start_date, end_date): effective
# weight (reported>recorded) in kg + effective reps. The UNIT is taken from the SAME source row as
# the weight (a CASE, not an independent COALESCE) so a unit-less correction can't mis-convert.
# The window filters on (started_at AT TIME ZONE tz)::date — consistent with the other local-day
# reads — so sessions bucket by B's day, not the DB session tz.
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


# Egg quantity logged in [start_date, end_date] local days. Sums food_meta.qty.amount ONLY when it
# is numeric (regex-guarded — a free-form value would otherwise abort the whole read) AND the unit is
# piece-like (a grams/ml qty is NOT a piece count, so those rows count as 1). Approximate by design
# (food_item ILIKE '%egg%'; protein_source is presence-only and holds no count).
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


# Plan-linked meal tally for the week [week_start, week_end] (inclusive, by plan_date — meal_plan is
# keyed to the day it's planned FOR, so no tz conversion). Returns {planned, eaten, spent_thb}:
#   planned = meal_plan rows for the week (any status), eaten = status 'ate',
#   spent_thb = Σ the eaten rows' items[].price_thb (the plan's snapshotted menu price — the plan-linked
#               actual cost of what B ate; staples/own-food carry no price_thb and drop out via the regex).
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


# Count of food_log entries whose protein_source array includes 'fish' in [start_date, end_date].
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


# Assembles the goal-review inputs for the reflection service: run estimate (latest quality run),
# build-muscle deltas (last 28d vs prior 28d), egg + fish tallies (this week). The service merges
# these with calibration (weight/maintenance/target) + the LLM narrative.
# week_start/week_end must be the Monday and the INCLUSIVE Sunday of the current week (egg/fish
# filter with BETWEEN — passing the next Monday as week_end would double-count that day).
def read_goal_inputs(today: date, tz_name: str, week_start: date, week_end: date) -> dict:
    qrun = read_latest_quality_run()
    run = gp.ten_k_goal_progress(*qrun) if qrun else None

    this_start = today - timedelta(days=_STRENGTH_WINDOW_DAYS)
    prev_start = today - timedelta(days=_STRENGTH_WINDOW_DAYS * 2)
    this_sets = read_strength_sets(this_start, today, tz_name)       # [today-28d, today)
    prev_sets = read_strength_sets(prev_start, this_start, tz_name)  # [today-56d, today-28d)
    muscle_deltas = gp.strength_volume_deltas(this_sets, prev_sets)

    eggs = read_egg_count(week_start, week_end, tz_name)
    fish = read_fish_count(week_start, week_end, tz_name)
    meals = read_meal_spend(week_start, week_end)
    # Protein-rotation tallies (lunch/dinner protein_source, like the meal planner): the Mon-Sun week
    # for beef/pork/fish, and a 2-WEEK window (prev Monday → this Sunday) for duck's "≥1 per 2wk".
    protein_1wk = read_protein_tally(week_start, week_end, tz_name)
    protein_2wk = read_protein_tally(week_start - timedelta(days=7), week_end, tz_name)

    log_event(logger, logging.INFO, "reflection_goal_inputs_read",
              has_quality_run=qrun is not None, this_sets=len(this_sets), prev_sets=len(prev_sets),
              eggs=eggs, fish=fish, meals_planned=meals["planned"], meals_eaten=meals["eaten"])
    return {"run": run, "muscle_deltas": muscle_deltas, "eggs": eggs, "fish_count": fish,
            "protein_1wk": protein_1wk, "protein_2wk": protein_2wk, "meals": meals}
