"""
Weight logging domain — handles log_weight intent.

Functions:
  handle_weight_log(msg) — extracts weight from message text, inserts into b.weight_measurements,
                           returns a formatted confirmation
  _extract_weight_kg(text) — parses the first numeric value from text, returns float or None
"""

import logging
import re
from datetime import datetime, timezone

import psycopg2.extras

from system.db import get_connection
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

_WEIGHT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")

_MIN_KG = 45.0
_MAX_KG = 99.0


# Extracts the first numeric value from text and validates it as a plausible weight.
# Returns float if valid, None if no number found or value is out of range.
def _extract_weight_kg(text: str) -> float | None:
    match = _WEIGHT_RE.search(text)
    if not match:
        return None
    val = float(match.group(1))
    if not (_MIN_KG < val < _MAX_KG):
        return None
    return val


# Handles a weight logging request from B.
# Inputs: InboundMessage with text containing a weight value in kg.
# Outputs: (reply string, None). No pending_state — no correction flow for weight.
def handle_weight_log(msg: InboundMessage) -> tuple[str, None]:
    if not msg.text:
        return ("What weight would you like to log? Send a number in kg, e.g. 57.1", None)

    weight_kg = _extract_weight_kg(msg.text)
    logger.info("update_id=%s weight extracted=%.1f from text=%r", msg.update_id, weight_kg or 0, msg.text)
    if weight_kg is None:
        return ("Couldn't find a valid weight in your message. Send a number in kg, e.g. 57.1", None)

    measured_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
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
                        INSERT INTO b.weight_measurements (measured_at, weight_kg, meta)
                        VALUES (%s, %s, %s)
                        """,
                        (measured_at, weight_kg, psycopg2.extras.Json(meta)),
                    )
        finally:
            conn.close()
    except Exception as e:
        logger.error("weight insert failed update_id=%s: %s", msg.update_id, e)
        return ("Couldn't save your weight — please try again.", None)

    logger.info("update_id=%s weight_kg=%.1f inserted measured_at=%s", msg.update_id, weight_kg, measured_at)
    return (f"⚖️ {weight_kg:.1f} kg logged.", None)
