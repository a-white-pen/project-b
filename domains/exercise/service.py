"""
Exercise domain — persists Strava-sourced activities to the right exercise table.

Three destination tables, dispatched by sport_type:
  exercise.cardio_activities  — run/walk/ride/swim and treadmill variants. Per-km
                                splits go to exercise.cardio_splits.
  exercise.strength_sessions  — WeightTraining/Workout/Crossfit. Written by the
                                Garmin processor after this module hands off via
                                category="strength". Per-set detail in strength_sets.
  exercise.other_exercises    — everything else (yoga, pilates, climbing, plus any
                                unknown future Strava sport_type). No sub-table.

Public functions:
  save_strava_activity(strava_inbound_id, activity) — SINGLE DISPATCHER used by
      both the live webhook processor and the historical backfill. Classifies the
      sport_type, holds a per-activity advisory lock for the save+sweep window,
      writes to the right table, then sweeps sibling tables so the activity
      lives in exactly one family (handles Strava re-tag scenarios). Returns
      (saved, category). For "strength", returns (False, "strength") without
      writing OR holding the lock — the caller (Strava processor) handles
      strength orchestration with its own strava_activity_lock window.
  strava_activity_lock(strava_activity_id)        — context manager that
      acquires a session-scope pg_advisory_lock keyed on the strava_activity_id.
      Serializes concurrent processing of the same activity (e.g. two close-
      together Strava edit events). Public so the processor's strength branch
      can wrap its own orchestration in the same lock.
  save_cardio_activity(strava_inbound_id, activity)  — thin write path; assumes
      category is already cardio. Direct callers (tests/scripts) only.
  save_other_exercise(strava_inbound_id, activity)   — thin write path for
      other_exercises rows.
  delete_cardio_activity(strava_activity_id)         — deletes cardio row + splits (CASCADE)
  delete_other_exercise(strava_activity_id)          — deletes one other_exercises row

Internal helpers:
  _classify_activity(sport_type)              — maps Strava sport_type to routing category
  _other_activity_type(sport_type)            — maps Strava sport_type to activity_type stored on other_exercises
  ensure_single_exercise_family(id, keep)    — deletes sibling rows so each activity_id lives in one family
  _extract_timezone(tz_str)                   — extracts IANA timezone from Strava timezone string
  _build_splits(activity)                     — merges laps + splits_metric into per-km split rows

Routing summary (via save_strava_activity):
  WeightTraining/Workout/Crossfit  → "strength" → handed off to inbound.garmin.processor
  Run/Walk/Ride/Swim variants      → category   → exercise.cardio_activities
  Everything else                  → "other"    → exercise.other_exercises
"""

import json
import logging
import re
from contextlib import contextmanager

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# Strava sport_type → Project B activity routing category.
# Types not listed here fall back to "other" so nothing is silently dropped —
# the Strava processor routes "other" rows to exercise.other_exercises.
_RUN_TYPES = {"Run", "TrailRun", "VirtualRun", "Treadmill"}
_WALK_TYPES = {"Walk", "Hike"}
_RIDE_TYPES = {"Ride", "VirtualRide", "EBikeRide", "MountainBikeRide", "GravelRide", "Velomobile"}
_SWIM_TYPES = {"Swim", "OpenWaterSwim"}
# Routed to Garmin processor — Garmin exercise_sets presence determines if they
# end up in strength tables. No cardio row is written for these.
_STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit"}


# Classifies a Strava sport_type into a Project B routing category. Drives which
# downstream save function the Strava processor calls — does NOT directly become
# the activity_category column on any row (cardio rows carry the specific
# category like "run"; other_exercises rows carry an activity_type derived from
# sport_type in save_other_exercise).
# Inputs: Strava sport_type string.
# Outputs: one of: "run", "walk", "ride", "swim", "strength", "other".
def _classify_activity(sport_type: str) -> str:
    if sport_type in _STRENGTH_TYPES:
        return "strength"
    if sport_type in _RUN_TYPES:
        return "run"
    if sport_type in _WALK_TYPES:
        return "walk"
    if sport_type in _RIDE_TYPES:
        return "ride"
    if sport_type in _SWIM_TYPES:
        return "swim"
    # Intentional catch-all — every unrecognised sport_type routes to
    # exercise.other_exercises, including cardio-ish machine types like Rowing,
    # Elliptical, StandUpPaddling, and Skating. Design decision (2026-05-26):
    # the conceptual split is "things with meaningful distance/pace" vs "things
    # with meaningful duration/HR". Machine cardio without B caring about pace
    # belongs in the second bucket. If a specific cardio-ish type later proves
    # important enough to warrant distance + pace handling, promote it into one
    # of the explicit type sets above.
    return "other"


