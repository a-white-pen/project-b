"""
Exercise domain — saves completed activities to the right exercise table.

Three destination tables, chosen by classify_activity(sport_type):
  exercise.cardio_activities  — run/walk/ride/swim and treadmill variants. Per-lap
                                splits go to exercise.cardio_splits.
  exercise.strength_sessions  — WeightTraining/Workout/Crossfit. Written by
                                domains.exercise.strength_service from the exercise-set
                                payload, not from here.
  exercise.other_exercises    — everything else (yoga, pilates, climbing, plus any
                                sport_type not mapped above). No sub-table.

Activities arrive as one normalized dict built by the inbound source (today
inbound.garmin.processor). Keys match the column names below, plus "name" for
activity_name and "splits" for the cardio laps. Rows are identified by source_app +
source_activity_id, which is unique per table, so saving the same activity twice
never creates a second row.

Public functions:
  classify_activity(sport_type)          — maps a sport_type to run/walk/ride/swim/strength/other
  save_cardio_activity(activity)         — inserts one cardio row and its splits; True if the row is new
  save_other_exercise(activity)          — inserts one other_exercises row; True if the row is new
  get_recorded_activities(source_app, source_activity_ids) — {source_activity_id: activity_name}
      for the ids already saved in any of the three tables
  find_activity_from_other_source(source_app, started_at) — True if another source already
      recorded a cardio or other session starting within two minutes of started_at
  update_activity_names(source_app, names) — applies renames made at the source to rows that
      source created
  get_presentations(source_app, source_activity_ids) — {source_activity_id: meta.presentation}
      for rows that source created, leaving out rows that show the Strava export's
  save_presentation(source_app, source_activity_id, presentation) — sets meta.presentation on
      the row that source created, unless it shows the Strava export's
  get_recent_activity_ids(source_app, since, limit) — source ids of the newest rows that source
      created, started at or after since (any time when None)
  delete_activities(source_app, source_activity_ids) — deletes the rows that source created,
      with their splits and sets

Internal helpers:
  _other_activity_type(sport_type) — maps sport_type to the activity_type stored on other_exercises
  _coerce_calories(value)          — float kcal to int, keeping an explicit 0
"""

import json
import logging
import re
from datetime import timedelta

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# sport_type → routing category. Types not listed here fall back to "other" so
# nothing is silently dropped.
_RUN_TYPES = {"Run", "TrailRun", "VirtualRun", "Treadmill"}
_WALK_TYPES = {"Walk", "Hike"}
_RIDE_TYPES = {"Ride", "VirtualRide", "EBikeRide", "MountainBikeRide", "GravelRide", "Velomobile"}
_SWIM_TYPES = {"Swim", "OpenWaterSwim"}
# Strength only when the session carries exercise sets; the inbound processor
# decides that, since only it has the set payload.
_STRENGTH_TYPES = {"WeightTraining", "Workout", "Crossfit"}

CARDIO_CATEGORIES = {"run", "walk", "ride", "swim"}

# Two recordings of the same workout start at the same second; the window absorbs
# clock rounding between sources.
_SAME_SESSION_WINDOW = timedelta(seconds=120)


# Classifies a sport_type into a routing category. Drives which save function the
# inbound processor calls — does NOT directly become the activity_category column
# on any row (cardio rows carry the specific category like "run"; other_exercises
# rows carry an activity_type derived from sport_type in save_other_exercise).
# Inputs: sport_type string.
# Outputs: one of: "run", "walk", "ride", "swim", "strength", "other".
def classify_activity(sport_type: str) -> str:
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
    # Elliptical, StandUpPaddling, and Skating. The split is "things with meaningful
    # distance/pace" vs "things with meaningful duration/HR". If a cardio-ish type
    # later needs distance + pace handling, promote it into one of the sets above.
    return "other"


# Coerces float calories to our integer column. Returns None ONLY when the source
# omitted the field — explicit 0.0 (legitimate for very short activities) is kept.
def _coerce_calories(value) -> int | None:
    if value is None:
        return None
    return int(value)


