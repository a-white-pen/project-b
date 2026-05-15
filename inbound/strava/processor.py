"""
Strava activity processor — fetches activity details, persists to exercise schema,
and sends proactive Telegram notifications.

Functions:
  process_activity_event(strava_inbound_id, activity_id, aspect_type) — fetches Strava activity,
      saves to exercise.cardio_activities + cardio_splits, sends Telegram notification
  _exchange_token()                                      — exchanges refresh token for access token
  _fetch_activity(access_token, activity_id)             — fetches full activity detail from Strava API
  _activity_label(sport_type, category, is_treadmill)          — maps sport_type to readable label
  _format_notification(activity, aspect_type, saved, category) — builds HTML-formatted Telegram message
  _get_latest_chat_id()                                  — reads chat_id from system.telegram_outbound
  _store_outbound(message_id, payload)                   — logs proactive outbound with telegram_update_id=NULL
"""

import html
import json
import logging
import re

import httpx

from domains.exercise.service import save_cardio_activity
from system.config import get_strava_config
from system.db import get_connection
from system.logging import log_event, log_failure
from telegram.replies import send_reply

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

        chat_id = _get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        text = _format_notification(activity, aspect_type, saved, activity_category)
        message_id, sent_payload = send_reply(chat_id, text)
        if message_id is not None:
            _store_outbound(message_id, sent_payload)
        log_event(logger, logging.INFO, "strava_notification_sent",
                  strava_inbound_id=strava_inbound_id,
                  chat_id=chat_id,
                  message_id=message_id)

    except Exception as e:
        log_failure(logger, logging.ERROR, "strava_process_failed", e,
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
    if sport_type in ("WeightTraining", "Workout"):
        return "Strength Session"
    # Convert CamelCase sport_type to readable words for everything else
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", sport_type) or "Activity"


# Builds the proactive Telegram notification text for an activity.
# Uses HTML formatting — bold label, HR pair, cadence, italic not-saved status.
# Inputs: full Strava activity dict, aspect_type, whether it was saved, and its category.
def _format_notification(activity: dict, aspect_type: str, saved: bool, activity_category: str | None) -> str:
    sport_type = activity.get("sport_type", "")
    is_treadmill = bool(activity.get("trainer"))
    elapsed_seconds = activity.get("elapsed_time") or activity.get("moving_time") or 0
    duration_str = _format_duration(elapsed_seconds)
    distance_m = activity.get("distance") or 0
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    cadence = activity.get("average_cadence")
    update_prefix = "Updated: " if aspect_type == "update" else ""

    label = _activity_label(sport_type, activity_category, is_treadmill)
    has_distance = bool(distance_m) and activity_category in ("run", "walk", "ride", "swim", "other_cardio")
    stats = f"{distance_m / 1000:.1f} km in {duration_str}" if has_distance else duration_str
    line1 = f"{update_prefix}<b>{label}</b> — {stats}"

    line2_parts = []
    if avg_hr and max_hr:
        line2_parts.append(f"❤️ {int(avg_hr)} avg · {int(max_hr)} max")
    elif avg_hr:
        line2_parts.append(f"❤️ {int(avg_hr)} avg")
    if cadence and activity_category in ("run", "walk", "ride", "swim", "other_cardio"):
        line2_parts.append(f"👟 {int(cadence)} spm")

    lines = [line1]
    if line2_parts:
        lines.append("  |  ".join(line2_parts))
    if not saved:
        lines.append("<i>Not yet saved.</i>")

    return "\n".join(lines)


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


# Reads the most recent chat_id from system.telegram_outbound.
# Used to find B's chat_id for proactive (unprompted) messages.
# Inputs: none.
# Outputs: chat_id as int, or None if system.telegram_outbound has no usable rows.
def _get_latest_chat_id() -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (payload->>'chat_id')::bigint
                    FROM system.telegram_outbound
                    WHERE payload->>'chat_id' IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Inserts a row into system.telegram_outbound for a proactive (unprompted) message.
# telegram_update_id is NULL because there is no inbound update that triggered this message.
# Inputs: Telegram message_id from the API response, full sent payload dict.
def _store_outbound(message_id: int, payload: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_outbound"
                    " (message_id, telegram_update_id, payload)"
                    " VALUES (%s, NULL, %s)",
                    (message_id, json.dumps(payload)),
                )
    finally:
        conn.close()