# Coerces Strava's float calories to our integer column. Returns None ONLY when
# the source omitted the field — explicit 0.0 (legitimate for very short
# activities) is preserved as int 0, not silently dropped. Used by both
# save_cardio_activity and save_other_exercise.
def _coerce_calories(value) -> int | None:
    if value is None:
        return None
    return int(value)


# Extracts the IANA timezone string from Strava's "(GMT+07:00) Asia/Bangkok" format.
def _extract_timezone(tz_str: str) -> str:
    if tz_str and ") " in tz_str:
        return tz_str.split(") ", 1)[1]
    if tz_str:
        log_event(logger, logging.WARNING, "exercise_timezone_format_unexpected", tz_str_len=len(tz_str))
    return tz_str or "UTC"


# Merges Strava laps (has cadence, max HR, elevation gain) with splits_metric
# (has moving_time, elevation_difference, grade_adjusted_speed) by split index.
# Assumption: Strava lap_index and splits_metric split key are 1-based and aligned — same position
# in each array maps to the same km split. If Strava ever changes this, splits will silently mismatch.
# Laps missing lap_index, distance, or elapsed_time are skipped — those columns are NOT NULL in the schema.
# Returns a list of dicts ready for bulk insert into exercise.cardio_splits.
def _build_splits(activity: dict) -> list[dict]:
    # Use .get("split") to avoid KeyError if Strava omits the split key on any entry.
    splits_by_idx = {s["split"]: s for s in activity.get("splits_metric", []) if s.get("split") is not None}
    rows = []
    for lap in activity.get("laps", []):
        idx = lap.get("lap_index")
        distance = lap.get("distance")
        elapsed = lap.get("elapsed_time")
        # lap_index, distance_m, elapsed_seconds are NOT NULL in the schema — skip incomplete laps.
        if idx is None or distance is None or elapsed is None:
            log_event(logger, logging.WARNING, "exercise_split_incomplete_lap_skipped",
                      lap_index=idx, has_distance=distance is not None, has_elapsed=elapsed is not None)
            continue
        sm = splits_by_idx.get(idx, {})
        rows.append({
            "lap_index": idx,
            "distance_m": distance,
            "elapsed_seconds": elapsed,
            "moving_seconds": sm.get("moving_time") or lap.get("moving_time"),
            "average_speed_mps": lap.get("average_speed"),
            "max_speed_mps": lap.get("max_speed"),
            "average_cadence": lap.get("average_cadence"),
            "average_heartrate": lap.get("average_heartrate"),
            "max_heartrate": lap.get("max_heartrate"),
            "elevation_gain_m": lap.get("total_elevation_gain"),
            "elevation_difference_m": sm.get("elevation_difference"),
            "grade_adjusted_speed_mps": sm.get("average_grade_adjusted_speed"),
            "pace_zone": lap.get("pace_zone") or (sm.get("pace_zone") if sm.get("pace_zone") else None),
        })
    return rows