# Inserts one cardio activity row and its splits in one transaction. Assumes the
# caller has already classified the activity as run/walk/ride/swim.
# Inputs: normalized activity dict (see module docstring) with "splits" as a list of
#         exercise.cardio_splits rows keyed by column name.
# Outputs: True if a new row was written; False if it already existed or the write
#          failed (failures are logged; the caller retries on its next run).
def save_cardio_activity(activity: dict) -> bool:
    category = classify_activity(activity["sport_type"])
    splits = activity.get("splits") or []
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO exercise.cardio_activities (
                        source_app, inbound_row_id, source_activity_id,
                        activity_name, sport_type, activity_category, is_treadmill,
                        started_at, timezone,
                        duration_seconds, moving_seconds,
                        distance_m, elevation_gain_m, elev_high_m, elev_low_m,
                        average_speed_mps, max_speed_mps, average_cadence,
                        average_heartrate, max_heartrate, calories_kcal,
                        perceived_exertion, gear_name, device_name,
                        polyline, start_lat, start_lng, meta
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (source_app, source_activity_id)
                        WHERE source_activity_id IS NOT NULL
                    DO NOTHING
                    RETURNING cardio_activity_id
                    """,
                    (
                        activity["source_app"], activity["inbound_row_id"], activity["source_activity_id"],
                        activity["name"], activity["sport_type"], category, activity["is_treadmill"],
                        activity["started_at"], activity["timezone"],
                        activity["duration_seconds"], activity["moving_seconds"],
                        activity.get("distance_m") or None,
                        activity.get("elevation_gain_m") or None,
                        activity.get("elev_high_m"),
                        activity.get("elev_low_m"),
                        activity.get("average_speed_mps") or None,
                        activity.get("max_speed_mps") or None,
                        activity.get("average_cadence") or None,
                        activity.get("average_heartrate") or None,
                        activity.get("max_heartrate") or None,
                        _coerce_calories(activity.get("calories_kcal")),
                        activity.get("perceived_exertion"),
                        activity.get("gear_name"),
                        activity.get("device_name"),
                        activity.get("polyline"),
                        activity.get("start_lat"),
                        activity.get("start_lng"),
                        json.dumps(activity.get("meta") or {}),
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    log_event(logger, logging.INFO, "exercise_cardio_already_saved",
                              source_app=activity["source_app"],
                              source_activity_id=activity["source_activity_id"])
                    return False
                cardio_activity_id = row[0]

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
                  source_app=activity["source_app"],
                  source_activity_id=activity["source_activity_id"],
                  activity_category=category,
                  sport_type=activity["sport_type"],
                  splits_count=len(splits))
        return True

    except Exception as e:
        log_failure(logger, logging.ERROR, "exercise_cardio_save_failed", e,
                    source_app=activity.get("source_app"),
                    source_activity_id=activity.get("source_activity_id"))
        return False
    finally:
        if conn is not None:
            conn.close()


# sport_type → activity_type stored on exercise.other_exercises rows. Lower-snake-case
# values for consistent agent/analytics filtering. Unknown sport_types fall through
# to a snake_case slug so new types (Tai Chi, Boxing, etc.) survive without code changes.
_OTHER_ACTIVITY_TYPE_MAP = {
    "Yoga": "yoga",
    "Pilates": "pilates",
    "RockClimbing": "climbing",
}


# Converts a sport_type into our normalised activity_type for other_exercises.
# Examples: "Yoga" → "yoga"; "RockClimbing" → "climbing" (mapped); "TaiChi" → "tai_chi" (slug); "" → "other".
def _other_activity_type(sport_type: str) -> str:
    if sport_type in _OTHER_ACTIVITY_TYPE_MAP:
        return _OTHER_ACTIVITY_TYPE_MAP[sport_type]
    if not sport_type:
        return "other"
    # CamelCase → snake_case → lowercase.
    return re.sub(r"(?<=[a-z])(?=[A-Z])", "_", sport_type).lower()


# Inserts one row into exercise.other_exercises — yoga, pilates, climbing, and any
# sport_type not classified as cardio or strength.
# Inputs: normalized activity dict (see module docstring).
# Outputs: True if a new row was written; False if it already existed or the write
#          failed (failures are logged; the caller retries on its next run).
def save_other_exercise(activity: dict) -> bool:
    sport_type = activity.get("sport_type", "")
    activity_type = _other_activity_type(sport_type)

    # Movement fields have no column on this table (most activity types leave them
    # empty), so the ones present go into meta for ad-hoc and agent queries.
    extras = {
        "sport_type": sport_type,
        "distance_m": activity.get("distance_m"),
        "moving_seconds": activity.get("moving_seconds"),
        "elevation_gain_m": activity.get("elevation_gain_m"),
        "elev_high_m": activity.get("elev_high_m"),
        "elev_low_m": activity.get("elev_low_m"),
        "average_speed_mps": activity.get("average_speed_mps"),
        "max_speed_mps": activity.get("max_speed_mps"),
        "average_cadence": activity.get("average_cadence"),
        "is_treadmill": activity.get("is_treadmill") or None,
        "gear_name": activity.get("gear_name"),
        "start_lat": activity.get("start_lat"),
        "start_lng": activity.get("start_lng"),
    }
    meta = {**(activity.get("meta") or {}), **{k: v for k, v in extras.items() if v is not None}}

    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO exercise.other_exercises (
                        source_app, inbound_row_id, source_activity_id,
                        activity_type, activity_name,
                        started_at, timezone,
                        duration_seconds,
                        avg_hr, max_hr, calories_kcal,
                        perceived_exertion, device_name, meta
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s
                    )
                    ON CONFLICT (source_app, source_activity_id)
                        WHERE source_activity_id IS NOT NULL
                    DO NOTHING
                    RETURNING other_exercise_id
                    """,
                    (
                        activity["source_app"],
                        activity["inbound_row_id"],
                        activity["source_activity_id"],
                        activity_type,
                        activity.get("name") or "Activity",
                        activity["started_at"],
                        activity.get("timezone"),
                        activity.get("duration_seconds") or activity.get("moving_seconds"),
                        activity.get("average_heartrate") or None,
                        activity.get("max_heartrate") or None,
                        _coerce_calories(activity.get("calories_kcal")),
                        activity.get("perceived_exertion"),
                        activity.get("device_name"),
                        json.dumps(meta),
                    ),
                )
                row = cur.fetchone()

        if row is None:
            log_event(logger, logging.INFO, "exercise_other_already_saved",
                      source_app=activity["source_app"],
                      source_activity_id=activity["source_activity_id"])
            return False

        log_event(logger, logging.INFO, "exercise_other_saved",
                  other_exercise_id=row[0],
                  source_app=activity["source_app"],
                  source_activity_id=activity["source_activity_id"],
                  activity_type=activity_type,
                  sport_type=sport_type)
        return True

    except Exception as e:
        log_failure(logger, logging.ERROR, "exercise_other_save_failed", e,
                    source_app=activity.get("source_app"),
                    source_activity_id=activity.get("source_activity_id"))
        return False
    finally:
        if conn is not None:
            conn.close()


