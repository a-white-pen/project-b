"""
Sleep/wake correction handler — applies B's quoted correction to sleep/wake rows.

Functions:
  handle_sleep_wake_correction(msg, state) — updates or deletes quoted sleep/wake events
  _fetch_events(ids)                       — loads sleep/wake rows by ID
  _apply_event_type_update(...)            — changes wake to sleep or sleep to wake
  _delete_events(ids)                      — deletes sleep/wake rows by ID
  _append_correction_meta(...)             — records correction provenance in meta.corrections
  _detect_event_type(text)                 — detects corrected sleep/wake type from text
  _is_delete_request(text)                 — detects explicit delete/remove correction text
"""

import logging
import re

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


_DELETE_WORDS = ("delete", "remove", "ignore", "discard")


# Handles a quoted correction to previously logged sleep/wake rows.
# Inputs: quoted InboundMessage plus conversation_state containing sleep_wake_event_ids.
# Outputs: (reply_text, pending_state) for saving the next correction state.
def handle_sleep_wake_correction(msg: InboundMessage, state: dict) -> tuple[str, dict | None]:
    context = state.get("context") or {}
    event_ids = context.get("sleep_wake_event_ids") or []
    correction_text = msg.text or msg.caption

    if not event_ids:
        return ("Nothing to correct — couldn't find the sleep/wake row.", None)
    if not correction_text:
        return ("What should change? Say delete, sleep, or wake.", None)

    try:
        current_events = _fetch_events(event_ids)
    except Exception as e:
        log_failure(logger, logging.ERROR, "sleep_wake_correction_fetch_failed", e, update_id=msg.update_id)
        return ("Couldn't load that sleep/wake row — try again.", None)

    if not current_events:
        return ("Nothing to correct — that sleep/wake row is already gone.", None)

    if _is_delete_request(correction_text):
        try:
            deleted_count = _delete_events([e["sleep_wake_event_id"] for e in current_events])
        except Exception as e:
            log_failure(logger, logging.ERROR, "sleep_wake_correction_delete_failed", e, update_id=msg.update_id)
            return ("Couldn't delete that sleep/wake row — try again.", None)
        log_event(logger, logging.INFO, "sleep_wake_correction_deleted", update_id=msg.update_id, deleted_count=deleted_count)
        return ("Sleep/wake row deleted. Tiny clerical bonfire.", None)

    corrected_type = _detect_event_type(correction_text)
    if corrected_type is None:
        return ("I can fix that if you say delete, sleep, or wake. My tiny clipboard needs nouns.", None)

    try:
        updated_events = _apply_event_type_update(current_events, corrected_type, correction_text, msg.update_id)
    except Exception as e:
        log_failure(logger, logging.ERROR, "sleep_wake_correction_update_failed", e, update_id=msg.update_id)
        return ("Correction parsed but failed to save — try again.", None)

    surviving_ids = [e["sleep_wake_event_id"] for e in updated_events]
    label = "Wake" if corrected_type == "wake" else "Sleep"
    emoji = "🌅" if corrected_type == "wake" else "🌙"
    new_state = {
        "domain": "sleep_wake",
        "context": {
            "sleep_wake_event_ids": surviving_ids,
            "event_type": corrected_type,
        },
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (f"{emoji} {label} time updated.", new_state)


# Loads sleep/wake events by ID.
# Inputs: list of sleep_wake_event_id values from conversation_state.
# Outputs: list of row dicts ordered by event ID.
def _fetch_events(event_ids: list[int]) -> list[dict]:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT sleep_wake_event_id, event_type, occurred_at, meta
                    FROM b.sleep_wake_events
                    WHERE sleep_wake_event_id = ANY(%s)
                    ORDER BY sleep_wake_event_id
                    """,
                    (event_ids,),
                )
                rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "sleep_wake_event_id": r[0],
            "event_type": r[1],
            "occurred_at": r[2],
            "meta": r[3] or {},
        }
        for r in rows
    ]


# Updates existing sleep/wake events to the corrected event type.
# Inputs: current DB rows, corrected event type, raw correction text, and Telegram update_id.
# Outputs: updated row dicts returned by the database.
def _apply_event_type_update(
    current_events: list[dict],
    event_type: str,
    correction_text: str,
    update_id: int | None,
) -> list[dict]:
    updated_events = []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for event in current_events:
                    cur.execute(
                        """
                        UPDATE b.sleep_wake_events
                        SET event_type = %s,
                            meta = %s
                        WHERE sleep_wake_event_id = %s
                        RETURNING sleep_wake_event_id, event_type, occurred_at, meta
                        """,
                        (
                            event_type,
                            psycopg2.extras.Json(_append_correction_meta(event, event_type, correction_text, update_id)),
                            event["sleep_wake_event_id"],
                        ),
                    )
                    returned = cur.fetchone()
                    if returned:
                        updated_events.append(
                            {
                                "sleep_wake_event_id": returned[0],
                                "event_type": returned[1],
                                "occurred_at": returned[2],
                                "meta": returned[3] or {},
                            }
                        )
    finally:
        conn.close()
    log_event(
        logger,
        logging.INFO,
        "sleep_wake_correction_updated",
        update_id=update_id,
        rows_written=len(updated_events),
        event_type=event_type,
    )
    return updated_events


# Deletes sleep/wake events by ID.
# Inputs: list of sleep_wake_event_id values.
# Outputs: number of deleted rows according to cursor.rowcount.
def _delete_events(event_ids: list[int]) -> int:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM b.sleep_wake_events WHERE sleep_wake_event_id = ANY(%s)",
                    (event_ids,),
                )
                return cur.rowcount
    finally:
        conn.close()


# Adds correction provenance to a copied meta dict.
# Inputs: existing event, corrected event type, raw correction text, and Telegram update_id.
# Outputs: updated meta dict.
def _append_correction_meta(event: dict, event_type: str, correction_text: str, update_id: int | None) -> dict:
    meta = dict(event.get("meta") or {})
    corrections = meta.get("corrections")
    if not isinstance(corrections, list):
        corrections = []
    corrections.append(
        {
            "source": "telegram",
            "self_reported": True,
            "telegram_update_id": update_id,
            "text": correction_text,
            "fields": ["event_type"],
            "old_event_type": event.get("event_type"),
            "new_event_type": event_type,
        }
    )
    meta["corrections"] = corrections
    return meta


# Detects a corrected sleep/wake type from B's correction text.
# Inputs: correction text from B.
# Outputs: "sleep", "wake", or None if no clear type appears.
def _detect_event_type(text: str) -> str | None:
    lowered = text.lower()
    if re.search(r"\b(wake|woke|morning|wake up)\b", lowered):
        return "wake"
    if re.search(r"\b(sleep|slept|bed|night)\b", lowered) or "orh orh" in lowered:
        return "sleep"
    return None


# Detects explicit delete/remove correction requests.
# Inputs: correction text from B.
# Outputs: True when B clearly wants the quoted sleep/wake row removed.
def _is_delete_request(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in _DELETE_WORDS)
