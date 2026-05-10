"""
Sleep/wake logging domain — handles log_sleep and log_wake intents.

Functions:
  handle_sleep_log(msg) — records a sleep event in b.sleep_wake_events
  handle_wake_log(msg)  — records a wake event in b.sleep_wake_events
"""

import logging
from datetime import datetime, timezone

import psycopg2.extras

from system.db import get_connection
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Inserts a sleep or wake event into b.sleep_wake_events.
# Inputs: InboundMessage, event_type ("sleep" or "wake").
# Outputs: (reply string, None). No pending_state needed.
def _log_event(msg: InboundMessage, event_type: str) -> tuple[str, None]:
    occurred_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    meta = {
        "source": "telegram",
        "self_reported": True,
        "telegram_update_id": msg.update_id,
    }

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO b.sleep_wake_events (event_type, occurred_at, meta)
                        VALUES (%s, %s, %s)
                        """,
                        (event_type, occurred_at, psycopg2.extras.Json(meta)),
                    )
        finally:
            conn.close()
    except Exception as e:
        logger.error("sleep_wake insert failed event_type=%s update_id=%s: %s", event_type, msg.update_id, e)
        return ("Couldn't save that — please try again.", None)

    logger.info("update_id=%s sleep_wake event_type=%s occurred_at=%s inserted", msg.update_id, event_type, occurred_at)
    if event_type == "wake":
        return ("🌅 Wake time logged.", None)
    return ("🌙 Sleep time logged.", None)


# Handles a wake logging request from B.
def handle_wake_log(msg: InboundMessage) -> tuple[str, None]:
    return _log_event(msg, "wake")


# Handles a sleep logging request from B.
def handle_sleep_log(msg: InboundMessage) -> tuple[str, None]:
    return _log_event(msg, "sleep")
