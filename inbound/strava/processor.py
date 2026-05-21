"""
Strava activity processor — fetches activity details, persists to exercise schema,
and sends proactive Telegram notifications.

Functions:
  process_activity_event(strava_inbound_id, activity_id, aspect_type) — fetches Strava activity,
      saves to exercise.cardio_activities + cardio_splits (cardio), or routes to Garmin
      processor (strength); sends Telegram notification for cardio only
  process_delete_event(strava_inbound_id, activity_id) — deletes a cardio activity row and notifies B
  _exchange_token()                                      — exchanges refresh token for access token
  _fetch_activity(access_token, activity_id)             — fetches full activity detail from Strava API
  _activity_label(sport_type, category, is_treadmill)          — maps sport_type to readable label
  _format_notification(activity, aspect_type, saved, category) — builds HTML-formatted Telegram message;
      format: name — label / distance·duration·pace / HR·kcal·cadence / per-km splits with avg/max HR and zone
  _fetch_splits(strava_activity_id)                      — reads per-km splits including max_heartrate and pace_zone
  get_latest_chat_id(), store_outbound() — shared helpers from telegram.replies
"""

import html
import logging
import re

import httpx

from domains.exercise.service import delete_cardio_activity, save_cardio_activity
from domains.exercise.strength_service import delete_strength_session
from inbound.garmin.processor import process_strength_event
from system.config import get_strava_config
from system.db import get_connection
from system.logging import log_event, log_failure
from telegram.replies import get_latest_chat_id, send_reply, store_outbound

logger = logging.getLogger(__name__)

_STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
_STRAVA_ACTIVITY_URL = "https://www.strava.com/api/v3/activities/{activity_id}"

# Fetches Strava activity details and sends a proactive Telegram notification.
# Inputs: strava_inbound_id (PK of the stored event row), activity_id (Strava object_id),
#         aspect_type ("create" or "update") — controls the status line in the notification.
# Outputs: none — notification sent to Telegram, result logged to system.telegram_outbound.
def process_activity_event(strava_inbound_id: int, activity_id: int, aspect_type: str) -> None:
    log_event(logger, logging.INFO, "strava_process_started",
              strava_inbound_id=strava_inbound_id, activity_id=activity_id, aspect_type=aspect_type)
    try:
        access_token = _exchange_token()
        activity = _fetch_activity(access_token, activity_id)
        log_event(logger, logging.INFO, "strava_activity_fetched",
                  strava_inbound_id=strava_inbound_id,
                  activity_id=activity_id,
                  sport_type=activity.get("sport_type"))

        saved, activity_category = save_cardio_activity(strava_inbound_id, activity)

        if activity_category == "strength":
            # Only process on create — update events for the same activity would
            # insert duplicate strength_sessions rows. Garmin data doesn't change
            # meaningfully on Strava updates, so safe to skip.
            if aspect_type != "create":
                log_event(logger, logging.INFO, "strava_strength_update_skipped",
                          strava_inbound_id=strava_inbound_id,
                          activity_id=activity_id,
                          aspect_type=aspect_type)
                return
            # Hand off to Garmin processor — it owns the fetch, storage, and
            # notification for strength sessions. No Telegram message sent from here.
            log_event(logger, logging.INFO, "strava_strength_handed_to_garmin",
                      strava_inbound_id=strava_inbound_id,
                      activity_id=activity_id)
            process_strength_event(strava_inbound_id, activity)
            return

        chat_id = get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        text = _format_notification(activity, aspect_type, saved, activity_category)
        message_id, sent_payload = send_reply(chat_id, text)
        if message_id is not None:
            store_outbound(message_id, sent_payload)
        log_event(logger, logging.INFO, "strava_notification_sent",
                  strava_inbound_id=strava_inbound_id,
                  chat_id=chat_id,
                  message_id=message_id)

    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_process_failed", e,
                    strava_inbound_id=strava_inbound_id, activity_id=activity_id)