# Upserts one cardio activity row and replaces its splits.
# Assumes the activity has ALREADY been classified as cardio by the caller —
# see save_strava_activity for the full dispatch path. Direct callers (tests,
# scripts) must pass an activity whose sport_type maps to run/walk/ride/swim.
# Returns (saved, activity_category): activity_category is the specific cardio
# category written (run/walk/ride/swim), or None on DB failure.
# Inputs: strava_inbound_id from system.strava_inbound, full Strava activity detail dict.
def save_cardio_activity(strava_inbound_id: int, activity: dict) -> tuple[bool, str | None]:
    sport_type = activity.get("sport_type", "")
    category = _classify_activity(sport_type)
    if category not in ("run", "walk", "ride", "swim"):
        # Defensive guard for direct callers; the dispatcher (save_strava_activity)
        # routes non-cardio types to their correct table before this is called.
        log_event(logger, logging.WARNING, "exercise_cardio_save_skipped_non_cardio",
                  sport_type=sport_type, classified_as=category,
                  strava_activity_id=activity.get("id"))
        return False, category

    strava_activity_id = activity["id"]
    start_latlng = activity.get("start_latlng") or []
    gear = activity.get("gear") or {}
    map_data = activity.get("map") or {}
    polyline = map_data.get("polyline") or map_data.get("summary_polyline") or None

    meta = {
        "strava_workout_type": activity.get("workout_type"),
        "external_id": activity.get("external_id"),
    }

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO exercise.cardio_activities (
                        strava_inbound_id, strava_activity_id, activity_name,
                        sport_type, activity_category, is_treadmill,
                        started_at, timezone,
                        duration_seconds, moving_seconds,
                        distance_m, elevation_gain_m, elev_high_m, elev_low_m,
                        average_speed_mps, max_speed_mps, average_cadence,
                        average_heartrate, max_heartrate, calories_kcal,
                        perceived_exertion, gear_name, device_name,
                        polyline, start_lat, start_lng, meta
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (strava_activity_id) DO UPDATE SET
                        strava_inbound_id    = EXCLUDED.strava_inbound_id,
                        activity_name        = EXCLUDED.activity_name,
                        sport_type           = EXCLUDED.sport_type,
                        activity_category    = EXCLUDED.activity_category,
                        is_treadmill         = EXCLUDED.is_treadmill,
                        started_at           = EXCLUDED.started_at,
                        timezone             = EXCLUDED.timezone,
                        duration_seconds     = EXCLUDED.duration_seconds,
                        moving_seconds       = EXCLUDED.moving_seconds,
                        distance_m           = EXCLUDED.distance_m,
                        elevation_gain_m     = EXCLUDED.elevation_gain_m,
                        elev_high_m          = EXCLUDED.elev_high_m,
                        elev_low_m           = EXCLUDED.elev_low_m,
                        average_speed_mps    = EXCLUDED.average_speed_mps,
                        max_speed_mps        = EXCLUDED.max_speed_mps,
                        average_cadence      = EXCLUDED.average_cadence,
                        average_heartrate    = EXCLUDED.average_heartrate,
                        max_heartrate        = EXCLUDED.max_heartrate,
                        calories_kcal        = EXCLUDED.calories_kcal,
                        perceived_exertion   = EXCLUDED.perceived_exertion,
                        gear_name            = EXCLUDED.gear_name,
                        device_name          = EXCLUDED.device_name,
                        polyline             = EXCLUDED.polyline,
                        start_lat            = EXCLUDED.start_lat,
                        start_lng            = EXCLUDED.start_lng,
                        meta                 = EXCLUDED.meta,
                        updated_at           = now()
                    RETURNING cardio_activity_id
                    """,
                    (
                        strava_inbound_id, strava_activity_id,
                        activity.get("name") or "Activity",
                        sport_type, category, bool(activity.get("trainer")),
                        activity.get("start_date"),
                        _extract_timezone(activity.get("timezone", "")),
                        activity.get("elapsed_time"), activity.get("moving_time"),
                        activity.get("distance") or None,
                        activity.get("total_elevation_gain") or None,
                        activity.get("elev_high") or None,
                        activity.get("elev_low") or None,
                        activity.get("average_speed") or None,
                        activity.get("max_speed") or None,
                        activity.get("average_cadence") or None,
                        activity.get("average_heartrate") or None,
                        activity.get("max_heartrate") or None,
                        _coerce_calories(activity.get("calories")),
                        activity.get("perceived_exertion"),
                        gear.get("name") or None,
                        activity.get("device_name") or None,
                        polyline or None,
                        start_latlng[0] if len(start_latlng) >= 2 else None,
                        start_latlng[1] if len(start_latlng) >= 2 else None,
                        json.dumps(meta),
                    ),
                )
                row = cur.fetchone()
                cardio_activity_id = row[0]

                # Replace splits — delete old ones then bulk insert current.
                cur.execute(
                    "DELETE FROM exercise.cardio_splits WHERE cardio_activity_id = %s",
                    (cardio_activity_id,),
                )
                splits = _build_splits(activity)
                if splits:
                    cur.executemany(
                        """
                        INSERT INTO exercise.cardio_splits (
                            cardio_activity_id, lap_index, distance_m,
                            elapsed_seconds, moving_seconds,
                            average_speed_mps, max_speed_mps, average_cadence,
                            average_heartrate, max_heartrate,
                            elevation_gain_m, elevation_difference_m,
                            grade_adjusted_speed_mps, pace_zone
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        [
                            (
                                cardio_activity_id, s["lap_index"], s["distance_m"],
                                s["elapsed_seconds"], s["moving_seconds"],
                                s["average_speed_mps"], s["max_speed_mps"], s["average_cadence"],
                                s["average_heartrate"], s["max_heartrate"],
                                s["elevation_gain_m"], s["elevation_difference_m"],
                                s["grade_adjusted_speed_mps"], s["pace_zone"],
                            )
                            for s in splits
                        ],
                    )

        log_event(logger, logging.INFO, "exercise_cardio_saved",
                  cardio_activity_id=cardio_activity_id,
                  strava_activity_id=strava_activity_id,
                  activity_category=category,
                  sport_type=sport_type,
                  splits_count=len(splits))
        return True, category

    except Exception as e:
        log_failure(logger, logging.ERROR, "exercise_cardio_save_failed", e,
                    strava_activity_id=strava_activity_id,
                    strava_inbound_id=strava_inbound_id)
        return False, None
    finally:
        conn.close()


# Deletes one cardio activity row and its splits (via CASCADE) by Strava activity ID.
# Called when Strava sends a delete event for an activity B has removed in the Strava app.
# Inputs: strava_activity_id from the Strava webhook event.
# Outputs: True if a row was deleted, False if no matching row existed. Raises on DB error.
def delete_cardio_activity(strava_activity_id: int) -> bool:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM exercise.cardio_activities WHERE strava_activity_id = %s",
                    (strava_activity_id,),
                )
                deleted = cur.rowcount > 0
        log_event(logger, logging.INFO, "exercise_cardio_deleted",
                  strava_activity_id=strava_activity_id, deleted=deleted)
        return deleted
    finally:
        conn.close()


