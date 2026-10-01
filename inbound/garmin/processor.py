"""
Garmin Connect activity processor — turns one Garmin activity into an exercise row and a
Telegram confirmation.

Called by inbound.garmin.sync for every activity not yet recorded. The Garmin payload is
normalized into the exercise domain's activity dict, so classification, the tables written
and the message formats are the same ones the Strava feed produced.

Functions:
  process_activity(client, listed, notify, now, source) — fetches the activity detail (plus
      laps for cardio, exercise sets and HR for strength), stores the raw payload in
      system.garmin_inbound, saves the exercise row and, when the row is new and notify is
      set, sends the Telegram confirmation and planner nudge. Returns the outcome.
  map_sport_type(activity_type)  — Garmin activityType → (sport_type, is_treadmill)
  parse_garmin_time(value)       — Garmin GMT timestamp → UTC datetime
  _normalize_activity(...)       — Garmin detail + laps → exercise domain activity dict
  _normalize_splits(splits, category) — Garmin lapDTOs → exercise.cardio_splits rows
  _cadence(category, *sources)   — cadence in the stored unit (one-foot count for run/walk)
  _timezone_name(summary, started_at) — IANA zone the activity was recorded in
  _fetch_exercise_sets(client, garmin_activity_id) — raw exercise sets for a session
  _fetch_activity_hr(client, garmin_activity_id)   — second-by-second HR samples
  _first_fetch_at(garmin_activity_id) — when the activity was first fetched (sets wait)
  _store_garmin_inbound(object_id, payload, source) — inserts one system.garmin_inbound row
  _send_confirmation(text, garmin_activity_id) — sends and logs one proactive Telegram message
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from domains.exercise.activity_formatter import format_activity_notification
from domains.exercise.service import (
    CARDIO_CATEGORIES,
    classify_activity,
    save_cardio_activity,
    save_other_exercise,
)
from domains.exercise.strength_formatter import format_strength_notification
from domains.exercise.strength_service import parse_active_sets, save_strength_session
from system.db import get_connection
from system.logging import log_event, log_failure
from system.timezone import get_timezone
from telegram.replies import get_latest_chat_id, send_logged

logger = logging.getLogger(__name__)

# Garmin activityType.typeKey → (sport_type, is_treadmill). sport_type is the vocabulary the
# exercise tables already hold — the names Garmin activities carried when they arrived via
# Strava — so the same workout classifies, labels and stores exactly as before.
_SPORT_TYPES = {
    "running": ("Run", False),
    "street_running": ("Run", False),
    "track_running": ("Run", False),
    "ultra_run": ("Run", False),
    "obstacle_run": ("Run", False),
    "treadmill_running": ("Run", True),
    "indoor_running": ("Run", True),
    "trail_running": ("TrailRun", False),
    "virtual_run": ("VirtualRun", True),
    "walking": ("Walk", False),
    "casual_walking": ("Walk", False),
    "speed_walking": ("Walk", False),
    "hiking": ("Hike", False),
    "rucking": ("Hike", False),
    "cycling": ("Ride", False),
    "road_biking": ("Ride", False),
    "indoor_cycling": ("Ride", True),
    "virtual_ride": ("VirtualRide", True),
    "mountain_biking": ("MountainBikeRide", False),
    "gravel_cycling": ("GravelRide", False),
    "e_bike_fitness": ("EBikeRide", False),
    "lap_swimming": ("Swim", False),
    "swimming": ("Swim", False),
    "open_water_swimming": ("OpenWaterSwim", False),
    "strength_training": ("WeightTraining", False),
    "indoor_cardio": ("Workout", False),
    "other": ("Workout", False),
    "hiit": ("HighIntensityIntervalTraining", False),
    "yoga": ("Yoga", False),
    "pilates": ("Pilates", False),
    "elliptical": ("Elliptical", False),
    "stair_climbing": ("StairStepper", False),
    "indoor_rowing": ("Rowing", False),
    "rowing": ("Rowing", False),
    "indoor_climbing": ("RockClimbing", False),
    "bouldering": ("RockClimbing", False),
    "rock_climbing": ("RockClimbing", False),
}

# Child types missing from the map above fall back to their parent (activityType.parentTypeId).
_PARENT_SPORT_TYPES = {
    1: ("Run", False),
    2: ("Ride", False),
    3: ("Hike", False),
    9: ("Walk", False),
    26: ("Swim", False),
}

# Recorded on the watch but not exercise; never written to the exercise tables.
IGNORED_TYPES = {"meditation", "breathwork"}

# Garmin often lists a strength session before its exercise sets are processed. A
# strength_training session without sets is re-checked on each sync for this long after
# it was first fetched, then saved without sets.
_SETS_GRACE = timedelta(minutes=30)


# Maps a Garmin activityType dict to (sport_type, is_treadmill). Unknown keys become a
# CamelCase sport_type ("tai_chi" → "TaiChi") and classify as "other".
def map_sport_type(activity_type: dict) -> tuple[str, bool]:
    type_key = (activity_type.get("typeKey") or "").lower()
    if type_key in _SPORT_TYPES:
        return _SPORT_TYPES[type_key]
    parent = _PARENT_SPORT_TYPES.get(activity_type.get("parentTypeId"))
    if parent:
        return parent
    return "".join(part.capitalize() for part in type_key.split("_")), False


# Parses a Garmin GMT timestamp ("2026-09-29 06:05:12", "2026-09-29T06:05:12.0" or epoch ms)
# into a UTC datetime. Raises ValueError on anything else.
def parse_garmin_time(value) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    text = str(value or "").strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError("unrecognised Garmin timestamp")


# Processes one Garmin activity end to end. Called by inbound.garmin.sync for activities
# that are not yet recorded; the unique source id on each table keeps a repeat harmless.
# Inputs: logged-in GarminApiClient, the activity's entry from the activity list, whether
#         a new row should be confirmed on Telegram, the run's current UTC time, and the
#         system.garmin_inbound source label (see inbound.garmin.sync.run_activity_sync).
# Outputs: "saved" (new row), "not_saved" (already recorded or save failed; logged),
#          "waiting" (strength sets not synced yet) or "ignored" (not exercise).
#          Raises on Garmin API errors so the caller retries on its next run.
def process_activity(client, listed: dict, notify: bool, now: datetime, source: str = "manual") -> str:
    garmin_activity_id = listed["activityId"]
    activity_type = listed.get("activityType") or {}
    type_key = activity_type.get("typeKey")

    if type_key in IGNORED_TYPES:
        log_event(logger, logging.INFO, "garmin_activity_ignored",
                  garmin_activity_id=garmin_activity_id, type_key=type_key)
        return "ignored"

    sport_type, is_treadmill = map_sport_type(activity_type)
    category = classify_activity(sport_type)
    log_event(logger, logging.INFO, "garmin_activity_processing",
              garmin_activity_id=garmin_activity_id, type_key=type_key,
              sport_type=sport_type, category=category, notify=notify)

    summary = client.connectapi(f"/activity-service/activity/{garmin_activity_id}") or {}
    payload = {"activity": listed, "summary": summary}

    if category == "strength":
        exercise_sets = _fetch_exercise_sets(client, garmin_activity_id)
        payload["exercise_sets"] = exercise_sets
        summary_dto = summary.get("summaryDTO") or {}
        session_start = summary_dto.get("startTimeGMT") or summary_dto.get("startTimeLocal")
        has_sets = bool(parse_active_sets(exercise_sets, session_start_str=session_start))

        if not has_sets and sport_type != "WeightTraining":
            # Cardio / Other sessions only count as strength when sets were recorded.
            category = "other"
        elif not has_sets:
            first_fetched = _first_fetch_at(garmin_activity_id)
            if first_fetched is None or now - first_fetched < _SETS_GRACE:
                if first_fetched is None:
                    _store_garmin_inbound(garmin_activity_id, payload, source)
                log_event(logger, logging.INFO, "garmin_strength_sets_pending",
                          garmin_activity_id=garmin_activity_id,
                          raw_set_count=len(exercise_sets))
                return "waiting"
            log_event(logger, logging.WARNING, "garmin_strength_saved_without_sets",
                      garmin_activity_id=garmin_activity_id,
                      raw_set_count=len(exercise_sets))

        if category == "strength":
            payload["hr_samples"] = _fetch_activity_hr(client, garmin_activity_id) if has_sets else []

    elif category in CARDIO_CATEGORIES:
        payload["splits"] = client.connectapi(
            f"/activity-service/activity/{garmin_activity_id}/splits"
        ) or {}

    garmin_inbound_id = _store_garmin_inbound(garmin_activity_id, payload, source)
    activity = _normalize_activity(listed, summary, payload.get("splits"),
                                   sport_type, is_treadmill, category, garmin_inbound_id)

    if category == "strength":
        return _save_strength(activity, summary, payload, notify)

    saved = (save_cardio_activity(activity) if category in CARDIO_CATEGORIES
             else save_other_exercise(activity))
    if not saved:
        return "not_saved"

    if notify:
        _send_confirmation(format_activity_notification(activity, category), garmin_activity_id)
        # A new cardio session reconciles its planned day and sends the weekly tally nudge.
        # Lazy-imported and wrapped so it can never affect ingestion.
        if category in CARDIO_CATEGORIES:
            try:
                from domains.health_agent.week_planner.activity_nudge import notify_activity_landed
                distance_m = activity.get("distance_m")
                detail = f"{category} ({distance_m / 1000:.1f} km)" if distance_m else category
                notify_activity_landed(activity["started_at"], "cardio", detail)
            except Exception as e:
                log_failure(logger, logging.WARNING, "cardio_reconcile_nudge_failed", e,
                            garmin_activity_id=garmin_activity_id)
    return "saved"


# Saves a strength session from the stored payload and, when new and notify is set, sends
# the set-table confirmation and the planner nudge.
# Inputs: normalized activity dict, Garmin activity detail, the stored payload, notify flag.
# Outputs: "saved" or "not_saved", as for process_activity.
def _save_strength(activity: dict, summary: dict, payload: dict, notify: bool) -> str:
    strength_session_id, parsed_sets, created = save_strength_session(
        garmin_inbound_id=activity["inbound_row_id"],
        summary=summary,
        exercise_sets=payload["exercise_sets"],
        started_at=activity["started_at"],
        hr_samples=payload["hr_samples"],
        extra_meta={**activity["meta"], "sport_type": activity["sport_type"],
                    "timezone": activity["timezone"]},
    )
    if not created:
        return "not_saved"
    if not notify:
        return "saved"

    summary_dto = summary.get("summaryDTO") or {}
    duration_raw = summary_dto.get("duration") or summary_dto.get("elapsedDuration")
    avg_hr_raw = summary_dto.get("averageHR") or summary_dto.get("averageHeartRate")
    max_hr_raw = summary_dto.get("maxHR") or summary_dto.get("maxHeartRate")
    calories_raw = summary_dto.get("calories") or summary_dto.get("activeKilocalories")

    try:
        text = format_strength_notification(
            activity_name=activity["name"],
            started_at=activity["started_at"],
            duration_seconds=int(duration_raw) if duration_raw else None,
            avg_hr=float(avg_hr_raw) if avg_hr_raw is not None else None,
            max_hr=float(max_hr_raw) if max_hr_raw is not None else None,
            calories_kcal=int(calories_raw) if calories_raw else None,
            parsed_sets=parsed_sets,
            timezone_str=activity["timezone"],
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "garmin_strength_format_failed", e,
                    strength_session_id=strength_session_id)
        return "saved"

    _send_confirmation(text, activity["source_activity_id"])

    # A new strength session reconciles its planned day and sends the weekly tally nudge.
    # Lazy-imported and wrapped so it can never affect ingestion.
    try:
        from domains.health_agent.week_planner.activity_nudge import notify_activity_landed
        notify_activity_landed(activity["started_at"], "strength", "strength session")
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_reconcile_nudge_failed", e,
                    strength_session_id=strength_session_id)
    return "saved"


# Builds the exercise domain's activity dict from Garmin's detail (summaryDTO), falling back
# to the activity-list entry for any field the detail lacks.
# Inputs: list entry, activity detail, splits response (cardio only), mapped sport_type,
#         treadmill flag, routing category, and the system.garmin_inbound row id.
# Outputs: normalized activity dict (keys documented in domains.exercise.service).
def _normalize_activity(listed: dict, summary: dict, splits: dict | None, sport_type: str,
                        is_treadmill: bool, category: str, inbound_row_id: int) -> dict:
    summary_dto = summary.get("summaryDTO") or {}

    # First non-null value for any of the keys, detail before list entry.
    def pick(*keys):
        for source in (summary_dto, listed):
            for key in keys:
                if source.get(key) is not None:
                    return source[key]
        return None

    started_at = parse_garmin_time(summary_dto.get("startTimeGMT") or listed.get("startTimeGMT"))
    duration = pick("elapsedDuration", "duration")
    moving = pick("movingDuration", "duration")
    rpe = summary_dto.get("directWorkoutRpe")
    device_id = (
        ((summary.get("metadataDTO") or {}).get("deviceMetaDataDTO") or {}).get("deviceId")
        or listed.get("deviceId")
    )
    meta = {"garmin_type_key": (listed.get("activityType") or {}).get("typeKey")}
    if device_id:
        meta["device_id"] = str(device_id)

    return {
        "source_app": "garmin",
        "source_activity_id": str(listed["activityId"]),
        "inbound_row_id": inbound_row_id,
        "name": summary.get("activityName") or listed.get("activityName") or "Activity",
        "sport_type": sport_type,
        "is_treadmill": is_treadmill,
        "started_at": started_at,
        "timezone": _timezone_name(summary, started_at),
        "duration_seconds": round(duration) if duration is not None else 0,
        "moving_seconds": round(moving) if moving is not None else 0,
        "distance_m": pick("distance"),
        "elevation_gain_m": pick("elevationGain"),
        "elev_high_m": pick("maxElevation"),
        "elev_low_m": pick("minElevation"),
        "average_speed_mps": pick("averageSpeed"),
        "max_speed_mps": pick("maxSpeed"),
        "average_cadence": _cadence(category, summary_dto, listed),
        "average_heartrate": pick("averageHR"),
        "max_heartrate": pick("maxHR"),
        "calories_kcal": pick("calories"),
        "perceived_exertion": round(rpe / 10) if rpe else None,
        "gear_name": None,
        "device_name": None,
        "polyline": None,
        "start_lat": pick("startLatitude"),
        "start_lng": pick("startLongitude"),
        "splits": _normalize_splits(splits, category) if category in CARDIO_CATEGORIES else [],
        "meta": meta,
    }


# Converts Garmin's lapDTOs (one per auto-lap, usually 1 km) into exercise.cardio_splits
# rows. Laps missing distance or time are skipped — those columns are NOT NULL.
# Inputs: /splits response, routing category (for the cadence unit).
# Outputs: list of split dicts keyed by column name, in lap order.
def _normalize_splits(splits: dict | None, category: str) -> list[dict]:
    rows = []
    for index, lap in enumerate((splits or {}).get("lapDTOs") or [], start=1):
        distance = lap.get("distance")
        elapsed = lap.get("elapsedDuration") or lap.get("duration")
        if distance is None or elapsed is None:
            log_event(logger, logging.WARNING, "garmin_split_incomplete_lap_skipped",
                      lap_index=index, has_distance=distance is not None,
                      has_elapsed=elapsed is not None)
            continue
        moving = lap.get("movingDuration")
        gain = lap.get("elevationGain")
        loss = lap.get("elevationLoss")
        rows.append({
            "lap_index": index,
            "distance_m": distance,
            "elapsed_seconds": round(elapsed),
            "moving_seconds": round(moving) if moving is not None else None,
            "average_speed_mps": lap.get("averageSpeed"),
            "max_speed_mps": lap.get("maxSpeed"),
            "average_cadence": _cadence(category, lap),
            "average_heartrate": lap.get("averageHR"),
            "max_heartrate": lap.get("maxHR"),
            "elevation_gain_m": gain,
            "elevation_difference_m": gain - loss if gain is not None and loss is not None else None,
            "grade_adjusted_speed_mps": lap.get("avgGradeAdjustedSpeed"),
            "pace_zone": None,
        })
    return rows


# Cadence in the unit the exercise tables store. Garmin reports run and walk cadence as
# steps per minute (both feet); the stored value is the one-foot count, so it is halved.
# Ride cadence is pedal rpm, swim cadence strokes per minute. Other activities have none.
# Inputs: routing category, then the dicts to read in order (detail, list entry or a lap).
def _cadence(category: str, *sources: dict) -> float | None:
    keys = {
        "run": ("averageRunCadence", "averageRunningCadenceInStepsPerMinute"),
        "walk": ("averageRunCadence", "averageRunningCadenceInStepsPerMinute"),
        "ride": ("averageBikeCadence", "averageBikingCadenceInRevPerMinute"),
        "swim": ("averageSwimCadence", "averageSwimCadenceInStrokesPerMinute"),
    }.get(category, ())
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value:
                return round(value / 2, 1) if category in ("run", "walk") else value
    return None


# The IANA zone the activity was recorded in, from the detail's timeZoneUnitDTO. Falls back
# to where B was at the time (system.timezone), then Asia/Singapore.
def _timezone_name(summary: dict, started_at: datetime) -> str:
    zone = summary.get("timeZoneUnitDTO") or {}
    for name in (zone.get("timeZone"), zone.get("unitKey")):
        if not name:
            continue
        try:
            ZoneInfo(name)
            return name
        except Exception:
            continue
    return str(get_timezone(started_at))


# Fetches the raw exercise sets for one activity. Returns [] when there are none or the
# call fails (logged) — the caller treats that as "sets not synced yet".
def _fetch_exercise_sets(client, garmin_activity_id: int) -> list:
    try:
        response = client.connectapi(f"/activity-service/activity/{garmin_activity_id}/exerciseSets")
    except Exception as e:
        log_failure(logger, logging.WARNING, "garmin_exercise_sets_fetch_failed", e,
                    garmin_activity_id=garmin_activity_id)
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        return response.get("exerciseSets") or []
    return []


# Fetches the second-by-second HR time series from the activity details endpoint.
# Parses metricDescriptors to find directHeartRate and directTimestamp indices,
# then extracts (timestamp_ms, hr_bpm) pairs from activityDetailMetrics.
# Inputs: logged-in Garmin client, Garmin activityId integer.
# Outputs: list of (timestamp_ms float, hr_bpm float) sorted by timestamp. [] on failure.
def _fetch_activity_hr(client, garmin_activity_id: int) -> list[tuple[float, float]]:
    try:
        details = client.connectapi(
            f"/activity-service/activity/{garmin_activity_id}/details",
            params={"maxChartSize": 2000, "maxPolylineSize": 4000},
        )
        if not details:
            return []
        descriptors = details.get("metricDescriptors") or []
        ts_idx = hr_idx = None
        for d in descriptors:
            key = d.get("key", "")
            idx = d.get("metricsIndex")
            if key == "directTimestamp":
                ts_idx = idx
            elif key == "directHeartRate":
                hr_idx = idx
        if ts_idx is None or hr_idx is None:
            log_event(logger, logging.WARNING, "garmin_hr_descriptors_missing",
                      garmin_activity_id=garmin_activity_id,
                      found_keys=[d.get("key") for d in descriptors])
            return []
        samples = []
        for row in (details.get("activityDetailMetrics") or []):
            metrics = row.get("metrics") or []
            if len(metrics) > max(ts_idx, hr_idx):
                ts = metrics[ts_idx]
                hr = metrics[hr_idx]
                if ts is not None and hr is not None and hr > 0:
                    samples.append((float(ts), float(hr)))
        samples.sort(key=lambda x: x[0])
        log_event(logger, logging.INFO, "garmin_hr_samples_fetched",
                  garmin_activity_id=garmin_activity_id,
                  sample_count=len(samples))
        return samples
    except Exception as e:
        log_failure(logger, logging.WARNING, "garmin_hr_details_fetch_failed", e,
                    garmin_activity_id=garmin_activity_id)
        return []


# When this activity was first fetched, from system.garmin_inbound; None if never.
# Used to bound how long a strength session waits for its sets.
def _first_fetch_at(garmin_activity_id: int) -> datetime | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT min(received_at) FROM system.garmin_inbound WHERE object_id = %s",
                    (garmin_activity_id,),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


# Inserts one row into system.garmin_inbound and returns the new garmin_inbound_id.
# Inputs: Garmin activityId, the payload fetched for it, and why it was fetched.
# Outputs: garmin_inbound_id of the inserted row.
def _store_garmin_inbound(object_id: int, payload: dict, source: str) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system.garmin_inbound (object_id, payload, source)
                    VALUES (%s, %s, %s)
                    RETURNING garmin_inbound_id
                    """,
                    (object_id, json.dumps(payload), source),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


# Sends one proactive confirmation to B's chat and logs it to system.telegram_outbound.
# A missing chat id or a failed send is logged; the saved row is unaffected.
def _send_confirmation(text: str, garmin_activity_id) -> None:
    chat_id = get_latest_chat_id()
    if chat_id is None:
        log_event(logger, logging.WARNING, "garmin_no_chat_id",
                  garmin_activity_id=garmin_activity_id)
        return
    message_id = send_logged(chat_id, text)
    log_event(logger, logging.INFO, "garmin_notification_sent",
              garmin_activity_id=garmin_activity_id, chat_id=chat_id, message_id=message_id)
