"""
Exercise domain — persists cardio activities from Strava to exercise.cardio_activities
and exercise.cardio_splits.

Functions:
  save_cardio_activity(strava_inbound_id, activity) — classifies, upserts activity row,
      replaces splits; returns (saved, activity_category) tuple
  delete_cardio_activity(strava_activity_id)        — deletes one activity row (and its splits via CASCADE)
  _classify_activity(sport_type)  — maps Strava sport_type to activity_category or None
  _extract_timezone(tz_str)       — extracts IANA timezone from Strava timezone string
  _build_splits(activity)         — merges laps + splits_metric into per-km split rows
"""

import json
import logging

from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

# Strava sport_type → Project B activity_category.
# Types not listed here that are not weight training fall back to "other_cardio".
_RUN_TYPES = {"Run", "TrailRun", "VirtualRun", "Treadmill"}
_WALK_TYPES = {"Walk", "Hike"}
_RIDE_TYPES = {"Ride", "VirtualRide", "EBikeRide", "MountainBikeRide", "GravelRide", "Velomobile"}
_SWIM_TYPES = {"Swim", "OpenWaterSwim"}
_SKIP_TYPES = {"WeightTraining", "Workout", "Crossfit", "RockClimbing", "Yoga", "Pilates"}


# Classifies a Strava sport_type into a Project B activity_category.
# Returns None for weight training and non-exercise types that should not be saved here.
def _classify_activity(sport_type: str) -> str | None:
    if sport_type in _SKIP_TYPES:
        return None
    if sport_type in _RUN_TYPES:
        return "run"
    if sport_type in _WALK_TYPES:
        return "walk"
    if sport_type in _RIDE_TYPES:
        return "ride"
    if sport_type in _SWIM_TYPES:
        return "swim"
    return "other_cardio"


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


# Upserts one cardio activity and replaces its splits.
# Returns (saved, activity_category): saved=False means the sport_type is skipped (e.g. WeightTraining).
# Inputs: strava_inbound_id from system.strava_inbound, full Strava activity detail dict.
def save_cardio_activity(strava_inbound_id: int, activity: dict) -> tuple[bool, str | None]:
    sport_type = activity.get("sport_type", "")
    category = _classify_activity(sport_type)
    if category is None:
        log_event(logger, logging.INFO, "exercise_activity_skipped",
                  sport_type=sport_type, strava_activity_id=activity.get("id"))
        return False, None

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
                        int(activity["calories"]) if activity.get("calories") else None,
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
