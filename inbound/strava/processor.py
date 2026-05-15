"""
Strava activity processor — fetches activity details and sends proactive Telegram notifications.

Functions:
  process_activity_event(strava_inbound_id, activity_id, aspect_type) — fetches Strava activity,
      sends Telegram notification, logs to system.telegram_outbound
  _exchange_token()                          — exchanges refresh token for a short-lived access token
  _fetch_activity(access_token, activity_id) — fetches full activity detail from Strava API
  _format_notification(activity, aspect_type) — builds the proactive Telegram message text
  _get_latest_chat_id()                      — reads the most recent chat_id from system.telegram_outbound
  _store_outbound(message_id, payload)       — inserts a proactive outbound row with telegram_update_id=NULL
"""

import html
import json
import logging

import httpx

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

        chat_id = _get_latest_chat_id()
        if chat_id is None:
            log_event(logger, logging.WARNING, "strava_no_chat_id",
                      strava_inbound_id=strava_inbound_id)
            return

        text = _format_notification(activity, aspect_type)
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


# Builds the proactive Telegram notification text for an activity.
# Inputs: full Strava activity dict, aspect_type ("create" or "update").
# Outputs: two-line string — line 1: activity name + duration; line 2: status.
def _format_notification(activity: dict, aspect_type: str) -> str:
    name = html.escape(activity.get("name") or "Activity")
    elapsed_seconds = activity.get("elapsed_time") or activity.get("moving_time") or 0
    duration_str = _format_duration(elapsed_seconds)

    status = "Updated on Strava. Not yet saved." if aspect_type == "update" else "Strava received. Not yet saved."
    return f"{name}: {duration_str}\n{status}"


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