# Maps Strava sport_type → activity_type stored on exercise.other_exercises rows.
# Lower-snake-case values for consistent agent/analytics filtering. Unknown
# sport_types fall through to a snake_case slug of the Strava string itself so
# new types (Tai Chi, Boxing, etc.) survive without code changes.
_OTHER_ACTIVITY_TYPE_MAP = {
    "Yoga": "yoga",
    "Pilates": "pilates",
    "RockClimbing": "climbing",
}


# Converts a Strava sport_type into our normalised activity_type for
# other_exercises. Falls back to a snake_case slug so future Strava additions
# land in a queryable form without needing code updates.
# Examples: "Yoga" → "yoga"; "RockClimbing" → "climbing" (mapped); "TaiChi" → "tai_chi" (slug); "" → "other".
def _other_activity_type(sport_type: str) -> str:
    if sport_type in _OTHER_ACTIVITY_TYPE_MAP:
        return _OTHER_ACTIVITY_TYPE_MAP[sport_type]
    if not sport_type:
        return "other"
    # CamelCase → snake_case → lowercase.
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "_", sport_type).lower()


# Upserts one row into exercise.other_exercises. Used by the Strava processor
# for activities that classify as "other" — yoga, pilates, climbing, and any
# unknown sport_type not explicitly mapped to cardio or strength.
# Inputs: strava_inbound_id from system.strava_inbound, full Strava activity detail dict.
# Outputs: True if a row was saved (insert or update), False on DB error.
def save_other_exercise(strava_inbound_id: int, activity: dict) -> bool:
    strava_activity_id = activity.get("id")
    if strava_activity_id is None:
        log_event(logger, logging.WARNING, "exercise_other_missing_activity_id",
                  strava_inbound_id=strava_inbound_id)
        return False

    sport_type = activity.get("sport_type", "")
    activity_type = _other_activity_type(sport_type)

    # Curate a payload of Strava extras into meta. system.strava_inbound only
    # stores the webhook event, not the fetched activity detail — anything we
    # drop here is gone for good. These fields don't earn dedicated columns
    # (the table is shape-stable across activity_types, where most of these
    # would be null) but they live in meta for ad-hoc / agent access via
    # meta ->> 'distance_m'. The Strava polyline blob (1-50KB) is intentionally
    # skipped; if a specific other-type ever needs route geometry, promote it
    # to a dedicated column then.
    gear = activity.get("gear") or {}
    start_latlng = activity.get("start_latlng") or []
    extras = {
        "strava_sport_type": sport_type,
        "strava_workout_type": activity.get("workout_type"),
        "external_id": activity.get("external_id"),
        # Movement / distance fields — present for cardio-ish "other" types
        # (Elliptical, Rowing, Skating, SUP) and absent for true non-cardio
        # (Yoga, Pilates, Climbing). None values are dropped below so the meta
        # payload stays compact.
        "distance_m": activity.get("distance"),
        "moving_seconds": activity.get("moving_time"),
        "elevation_gain_m": activity.get("total_elevation_gain"),
        "elev_high_m": activity.get("elev_high"),
        "elev_low_m": activity.get("elev_low"),
        "average_speed_mps": activity.get("average_speed"),
        "max_speed_mps": activity.get("max_speed"),
        "average_cadence": activity.get("average_cadence"),
        "is_treadmill": activity.get("trainer") if "trainer" in activity else None,
        "gear_name": gear.get("name"),
        "start_lat": start_latlng[0] if len(start_latlng) >= 2 else None,
        "start_lng": start_latlng[1] if len(start_latlng) >= 2 else None,
    }
    meta = {k: v for k, v in extras.items() if v is not None}

    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO exercise.other_exercises (
                        strava_inbound_id, strava_activity_id,
                        source_app, inbound_row_id, source_activity_id,
                        activity_type, activity_name,
                        started_at, timezone,
                        duration_seconds,
                        avg_hr, max_hr, calories_kcal,
                        perceived_exertion, device_name, meta
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (strava_activity_id) WHERE strava_activity_id IS NOT NULL
                    DO UPDATE SET
                        strava_inbound_id  = EXCLUDED.strava_inbound_id,
                        source_app         = EXCLUDED.source_app,
                        inbound_row_id     = EXCLUDED.inbound_row_id,
                        source_activity_id = EXCLUDED.source_activity_id,
                        activity_type      = EXCLUDED.activity_type,
                        activity_name      = EXCLUDED.activity_name,
                        started_at         = EXCLUDED.started_at,
                        timezone           = EXCLUDED.timezone,
                        duration_seconds   = EXCLUDED.duration_seconds,
                        avg_hr             = EXCLUDED.avg_hr,
                        max_hr             = EXCLUDED.max_hr,
                        calories_kcal      = EXCLUDED.calories_kcal,
                        perceived_exertion = EXCLUDED.perceived_exertion,
                        device_name        = EXCLUDED.device_name,
                        meta               = EXCLUDED.meta,
                        updated_at         = now()
                    RETURNING other_exercise_id
                    """,
                    (
                        strava_inbound_id,
                        strava_activity_id,
                        "strava",
                        strava_inbound_id,    # inbound_row_id — same as strava_inbound_id for source_app='strava'
                        str(strava_activity_id),
                        activity_type,
                        activity.get("name") or "Activity",
                        activity.get("start_date"),
                        _extract_timezone(activity.get("timezone", "")),
                        activity.get("elapsed_time") or activity.get("moving_time"),
                        activity.get("average_heartrate") or None,
                        activity.get("max_heartrate") or None,
                        _coerce_calories(activity.get("calories")),
                        activity.get("perceived_exertion"),
                        activity.get("device_name") or None,
                        json.dumps(meta),
                    ),
                )
                row = cur.fetchone()
                other_exercise_id = row[0] if row else None

        log_event(logger, logging.INFO, "exercise_other_saved",
                  other_exercise_id=other_exercise_id,
                  strava_activity_id=strava_activity_id,
                  activity_type=activity_type,
                  sport_type=sport_type)
        return True

    except Exception as e:
        log_failure(logger, logging.ERROR, "exercise_other_save_failed", e,
                    strava_activity_id=strava_activity_id,
                    strava_inbound_id=strava_inbound_id)
        return False
    finally:
        if conn is not None:
            conn.close()


# Deletes one other_exercises row by Strava activity ID. Called from the Strava
# delete dispatcher alongside delete_cardio_activity / delete_strength_session.
# Inputs: strava_activity_id from the Strava webhook event.
# Outputs: True if a row was deleted, False if no matching row existed. Raises on DB error.
def delete_other_exercise(strava_activity_id: int) -> bool:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM exercise.other_exercises WHERE strava_activity_id = %s",
                    (strava_activity_id,),
                )
                deleted = cur.rowcount > 0
        log_event(logger, logging.INFO, "exercise_other_deleted",
                  strava_activity_id=strava_activity_id, deleted=deleted)
        return deleted
    finally:
        conn.close()


# Ensures a Strava activity ID lives in EXACTLY ONE exercise family table by
# deleting any sibling rows. Called by save_strava_activity AFTER a successful
# save (and by the strength path in inbound/strava/processor.py after a
# successful Garmin save) so that a re-tag in Strava — Run → Yoga, Yoga →
# WeightTraining, etc. — moves the row cleanly rather than leaving the activity
# in two tables.
#
# Inputs: strava_activity_id, keep — the category the activity now lives under.
# One of: "run"/"walk"/"ride"/"swim" (cardio family), "strength", "other".
# Outputs: list of sibling table names where a row was deleted, for logging.
#
# Per-table failures are caught and logged but NEVER re-raised. The post-save
# sweep is a cleanup step: the user-visible reply / notification must not be
# suppressed by a transient sibling-delete DB hiccup. Worst case on partial
# failure is a recoverable duplicate (activity in two tables) — the next
# webhook for the same activity, or the next backfill run, will clean up.
#
# Idempotent — no-op when sibling tables don't have the row. Strength import
# is local to avoid a top-level circular import between service.py and
# strength_service.py.
_CARDIO_CATEGORIES = {"run", "walk", "ride", "swim"}


def ensure_single_exercise_family(strava_activity_id: int, keep: str) -> list[str]:
    from domains.exercise.strength_service import delete_strength_session

    swept: list[str] = []
    failures: list[str] = []

    # Defensive per-table try/except: a single failing delete must not stop us
    # from sweeping the other siblings, AND must not surface as an exception
    # to the user-visible reply path.
    def _safe_delete(table_label: str, fn) -> None:
        try:
            if fn(strava_activity_id):
                swept.append(table_label)
        except Exception as e:
            failures.append(table_label)
            log_failure(
                logger,
                logging.WARNING,
                "exercise_sibling_sweep_table_failed",
                e,
                strava_activity_id=strava_activity_id,
                kept_category=keep,
                failed_table=table_label,
            )

    if keep not in _CARDIO_CATEGORIES:
        _safe_delete("cardio_activities", delete_cardio_activity)
    if keep != "strength":
        _safe_delete("strength_sessions", delete_strength_session)
    if keep != "other":
        _safe_delete("other_exercises", delete_other_exercise)

    if swept or failures:
        log_event(
            logger,
            logging.INFO,
            "exercise_sibling_tables_swept",
            strava_activity_id=strava_activity_id,
            kept_category=keep,
            swept_tables=swept,
            failed_tables=failures,
        )
    return swept


# Per-activity session-scope advisory lock around save+sweep. Two webhook events
# for the SAME strava_activity_id (e.g. B edits Run → Yoga → Run quickly) can be
# processed concurrently as FastAPI background tasks; without serialization both
# can save then both can sweep, deleting each other's just-saved rows and
# leaving NO row in any table. The lock pins all processing of a given
# strava_activity_id to one in-flight handler at a time. Different activities
# still process in parallel.
#
# Key choice: raw strava_activity_id (bigint). Strava IDs are 10+ digit
# positive integers, well clear of the int4-range hashtext keys used by
# _lock_attention_writes / _lock_sleep_wake_writes in the bigint lock space.
#
# Lock is held by a dedicated short-lived connection in autocommit mode so it
# survives the multiple sub-transactions opened by save_*/delete_* helpers.
# Released explicitly in the finally; conn.close() releases on any error path.
@contextmanager
def strava_activity_lock(strava_activity_id):
    if strava_activity_id is None:
        # Malformed payload — no lock keyed available. Best we can do is
        # proceed; the missing-id guard in save_other_exercise will reject.
        yield
        return
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (int(strava_activity_id),))
        yield
    finally:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (int(strava_activity_id),))
        except Exception as e:
            log_failure(
                logger,
                logging.WARNING,
                "exercise_activity_lock_release_failed",
                e,
                strava_activity_id=strava_activity_id,
            )
        conn.close()


# Single dispatcher entry point for routing one Strava activity to the right
# exercise table. Used by both the webhook processor (inbound/strava/processor.py)
# and the historical backfill (inbound/strava/backfill.py) so live and replay
# behaviour stay in sync.
#
# Behaviour:
#   1. Classifies the Strava sport_type → category.
#   2. Writes to the right table (cardio or other). On successful write, sweeps
#      sibling tables so the strava_activity_id lives in at most one family
#      (handles Strava re-tag scenarios like Run → Yoga).
#   3. For "strength", returns without writing OR sweeping — Garmin fetch lives
#      in a separate module and the sibling-sweep decision is the caller's
#      (must distinguish a genuine re-tag from a benign update of an existing
#      strength activity; only the caller has the aspect_type context).
#
# Inputs: strava_inbound_id, full Strava activity detail dict.
# Outputs: (saved, category) where:
#   (True,  "run"/"walk"/"ride"/"swim")  — cardio row written
#   (True,  "other")                     — other_exercises row written
#   (False, "strength")                  — caller must orchestrate the strength
#                                          path (sibling sweep + Garmin fetch);
#                                          see processor.py for the rules
#   (False, category)                    — write failure (errors already logged)
#
# Sweep ordering — IMPORTANT:
# Sweep runs AFTER a successful save, not before. Rationale: if we sweep first
# and the save fails (e.g. transient DB error), the old row is permanently
# deleted with no replacement. Sweep-after means save failures leave the
# pre-existing row in place. The trade-off: while the save commits and the
# sweep runs, an activity can briefly appear in two tables. The sweep is a
# single-row delete and almost always succeeds; if it fails (rare), we have a
# recoverable duplicate, never lost data. The previous design's claim that
# Strava webhook retries would cover sweep+save failures was WRONG — the
# webhook handler returns 200 OK before this code runs (background task in
# inbound/strava/webhook.py), so Strava sees success and never retries.
def save_strava_activity(strava_inbound_id: int, activity: dict) -> tuple[bool, str]:
    sport_type = activity.get("sport_type", "")
    category = _classify_activity(sport_type)
    strava_activity_id = activity.get("id")

    # Strength dispatch needs no DB write here — return early outside the lock.
    # The processor handles strength orchestration (including its own
    # ensure_single_exercise_family call); the strength branch in
    # process_activity_event holds an equivalent lock-friendly window via the
    # synchronous fetch + post-fetch existence check.
    if category == "strength":
        log_event(logger, logging.INFO, "exercise_strength_routed_to_caller",
                  sport_type=sport_type, strava_activity_id=strava_activity_id)
        return False, "strength"

    # Serialize concurrent processing of the same activity_id. Different
    # activity_ids continue to run in parallel.
    with strava_activity_lock(strava_activity_id):
        if category == "other":
            saved = save_other_exercise(strava_inbound_id, activity)
            if saved and strava_activity_id is not None:
                # Post-save sweep: clear any stale cardio/strength row left over
                # from a re-tag (Run → Yoga etc.). Idempotent — no-op when
                # siblings are empty. Failure here leaves a recoverable
                # duplicate, not data loss; logged inside the helper.
                ensure_single_exercise_family(strava_activity_id, keep="other")
            return saved, "other"

        # Cardio path — category is run/walk/ride/swim.
        saved, returned_category = save_cardio_activity(strava_inbound_id, activity)
        final_category = returned_category or category
        if saved and strava_activity_id is not None:
            ensure_single_exercise_family(strava_activity_id, keep=final_category)
        return saved, final_category