# Looks up which of the given source ids are already saved in any exercise table.
# Inputs: source_app (e.g. "garmin"), list of source activity ids as text.
# Outputs: {source_activity_id: activity_name} for the ids found. Raises on DB error so
#          the caller skips the run rather than re-saving everything it cannot check.
def get_recorded_activities(source_app: str, source_activity_ids: list[str]) -> dict[str, str | None]:
    if not source_activity_ids:
        return {}
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_activity_id, activity_name
                    FROM exercise.cardio_activities
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    UNION ALL
                    SELECT source_activity_id, activity_name
                    FROM exercise.strength_sessions
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    UNION ALL
                    SELECT source_activity_id, activity_name
                    FROM exercise.other_exercises
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    """,
                    {"app": source_app, "ids": list(source_activity_ids)},
                )
                return {row[0]: row[1] for row in cur.fetchall()}
    finally:
        conn.close()


# True if a cardio or other session from a different source starts within two minutes
# of started_at — the same workout already recorded before this source took over.
# Strength rows are not checked: they have always been keyed on the Garmin id.
# Inputs: source_app of the incoming activity, its UTC start.
# Outputs: bool. Raises on DB error so the caller retries rather than duplicating.
def find_activity_from_other_source(source_app: str, started_at) -> bool:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM exercise.cardio_activities
                    WHERE source_app <> %(app)s AND started_at BETWEEN %(lo)s AND %(hi)s
                    UNION ALL
                    SELECT 1 FROM exercise.other_exercises
                    WHERE source_app <> %(app)s AND started_at BETWEEN %(lo)s AND %(hi)s
                    LIMIT 1
                    """,
                    {
                        "app": source_app,
                        "lo": started_at - _SAME_SESSION_WINDOW,
                        "hi": started_at + _SAME_SESSION_WINDOW,
                    },
                )
                return cur.fetchone() is not None
    finally:
        conn.close()


# Copies activity names changed at the source onto the rows that source created
# directly. Rows that came in through the old Strava trigger keep their name, which
# may have been edited on Strava.
# Inputs: source_app, {source_activity_id: current name at the source}.
# Outputs: number of rows renamed. Failures are logged and return 0 (cosmetic only).
def update_activity_names(source_app: str, names: dict[str, str]) -> int:
    if not names:
        return 0
    renamed = 0
    conn = None
    try:
        conn = get_connection()
        with conn:
            with conn.cursor() as cur:
                for table in ("cardio_activities", "strength_sessions", "other_exercises"):
                    cur.execute(
                        f"""
                        UPDATE exercise.{table} t
                        SET activity_name = v.name, updated_at = now()
                        FROM unnest(%s::text[], %s::text[]) AS v(source_activity_id, name)
                        WHERE t.source_app = %s
                          AND t.source_activity_id = v.source_activity_id
                          AND t.strava_activity_id IS NULL
                          AND t.activity_name IS DISTINCT FROM v.name
                        """,
                        (list(names.keys()), list(names.values()), source_app),
                    )
                    renamed += cur.rowcount
        if renamed:
            log_event(logger, logging.INFO, "exercise_activity_names_updated",
                      source_app=source_app, renamed=renamed)
        return renamed
    except Exception as e:
        log_failure(logger, logging.WARNING, "exercise_activity_rename_failed", e,
                    source_app=source_app)
        return 0
    finally:
        if conn is not None:
            conn.close()


