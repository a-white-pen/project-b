"""
Weight correction handler — applies B's quoted correction to weight rows.

Functions:
  handle_weight_correction(msg, state) — updates or deletes quoted weight measurements
  _fetch_measurements(ids)             — loads weight rows by ID
  _apply_weight_update(...)            — updates one or more weight rows
  _delete_measurements(ids)            — deletes weight rows by ID
  _append_correction_meta(...)         — records correction provenance in meta.corrections
  _is_delete_request(text)             — detects explicit delete/remove correction text
"""

import logging

import psycopg2.extras

from domains.weight.service import _extract_weight_kg, format_weight_kg
from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


_DELETE_WORDS = ("delete", "remove", "ignore", "discard")


# Handles a quoted correction to previously logged weight rows.
# Inputs: quoted InboundMessage plus conversation_state containing weight_measurement_ids.
# Outputs: (reply_text, pending_state) for saving the next correction state.
def handle_weight_correction(msg: InboundMessage, state: dict) -> tuple[str, dict | None]:
    context = state.get("context") or {}
    measurement_ids = context.get("weight_measurement_ids") or []
    correction_text = msg.text or msg.caption

    if not measurement_ids:
        return ("Nothing to correct — couldn't find the weight row.", None)
    if not correction_text:
        return ("What should the weight be? Send the corrected kg number.", None)

    try:
        current_rows = _fetch_measurements(measurement_ids)
    except Exception as e:
        log_failure(logger, logging.ERROR, "weight_correction_fetch_failed", e, update_id=msg.update_id)
        return ("Couldn't load that weight row — try again.", None)

    if not current_rows:
        return ("Nothing to correct — that weight row is already gone.", None)

    if _is_delete_request(correction_text):
        try:
            deleted_count = _delete_measurements([r["weight_measurement_id"] for r in current_rows])
        except Exception as e:
            log_failure(logger, logging.ERROR, "weight_correction_delete_failed", e, update_id=msg.update_id)
            return ("Couldn't delete that weight row — try again.", None)
        log_event(logger, logging.INFO, "weight_correction_deleted", update_id=msg.update_id, deleted_count=deleted_count)
        return ("⚖️ Weight removed.", None)

    weight_kg = _extract_weight_kg(correction_text)
    if weight_kg is None:
        return ("Couldn't find a valid corrected weight. Send a number in kg, e.g. 56.45", None)

    try:
        updated_rows = _apply_weight_update(current_rows, weight_kg, correction_text, msg.update_id)
    except Exception as e:
        log_failure(logger, logging.ERROR, "weight_correction_update_failed", e, update_id=msg.update_id)
        return ("Correction parsed but failed to save — try again.", None)

    surviving_ids = [r["weight_measurement_id"] for r in updated_rows]
    reply = f"⚖️ {format_weight_kg(weight_kg)} kg logged."
    new_state = {
        "domain": "weight",
        "context": {"weight_measurement_ids": surviving_ids},
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (reply, new_state)


# Loads weight measurements by ID.
# Inputs: list of weight_measurement_id values from conversation_state.
# Outputs: list of row dicts ordered by measurement ID.
def _fetch_measurements(measurement_ids: list[int]) -> list[dict]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT weight_measurement_id, measured_at, weight_kg, meta
                    FROM b.weight_measurements
                    WHERE weight_measurement_id = ANY(%s)
                    ORDER BY weight_measurement_id
                    """,
                    (measurement_ids,),
                )
                rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "weight_measurement_id": r[0],
            "measured_at": r[1],
            "weight_kg": float(r[2]),
            "meta": r[3] or {},
        }
        for r in rows
    ]


# Updates existing weight measurements to the corrected kg value.
# Inputs: current DB rows, corrected kg value, raw correction text, and Telegram update_id.
# Outputs: updated row dicts returned by the database.
def _apply_weight_update(
    current_rows: list[dict],
    weight_kg: float,
    correction_text: str,
    update_id: int | None,
) -> list[dict]:
    updated_rows = []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for row in current_rows:
                    cur.execute(
                        """
                        UPDATE b.weight_measurements
                        SET weight_kg = %s,
                            meta = %s
                        WHERE weight_measurement_id = %s
                        RETURNING weight_measurement_id, measured_at, weight_kg, meta
                        """,
                        (
                            weight_kg,
                            psycopg2.extras.Json(_append_correction_meta(row, weight_kg, correction_text, update_id)),
                            row["weight_measurement_id"],
                        ),
                    )
                    returned = cur.fetchone()
                    if returned:
                        updated_rows.append(
                            {
                                "weight_measurement_id": returned[0],
                                "measured_at": returned[1],
                                "weight_kg": float(returned[2]),
                                "meta": returned[3] or {},
                            }
                        )
    finally:
        conn.close()
    log_event(
        logger,
        logging.INFO,
        "weight_correction_updated",
        update_id=update_id,
        rows_written=len(updated_rows),
        weight_kg=weight_kg,
    )
    return updated_rows


# Deletes weight measurements by ID.
# Inputs: list of weight_measurement_id values.
# Outputs: number of deleted rows according to cursor.rowcount.
def _delete_measurements(measurement_ids: list[int]) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM b.weight_measurements WHERE weight_measurement_id = ANY(%s)",
                    (measurement_ids,),
                )
                return cur.rowcount
    finally:
        conn.close()


# Adds correction provenance to a copied meta dict.
# Inputs: existing row, corrected kg value, raw correction text, and Telegram update_id.
# Outputs: updated meta dict.
def _append_correction_meta(row: dict, weight_kg: float, correction_text: str, update_id: int | None) -> dict:
    meta = dict(row.get("meta") or {})
    corrections = meta.get("corrections")
    if not isinstance(corrections, list):
        corrections = []
    corrections.append(
        {
            "source": "telegram",
            "self_reported": True,
            "telegram_update_id": update_id,
            "text": correction_text,
            "fields": ["weight_kg"],
            "old_weight_kg": row.get("weight_kg"),
            "new_weight_kg": weight_kg,
        }
    )
    meta["corrections"] = corrections
    return meta


# Detects explicit delete/remove correction requests.
# Inputs: correction text from B.
# Outputs: True when B clearly wants the quoted weight row removed.
def _is_delete_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _DELETE_WORDS)
