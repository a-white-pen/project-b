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
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Inserts a sleep or wake event into b.sleep_wake_events.
# Inputs: InboundMessage, event_type ("sleep" or "wake").
# Outputs: (reply string, None). No pending_state needed.
def _insert_sleep_event(msg: InboundMessage, event_type: str) -> tuple[str, None]:
    occurred_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    meta = {
        "source": "telegram",
        "self_reported": True,
        "telegram_update_id": msg.update_id,
    }
    log_event(
        logger,
        logging.INFO,
        "sleep_wake_log_started",
        update_id=msg.update_id,
        event_type=event_type,
        occurred_at=occurred_at.isoformat(),
    )

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
        log_failure(
            logger,
            logging.ERROR,
            "sleep_wake_insert_failed",
            e,
            event_type=event_type,
            update_id=msg.update_id,
        )
        return ("Couldn't save that — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "sleep_wake_inserted",
        update_id=msg.update_id,
        event_type=event_type,
        occurred_at=occurred_at.isoformat(),
    )
    if event_type == "wake":
        return ("🌅 Wake time logged.", None)
    return ("🌙 Sleep time logged.", None)


# Handles a wake logging request from B.
def handle_wake_log(msg: InboundMessage) -> tuple[str, None]:
    return _insert_sleep_event(msg, "wake")


# Handles a sleep logging request from B.
def handle_sleep_log(msg: InboundMessage) -> tuple[str, None]:
    return _insert_sleep_event(msg, "sleep")
