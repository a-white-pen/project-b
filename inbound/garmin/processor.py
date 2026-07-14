"""
Garmin Connect strength session processor — fetches activity detail when a Strava
WeightTraining/Workout/Crossfit event fires, stores the raw payload in
system.garmin_inbound, parses it into exercise.strength_sessions + strength_sets,
and sends a Telegram notification.

Functions:
  process_strength_event(strava_inbound_id, strava_activity) — main entry point;
      called from inbound.strava.processor when sport_type is a strength type.
      Matches the Garmin activity by start time, fetches detail + exercise sets,
      stores raw payload (Phase 1), parses + saves structured rows (Phase 2),
      and sends Telegram notification. Retries up to 3 times if Garmin hasn't synced.
  _match_garmin_activity(client, strava_start_dt) — searches Garmin for an activity
      within ±120s of the Strava start time; returns the Garmin activity dict or None.
  fetch_garmin_detail(client, garmin_activity_id) — fetches full summary and
      exercise sets for one Garmin activity.
  _store_garmin_inbound(object_id, payload, strava_inbound_id) — inserts one row
      into system.garmin_inbound; returns the new garmin_inbound_id.

Shared outbound helpers (get_latest_chat_id, store_outbound) live in telegram.replies.
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from domains.exercise.strength_formatter import format_strength_notification
from domains.exercise.strength_service import parse_active_sets, save_strength_session
from inbound.garmin.client import get_garmin_client
from system.db import get_connection
from system.logging import log_event, log_failure
from telegram.replies import get_latest_chat_id, send_reply, store_outbound

logger = logging.getLogger(__name__)

# Retry delays (seconds) when Garmin hasn't synced the session yet.
_RETRY_DELAYS = [90, 240, 600]


# Entry point — called as a FastAPI BackgroundTask from inbound.strava.processor.
# Fetches the matching Garmin activity for a Strava strength event, stores the raw
# payload (Phase 1), parses + saves structured rows (Phase 2), and sends Telegram
# notification. Retries on a fixed schedule if Garmin hasn't processed the session yet.
# Inputs: strava_inbound_id (FK to system.strava_inbound), full Strava activity dict.
# Outputs: none — result logged; rows written to strength_sessions / strength_sets;
#          notification sent to Telegram.
def process_strength_event(strava_inbound_id: int, strava_activity: dict) -> None:
    sport_type = strava_activity.get("sport_type", "")
    strava_activity_id = strava_activity.get("id")
    strava_start_str = strava_activity.get("start_date", "")

    log_event(logger, logging.INFO, "garmin_strength_event_started",
              strava_inbound_id=strava_inbound_id,
              strava_activity_id=strava_activity_id,
              sport_type=sport_type)

    try:
        strava_start_dt = datetime.fromisoformat(
            strava_start_str.replace("Z", "+00:00")
        )
    except (ValueError, AttributeError):
        log_event(logger, logging.ERROR, "garmin_strength_invalid_start_date",
                  strava_inbound_id=strava_inbound_id,
                  strava_activity_id=strava_activity_id)
        return

    for attempt in range(len(_RETRY_DELAYS) + 1):
        if attempt > 0:
            delay = _RETRY_DELAYS[attempt - 1]
            log_event(logger, logging.INFO, "garmin_strength_retry_wait",
                      strava_inbound_id=strava_inbound_id,
                      attempt=attempt,
                      delay_seconds=delay)
            # Known limitation: time.sleep() inside a FastAPI background task holds a
            # Cloud Run instance for up to 15.5 min. The instance may be killed mid-sleep
            # on scale-to-zero. Acceptable for now; proper fix is Cloud Tasks enqueue.
            time.sleep(delay)

        try:
            client = get_garmin_client()
            garmin_activity = _match_garmin_activity(client, strava_start_dt)

            if garmin_activity is None:
                log_event(logger, logging.WARNING, "garmin_strength_no_match",
                          strava_inbound_id=strava_inbound_id,
                          attempt=attempt)
                if attempt < len(_RETRY_DELAYS):
                    continue
                log_event(logger, logging.WARNING, "garmin_strength_match_exhausted",
                          strava_inbound_id=strava_inbound_id,
                          strava_activity_id=strava_activity_id)
                return

            garmin_activity_id = garmin_activity.get("activityId")
            summary, exercise_sets, hr_samples = fetch_garmin_detail(client, garmin_activity_id)

            # Guard: Garmin often syncs the activity summary before exercise sets arrive.
            # Treat empty sets as a transient failure and retry on the same schedule as
            # no-match; on the final attempt log a warning and give up without saving.
            if not exercise_sets:
                log_event(logger, logging.WARNING, "garmin_strength_empty_sets",
                          garmin_activity_id=garmin_activity_id,
                          strava_inbound_id=strava_inbound_id,
                          attempt=attempt)
                if attempt < len(_RETRY_DELAYS):
                    continue
                log_event(logger, logging.WARNING, "garmin_strength_sets_exhausted",
                          garmin_activity_id=garmin_activity_id,
                          strava_inbound_id=strava_inbound_id,
                          strava_activity_id=strava_activity_id)
                return

            # Guard: a non-empty raw payload can still parse to zero active sets if
            # all rows are REST/WARMUP (Garmin tags set types in a second pass).
            # Retry on the same schedule; give up without saving on the final attempt.
            # Count-only check; HR is correlated later inside save_strength_session.
            _summary_dto = summary.get("summaryDTO") or {}
            _session_start = _summary_dto.get("startTimeGMT") or _summary_dto.get("startTimeLocal")
            if not parse_active_sets(exercise_sets, session_start_str=_session_start):
                log_event(logger, logging.WARNING, "garmin_strength_no_active_sets",
                          garmin_activity_id=garmin_activity_id,
                          raw_set_count=len(exercise_sets),
                          strava_inbound_id=strava_inbound_id,
                          attempt=attempt)
                if attempt < len(_RETRY_DELAYS):
                    continue
                log_event(logger, logging.WARNING, "garmin_strength_active_sets_exhausted",
                          garmin_activity_id=garmin_activity_id,
                          strava_inbound_id=strava_inbound_id,
                          strava_activity_id=strava_activity_id)
                return

            payload = {"summary": summary, "exercise_sets": exercise_sets, "hr_samples": hr_samples}
            garmin_inbound_id = _store_garmin_inbound(
                garmin_activity_id, payload, strava_inbound_id
            )

            log_event(logger, logging.INFO, "garmin_payload_captured",
                      garmin_inbound_id=garmin_inbound_id,
                      garmin_activity_id=garmin_activity_id,
                      strava_inbound_id=strava_inbound_id,
                      has_exercise_sets=bool(exercise_sets),
                      set_count=len(exercise_sets) if isinstance(exercise_sets, list) else 0,
                      attempt=attempt)

            # Phase 2 — parse and save structured rows, then notify.
            _parse_and_notify(
                garmin_inbound_id=garmin_inbound_id,
                summary=summary,
                exercise_sets=exercise_sets,
                hr_samples=hr_samples,
                strava_inbound_id=strava_inbound_id,
                strava_activity_id=strava_activity_id,
                strava_activity=strava_activity,
            )
            return

        except Exception as e:
            log_failure(logger, logging.ERROR, "garmin_strength_event_failed", e,
                        strava_inbound_id=strava_inbound_id,
                        attempt=attempt)
            if attempt < len(_RETRY_DELAYS):
                continue
            return


# Parses the captured Garmin payload into structured rows and sends Telegram notification.
# Inputs: all fields needed for save_strength_session + the original Strava activity
#         dict (used to extract timezone and start_date for local time display).
#         strava_start_dt: optional UTC datetime override — used by the replay script
#         where strava_activity is the webhook event body (no start_date field).
# Outputs: none — rows written to DB; notification sent; failures logged and swallowed.
def _parse_and_notify(
    garmin_inbound_id: int,
    summary: dict,
    exercise_sets: list,
    hr_samples: list,
    strava_inbound_id: int,
    strava_activity_id: int | None,
    strava_activity: dict,
    strava_start_dt: datetime | None = None,
) -> None:
    # Parse Strava start_date (UTC) — authoritative time source.
    # summaryDTO.startTimeGMT is true UTC; exercise_set startTime is also UTC — consistent.
    # strava_start_dt may already be set (e.g. passed by the replay script).
    if strava_start_dt is None:
        strava_start_str = strava_activity.get("start_date", "")
        if strava_start_str:
            try:
                strava_start_dt = datetime.fromisoformat(strava_start_str.replace("Z", "+00:00"))
            except ValueError:
                pass

    strength_session_id, parsed_sets, created = save_strength_session(
        garmin_inbound_id=garmin_inbound_id,
        summary=summary,
        exercise_sets=exercise_sets,
        hr_samples=hr_samples,
        strava_inbound_id=strava_inbound_id,
        strava_activity_id=strava_activity_id,
        strava_start_dt=strava_start_dt,
    )
    if strength_session_id is None:
        log_event(logger, logging.WARNING, "garmin_strength_save_failed_no_notify",
                  garmin_inbound_id=garmin_inbound_id)
        return
    if not created:
        # Session already existed (duplicate Strava delivery or retry after partial success).
        # Do not send a second notification.
        log_event(logger, logging.INFO, "garmin_strength_duplicate_skipped",
                  garmin_inbound_id=garmin_inbound_id,
                  strength_session_id=strength_session_id)
        return

    # Extract timezone from Strava activity for local time display.
    tz_raw = strava_activity.get("timezone") or ""
    # Strava format: "(GMT+07:00) Asia/Bangkok" — extract the IANA part.
    timezone_str = tz_raw.split(") ", 1)[1] if ") " in tz_raw else "Asia/Bangkok"

    # Build notification fields from summaryDTO (where Garmin nests session stats).
    summary_dto = summary.get("summaryDTO") or {}
    activity_name = (
        summary.get("activityName") or summary.get("activityDescription") or "Strength Session"
    )
    # Use Strava start_date (UTC) for display; formatter converts to local time via timezone_str.
    started_at = strava_start_dt
    duration_raw = summary_dto.get("duration") or summary_dto.get("elapsedDuration")
    avg_hr_raw = summary_dto.get("averageHR") or summary_dto.get("averageHeartRate")
    max_hr_raw = summary_dto.get("maxHR") or summary_dto.get("maxHeartRate")
    calories_raw = summary_dto.get("calories") or summary_dto.get("activeKilocalories")

    try:
        text = format_strength_notification(
            activity_name=activity_name,
            started_at=started_at,  # UTC datetime; formatter converts to local via timezone_str
            duration_seconds=int(duration_raw) if duration_raw else None,
            avg_hr=float(avg_hr_raw) if avg_hr_raw is not None else None,
            max_hr=float(max_hr_raw) if max_hr_raw is not None else None,
            calories_kcal=int(calories_raw) if calories_raw else None,
            parsed_sets=parsed_sets,
            timezone_str=timezone_str,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "garmin_strength_format_failed", e,
                    strength_session_id=strength_session_id)
        return

    chat_id = get_latest_chat_id()
    if chat_id is None:
        log_event(logger, logging.WARNING, "garmin_strength_no_chat_id",
                  strength_session_id=strength_session_id)
        return

    message_id, sent_payload = send_reply(chat_id, text)
    if message_id is not None:
        store_outbound(message_id, sent_payload)

    log_event(logger, logging.INFO, "garmin_strength_notification_sent",
              strength_session_id=strength_session_id,
              chat_id=chat_id,
              message_id=message_id,
              parsed_sets_count=len(parsed_sets))

    # Spec F: a NEW strength session reconciles its planned day + sends a proactive tally nudge. Reached
    # only on created=True (the early return above blocks re-syncs); lazy-imported + wrapped so it can
    # NEVER affect ingestion.
    try:
        from domains.health_agent.week_planner.activity_nudge import notify_activity_landed
        notify_activity_landed(started_at, "strength", "strength session")
    except Exception as e:
        log_failure(logger, logging.WARNING, "strength_reconcile_nudge_failed", e,
                    strength_session_id=strength_session_id)


# Searches Garmin for an activity within ±120s of the given UTC datetime.
# Fetches activities for a ±1 day window around the Strava start time to account
# for timezone differences, then filters by start time proximity.
# Only returns an activity that started within 120s of strava_start_dt.
# Inputs: logged-in Garmin client, Strava activity start time as UTC datetime.
# Outputs: Garmin activity dict (from the activity list endpoint) or None.
def _match_garmin_activity(client, strava_start_dt: datetime) -> dict | None:
    window_start = (strava_start_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    window_end = (strava_start_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    activities = client.connectapi(
        "/activitylist-service/activities/search/activities",
        params={"startDate": window_start, "endDate": window_end, "limit": 20},
    ) or []

    best = None
    best_delta = timedelta(seconds=120)

    for activity in activities:
        start_str = activity.get("startTimeGMT") or activity.get("startTimeLocal", "")
        if not start_str:
            continue
        try:
            garmin_start = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            continue
        delta = abs(garmin_start - strava_start_dt)
        if delta < best_delta:
            best_delta = delta
            best = activity

    return best


# Fetches full activity detail, exercise sets, and HR time series for one Garmin activity.
# Makes three API calls: activity summary, exercise sets, and second-by-second details.
# Inputs: logged-in Garmin client, Garmin activityId integer.
# Outputs: (summary dict, exercise_sets list, hr_samples list).
#   hr_samples: list of (timestamp_ms, hr_bpm) tuples sorted by time. Empty if unavailable.
def fetch_garmin_detail(client, garmin_activity_id: int) -> tuple[dict, list, list]:
    summary = client.connectapi(
        f"/activity-service/activity/{garmin_activity_id}"
    ) or {}

    try:
        sets_response = client.connectapi(
            f"/activity-service/activity/{garmin_activity_id}/exerciseSets"
        )
        if isinstance(sets_response, list):
            exercise_sets = sets_response
        elif isinstance(sets_response, dict):
            exercise_sets = sets_response.get("exerciseSets", [])
        else:
            exercise_sets = []
    except Exception as e:
        log_failure(logger, logging.WARNING, "garmin_exercise_sets_fetch_failed", e,
                    garmin_activity_id=garmin_activity_id)
        exercise_sets = []

    hr_samples = _fetch_activity_hr(client, garmin_activity_id)

    return summary, exercise_sets, hr_samples


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


# Inserts one row into system.garmin_inbound and returns the new garmin_inbound_id.
# Inputs: Garmin activityId, full payload dict, strava_inbound_id for traceability.
# Outputs: garmin_inbound_id of the inserted row.
def _store_garmin_inbound(
    object_id: int, payload: dict, strava_inbound_id: int
) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system.garmin_inbound
                        (object_id, payload, source, strava_inbound_id)
                    VALUES (%s, %s, 'strava_trigger', %s)
                    RETURNING garmin_inbound_id
                    """,
                    (object_id, json.dumps(payload), strava_inbound_id),
                )
                return cur.fetchone()[0]
    finally:
        conn.close()