# Handles a Strava delete event — removes the activity row and notifies B.
# Inputs: strava_inbound_id from the stored event row, activity_id from the Strava webhook.
# Outputs: none — result logged; notification sent if a row was deleted.
def process_delete_event(strava_inbound_id: int, activity_id: int) -> None:
    log_event(logger, logging.INFO, "strava_delete_started",
              strava_inbound_id=strava_inbound_id, activity_id=activity_id)
    try:
        # Try both table families — we don't know from the delete event which type it was.
        cardio_deleted = delete_cardio_activity(activity_id)
        strength_deleted = delete_strength_session(activity_id)
        deleted = cardio_deleted or strength_deleted

        chat_id = get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        if deleted:
            text = "<i>Activity deleted from Strava — removed from log.</i>"
        else:
            text = "<i>Activity deleted from Strava (was not saved).</i>"

        message_id, sent_payload = send_reply(chat_id, text)
        if message_id is not None:
            store_outbound(message_id, sent_payload)
        log_event(logger, logging.INFO, "strava_delete_notification_sent",
                  strava_inbound_id=strava_inbound_id, activity_id=activity_id, deleted=deleted)

    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_delete_failed", e,
                    strava_inbound_id=strava_inbound_id, activity_id=activity_id)


# Exchanges the stored refresh token for a short-lived Strava access token.
# Inputs: Strava OAuth credentials from StravaConfig.
# Outputs: access token string.
def _exchange_token() -> str:
    cfg = get_strava_config()
    response = httpx.post(
        _STRAVA_TOKEN_URL,
        data={
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
            "refresh_token": cfg.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


# Fetches full activity detail from the Strava API.
# Inputs: short-lived access_token, Strava activity_id.
# Outputs: activity dict from the Strava API response.
def _fetch_activity(access_token: str, activity_id: int) -> dict:
    url = _STRAVA_ACTIVITY_URL.format(activity_id=activity_id)
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# Maps Strava sport_type + category to a human-readable activity label.
def _activity_label(sport_type: str, activity_category: str | None, is_treadmill: bool) -> str:
    if activity_category == "run":
        if is_treadmill:
            return "Treadmill Run"
        return {"TrailRun": "Trail Run", "VirtualRun": "Virtual Run"}.get(sport_type, "Run")
    if activity_category == "walk":
        return "Hike" if sport_type == "Hike" else "Walk"
    if activity_category == "ride":
        return {
            "VirtualRide": "Virtual Ride",
            "MountainBikeRide": "Mountain Bike Ride",
            "GravelRide": "Gravel Ride",
            "EBikeRide": "E-Bike Ride",
        }.get(sport_type, "Ride")
    if activity_category == "swim":
        return "Open Water Swim" if sport_type == "OpenWaterSwim" else "Swim"
    # Convert CamelCase sport_type to readable words for everything else.
    # Note: strength activities (WeightTraining/Workout/Crossfit) are handed off to the
    # Garmin processor before _format_notification is called — this branch is never reached
    # for them, so no explicit strength case is needed here.
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sport_type) or "Activity"


# Builds the proactive Telegram notification text for a cardio activity.
# Format: Line 1 = activity name — type label, Line 2 = distance/duration/pace,
# Line 3 = HR · kcal · cadence. Per-km splits follow with avg/max HR and pace zone.
# Inputs: full Strava activity dict, aspect_type, whether it was saved, and its category.
def _format_notification(activity: dict, aspect_type: str, saved: bool, activity_category: str | None) -> str:
    sport_type = activity.get("sport_type", "")
    is_treadmill = bool(activity.get("trainer"))
    elapsed_seconds = activity.get("elapsed_time") or activity.get("moving_time") or 0
    distance_m = activity.get("distance")  # keep None vs 0 distinct: 0.0 = treadmill/GPS failure
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    cadence = activity.get("average_cadence")
    calories = activity.get("calories")
    activity_name = html.escape(activity.get("name") or "Activity")
    update_prefix = "Updated: " if aspect_type == "update" else ""

    label = _activity_label(sport_type, activity_category, is_treadmill)
    has_distance = (
        distance_m is not None
        and distance_m > 0
        and activity_category in ("run", "walk", "ride", "swim", "other_cardio")
    )

    # Line 1: activity name (bold) — type label
    lines = [f"{update_prefix}<b>{activity_name}</b> — {label}"]

    # Line 2: distance · duration · pace or speed
    duration_str = _format_duration(elapsed_seconds)
    if has_distance:
        stats_parts = [f"{distance_m / 1000:.2f} km", duration_str]
        if elapsed_seconds > 0:
            if activity_category in ("run", "walk"):
                pace_sec = elapsed_seconds / (distance_m / 1000)
                stats_parts.append(f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d} /km")
            elif activity_category == "ride":
                speed_kmh = (distance_m / elapsed_seconds) * 3.6
                stats_parts.append(f"{speed_kmh:.1f} km/h")
        lines.append(" · ".join(stats_parts))
    else:
        lines.append(duration_str)

    # Line 3: ❤️ avg · max · 🔥 kcal · 👟 cadence (all on one line)
    stat3_parts = []
    if avg_hr and max_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg · {int(max_hr)} max")
    elif avg_hr:
        stat3_parts.append(f"❤️ {int(avg_hr)} avg")
    if calories:
        stat3_parts.append(f"🔥 {int(calories)} kcal")
    if cadence and activity_category in ("run", "walk", "ride", "swim", "other_cardio"):
        stat3_parts.append(f"👟 {int(cadence)} spm")
    if stat3_parts:
        lines.append(" · ".join(stat3_parts))

    if not saved:
        lines.append("<i>Not yet saved.</i>")

    # Per-km splits block
    strava_activity_id = activity.get("id")
    if strava_activity_id:
        splits = _fetch_splits(strava_activity_id)
        if splits:
            lines.append("")
            for s in splits:
                moving = s.get("moving_seconds") or s.get("elapsed_seconds") or 0
                pace_sec = moving / ((s.get("distance_m") or 1000) / 1000) if moving else 0
                pace_str = f"{int(pace_sec // 60)}:{int(pace_sec % 60):02d}" if pace_sec else "?:??"
                row = f"km {s['lap_index']}   {pace_str}"
                avg = s.get("average_heartrate")
                mx = s.get("max_heartrate")
                cad = s.get("average_cadence")
                zone = s.get("pace_zone")
                if avg and mx:
                    row += f"   ❤️ {int(avg)}/{int(mx)}"
                elif avg:
                    row += f"   ❤️ {int(avg)}"
                if cad:
                    row += f"   👟 {int(cad)}"
                if zone:
                    row += f"   z{zone}"
                lines.append(row)

    return "\n".join(lines)


# Fetches per-km splits for a cardio activity from exercise.cardio_splits.
# Inputs: strava_activity_id (the Strava ID, not the internal cardio_activity_id).
# Outputs: list of dicts with keys: lap_index, distance_m, moving_seconds, elapsed_seconds,
#          average_heartrate, max_heartrate, average_cadence, pace_zone
#          — ordered by lap_index. Empty list if none found.
def _fetch_splits(strava_activity_id: int) -> list[dict]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT cs.lap_index, cs.distance_m, cs.moving_seconds, cs.elapsed_seconds,
                           cs.average_heartrate, cs.max_heartrate,
                           cs.average_cadence, cs.pace_zone
                    FROM exercise.cardio_splits cs
                    JOIN exercise.cardio_activities ca USING (cardio_activity_id)
                    WHERE ca.strava_activity_id = %s
                    ORDER BY cs.lap_index
                    """,
                    (strava_activity_id,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "lap_index": r[0],
                        "distance_m": float(r[1]) if r[1] is not None else 1000.0,
                        "moving_seconds": r[2],
                        "elapsed_seconds": r[3],
                        "average_heartrate": r[4],
                        "max_heartrate": r[5],
                        "average_cadence": r[6],
                        "pace_zone": r[7],
                    }
                    for r in rows
                ]
    except Exception:
        # Splits are best-effort — don't break notifications if DB is unavailable.
        return []
    finally:
        conn.close()


# Formats a duration in seconds as mm:ss or h:mm:ss.
# Inputs: total seconds (int).
# Outputs: formatted string, e.g. "31:04" or "1:02:30".
def _format_duration(total_seconds: int) -> str:
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min"
    return f"{total_seconds} sec"