# Reads meta.presentation (what the site shows for a session, e.g. its photos) from the rows
# the source created. Rows showing the Strava export's presentation are left out: their
# title and photos came from Strava and are kept as they are.
# Inputs: source_app, list of source activity ids as text.
# Outputs: {source_activity_id: presentation} ({} when the row has none). Raises on DB error.
def get_presentations(source_app: str, source_activity_ids: list[str]) -> dict[str, dict]:
    if not source_activity_ids:
        return {}
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_activity_id, meta->'presentation'
                    FROM exercise.cardio_activities
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    UNION ALL
                    SELECT source_activity_id, meta->'presentation'
                    FROM exercise.strength_sessions
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    UNION ALL
                    SELECT source_activity_id, meta->'presentation'
                    FROM exercise.other_exercises
                    WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)
                    """,
                    {"app": source_app, "ids": list(source_activity_ids)},
                )
                rows = cur.fetchall()
    finally:
        conn.close()
    return {source_activity_id: presentation or {} for source_activity_id, presentation in rows
            if (presentation or {}).get("source") != "strava_export"}


# Sets meta.presentation on the row the source created, leaving the rest of meta alone. A row
# showing the Strava export's presentation is never changed.
# Inputs: source_app, source activity id, the new presentation.
# Outputs: True if a row was updated. Raises on DB error.
def save_presentation(source_app: str, source_activity_id: str, presentation: dict) -> bool:
    updated = 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for table in ("cardio_activities", "strength_sessions", "other_exercises"):
                    cur.execute(
                        f"""
                        UPDATE exercise.{table}
                        SET meta = COALESCE(meta, '{{}}'::jsonb)
                                   || jsonb_build_object('presentation', %s::jsonb),
                            updated_at = now()
                        WHERE source_app = %s AND source_activity_id = %s
                          AND COALESCE(meta->'presentation'->>'source', '') <> 'strava_export'
                        """,
                        (json.dumps(presentation), source_app, source_activity_id),
                    )
                    updated += cur.rowcount
    finally:
        conn.close()
    return updated > 0


# Lists the newest rows the source created, in any exercise table.
# Inputs: source_app, a UTC datetime the rows started at or after (None for any time), and
#         how many to return at most.
# Outputs: source activity ids as text, newest first. Raises on DB error.
def get_recent_activity_ids(source_app: str, since, limit: int) -> list[str]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_activity_id FROM (
                        SELECT source_activity_id, started_at FROM exercise.cardio_activities
                        WHERE source_app = %(app)s
                        UNION ALL
                        SELECT source_activity_id, started_at FROM exercise.strength_sessions
                        WHERE source_app = %(app)s
                        UNION ALL
                        SELECT source_activity_id, started_at FROM exercise.other_exercises
                        WHERE source_app = %(app)s
                    ) recorded
                    WHERE source_activity_id IS NOT NULL
                      AND (%(since)s::timestamptz IS NULL OR started_at >= %(since)s)
                    ORDER BY started_at DESC
                    LIMIT %(limit)s
                    """,
                    {"app": source_app, "since": since, "limit": limit},
                )
                return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


# Deletes the rows the source created for the given ids, with their cardio splits and
# strength sets, in one transaction. The planner's records of them are left to
# week_planner.reconcile, which reopens a plan whose workout is gone.
# Inputs: source_app, source activity ids as text. Outputs: rows deleted. Raises on DB error.
def delete_activities(source_app: str, source_activity_ids: list[str]) -> int:
    if not source_activity_ids:
        return 0
    params = {"app": source_app, "ids": list(source_activity_ids)}
    deleted = 0
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM exercise.cardio_splits WHERE cardio_activity_id IN (
                        SELECT cardio_activity_id FROM exercise.cardio_activities
                        WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s))
                    """, params)
                cur.execute(
                    """
                    DELETE FROM exercise.strength_sets WHERE strength_session_id IN (
                        SELECT strength_session_id FROM exercise.strength_sessions
                        WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s))
                    """, params)
                for table in ("cardio_activities", "strength_sessions", "other_exercises"):
                    cur.execute(
                        f"DELETE FROM exercise.{table} "
                        "WHERE source_app = %(app)s AND source_activity_id = ANY(%(ids)s)", params)
                    deleted += cur.rowcount
    finally:
        conn.close()
    log_event(logger, logging.INFO, "exercise_activities_deleted",
              source_app=source_app, deleted=deleted, source_activity_ids=",".join(source_activity_ids))
    return deleted
