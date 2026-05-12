"""
Weight logging domain — handles log_weight intent.

Functions:
  handle_weight_log(msg) — extracts weight from message text, inserts into b.weight_measurements,
                           returns a formatted confirmation
  _extract_weight_kg(text) — parses the first numeric value from text, returns float or None
  format_weight_kg(weight_kg) — formats kg values with useful decimal precision
"""

import logging
import re
from datetime import datetime, timezone

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

_WEIGHT_RE = re.compile(r"\b(\d+(?:\.\d+)?)(?:\s*(?:kg|kgs|kilograms?)\b)?(?=\W|$)")

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


# Formats a weight value without hiding useful precision.
# Inputs: numeric kg value from parsing or DB.
# Outputs: one or two decimal places, keeping at least one decimal.
def format_weight_kg(weight_kg: float) -> str:
    formatted = f"{weight_kg:.2f}".rstrip("0").rstrip(".")
    if "." not in formatted:
        formatted += ".0"
    return formatted


# Handles a weight logging request from B.
# Inputs: InboundMessage with text containing a weight value in kg.
# Outputs: (reply string, pending_state dict | None). pending_state enables quoted corrections.
def handle_weight_log(msg: InboundMessage) -> tuple[str, dict | None]:
    if not msg.text:
        log_event(logger, logging.WARNING, "weight_log_missing_text", update_id=msg.update_id)
        return ("What weight would you like to log? Send a number in kg, e.g. 57.1", None)

    weight_kg = _extract_weight_kg(msg.text)
    log_event(
        logger,
        logging.INFO,
        "weight_log_parsed",
        update_id=msg.update_id,
        parsed_weight_kg=weight_kg,
        text_chars=len(msg.text),
    )
    if weight_kg is None:
        log_event(
            logger,
            logging.WARNING,
            "weight_log_invalid_value",
            update_id=msg.update_id,
            text_chars=len(msg.text),
        )
        return ("Couldn't find a valid weight in your message. Send a number in kg, e.g. 57.1", None)

    measured_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    meta = {
        "source": "telegram",
        "self_reported": True,
        "telegram_update_id": msg.update_id,
    }

    weight_measurement_id: int | None = None
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO b.weight_measurements (measured_at, weight_kg, meta)
                        VALUES (%s, %s, %s)
                        RETURNING weight_measurement_id
                        """,
                        (measured_at, weight_kg, psycopg2.extras.Json(meta)),
                    )
                    row = cur.fetchone()
                    weight_measurement_id = row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "weight_insert_failed", e, update_id=msg.update_id)
        return ("Couldn't save your weight — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "weight_inserted",
        update_id=msg.update_id,
        weight_kg=weight_kg,
        measured_at=measured_at.isoformat(),
        weight_measurement_id=weight_measurement_id,
    )
    state = {
        "domain": "weight",
        "context": {"weight_measurement_ids": [weight_measurement_id]},
    }
    return (f"⚖️ {format_weight_kg(weight_kg)} kg logged.", state)
