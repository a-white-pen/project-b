"""
Sleep/wake correction handler — applies B's quoted correction to sleep/wake rows.

Functions:
  handle_sleep_wake_correction(msg, state) — updates or deletes quoted sleep/wake events
  _fetch_events(ids)                       — loads sleep/wake rows by ID
  _apply_event_type_update(...)            — changes wake to sleep or sleep to wake
  _apply_occurred_at_update(...)           — changes occurred_at on quoted rows (time fix)
  _delete_events(ids)                      — deletes sleep/wake rows by ID
  _append_correction_meta(...)             — records correction provenance in meta.corrections
  _detect_event_type(text)                 — detects corrected sleep/wake type from text
  _parse_time_correction(text, msg_ts, anchor_ts, tz) — parses "7am", "6:30", "8:30 pm" into UTC datetime
  _is_delete_request(text)                 — detects explicit delete/remove correction text
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage
from system.timezone import get_timezone

logger = logging.getLogger(__name__)


_DELETE_WORDS = ("delete", "remove", "ignore", "discard")

# Matches "7am", "7 am", "7:30am", "7:30 am", "07:30", "19:45", "8.30am" etc.
# Group 1: hour, Group 2: optional minute, Group 3: optional am/pm.
# _parse_time_correction requires at least ONE of group 2 (minute) or group 3
# (meridiem) to accept — bare numbers are too ambiguous (7 → 07:00 or 19:00?).
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b",
    re.IGNORECASE,
)


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

    # Time correction takes precedence over type swap — if B writes "7am", treat
    # that as a time edit on the existing row(s) rather than trying to interpret
    # "am" as an event-type token. Anchor "today" to the most recently quoted
    # event's occurred_at so a wake at 11pm last night quoted at 8am today is
    # corrected on the right date.
    msg_ts = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    anchor_ts = current_events[-1]["occurred_at"]
    tz = get_timezone(anchor_ts)
    new_occurred_at_utc = _parse_time_correction(correction_text, msg_ts, anchor_ts, tz)
    if new_occurred_at_utc is not None:
        try:
            updated_events = _apply_occurred_at_update(
                current_events, new_occurred_at_utc, correction_text, msg.update_id
            )
        except Exception as e:
            log_failure(logger, logging.ERROR, "sleep_wake_correction_time_update_failed", e, update_id=msg.update_id)
            return ("Correction parsed but failed to save — try again.", None)
        surviving_ids = [e["sleep_wake_event_id"] for e in updated_events]
        kind = updated_events[0]["event_type"] if updated_events else "wake"
        label = "Wake" if kind == "wake" else "Sleep"
        emoji = "🌅" if kind == "wake" else "🌙"
        new_local = new_occurred_at_utc.astimezone(tz)
        hour_12 = new_local.hour % 12 or 12
        meridiem = "AM" if new_local.hour < 12 else "PM"
        time_str = f"{hour_12}:{new_local.minute:02d} {meridiem}"
        new_state = {
            "domain": "sleep_wake",
            "context": {
                "sleep_wake_event_ids": surviving_ids,
                "event_type": kind,
            },
            "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
        }
        return (f"{emoji} {label} time updated to {time_str}.", new_state)

    corrected_type = _detect_event_type(correction_text)
    if corrected_type is None:
        return ("I can fix that if you say delete, sleep, wake, or a time like '7am'.", None)

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


# Updates occurred_at on each quoted sleep/wake row to new_occurred_at_utc. Used
# when B quote-replies an auto-inferred-wake reminder with a corrected time
# (e.g. "7am" → wake event updated to 7am local). Returns the freshly-read rows.
def _apply_occurred_at_update(
    current_events: list[dict],
    new_occurred_at_utc: datetime,
    correction_text: str,
    update_id: int | None,
) -> list[dict]:
    updated_events = []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for event in current_events:
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
                            "fields": ["occurred_at"],
                            "old_occurred_at": event["occurred_at"].isoformat(),
                            "new_occurred_at": new_occurred_at_utc.isoformat(),
                        }
                    )
                    meta["corrections"] = corrections
                    # If this row was an auto-inferred placeholder, the time edit
                    # is B confirming/correcting it — flip the flag so analytics
                    # treat it as user-confirmed going forward.
                    if meta.get("auto_inferred"):
                        meta["auto_inferred"] = False
                        meta["self_reported"] = True
                    cur.execute(
                        """
                        UPDATE b.sleep_wake_events
                        SET occurred_at = %s,
                            meta = %s
                        WHERE sleep_wake_event_id = %s
                        RETURNING sleep_wake_event_id, event_type, occurred_at, meta
                        """,
                        (new_occurred_at_utc, psycopg2.extras.Json(meta), event["sleep_wake_event_id"]),
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
        "sleep_wake_correction_time_updated",
        update_id=update_id,
        rows_written=len(updated_events),
        new_occurred_at=new_occurred_at_utc.isoformat(),
    )
    return updated_events


# Parses a time-of-day from B's correction text (`text`) and returns a tz-aware
# UTC datetime anchored to the same local date as `anchor_ts` (the row being
# corrected). `msg_ts` is when B sent the correction — used to roll back one day
# if the parsed time would land in the future. `tz` is B's current timezone.
# Returns None when no unambiguous time can be extracted (caller falls through
# to other correction modes).
#
# A match is accepted ONLY when it carries an unambiguous time signal:
#   1. an explicit minute component ("6:30", "07.15", "13:45") — interpreted as
#      24h when no meridiem is given, 12h when am/pm is given, OR
#   2. an explicit meridiem ("7am", "8 pm").
#
# Bare 1-2 digit numbers with NO minute and NO meridiem ("7", "19", "10 minutes
# earlier") are rejected. Writing 07:00 when B meant 19:00 (or vice versa) is
# worse than asking B to type "7pm" or "7:00" — silent wrong-AM/PM corruption
# is the failure mode this guard prevents.
#
# Scans all regex matches in order — the first acceptable one wins. Stopping on
# the first match would fail on "May 24 7am": "24" has no meridiem (rejected on
# hour-range too, but conceptually it's also bare-number-rejected); the loop
# advances to "7am" which has a meridiem and wins.
#
# Examples (assume tz=Asia/Bangkok, anchor_ts=2026-05-25 02:15 UTC = 09:15 local):
#   "7am"               → 2026-05-25 00:00 UTC (07:00 local same day)
#   "6:30"              → 2026-05-24 23:30 UTC (06:30 local same day)
#   "8:30 pm"           → 2026-05-24 13:30 UTC (20:30 local prev day, future-rolled)
#   "13:45"             → 2026-05-24 06:45 UTC (13:45 local prev day, future-rolled)
#   "May 24 7am"        → 2026-05-25 00:00 UTC (07:00 local, "24" skipped, "7am" wins)
#   "7"                 → None (bare number — ambiguous, reject)
#   "10 minutes earlier"→ None (bare number "10")
def _parse_time_correction(
    text: str,
    msg_ts: datetime,
    anchor_ts: datetime,
    tz: ZoneInfo,
) -> datetime | None:
    # Reject reasons use category labels only — no quoted substrings of the
    # original text — so freeform user content does not leak into Cloud Logging.
    reject_reasons: list[str] = []
    for match in _TIME_RE.finditer(text):
        hour_raw = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        meridiem = (match.group(3) or "").lower()
        has_minute = match.group(2) is not None
        has_meridiem = bool(meridiem)

        # Validate raw numbers BEFORE applying meridiem adjustments.
        if hour_raw < 0 or hour_raw > 23 or minute < 0 or minute > 59:
            reject_reasons.append("out_of_range")
            continue
        if has_meridiem and hour_raw > 12:
            reject_reasons.append("meridiem_with_24h")
            continue

        # Reject bare 1-2 digit numbers (no minute AND no meridiem). Silent
        # wrong-AM/PM writes are worse than asking B to disambiguate.
        if not has_minute and not has_meridiem:
            reject_reasons.append("bare_number")
            continue

        hour = hour_raw
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

        anchor_local = anchor_ts.astimezone(tz)
        candidate_local = anchor_local.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        candidate_utc = candidate_local.astimezone(timezone.utc)
        rolled_back = False
        # If candidate is in the future relative to when B sent the correction,
        # roll back one day — B is almost certainly correcting an earlier event.
        if candidate_utc > msg_ts:
            candidate_utc -= timedelta(days=1)
            rolled_back = True
        log_event(
            logger,
            logging.INFO,
            "sleep_wake_time_parse_succeeded",
            parsed_local=candidate_utc.astimezone(tz).isoformat(),
            parsed_utc=candidate_utc.isoformat(),
            rolled_back_one_day=rolled_back,
            had_meridiem=has_meridiem,
            had_minute=has_minute,
        )
        return candidate_utc

    # No raw text logged — only redacted shape signals.
    log_event(
        logger,
        logging.INFO,
        "sleep_wake_time_parse_failed",
        text_chars=len(text),
        had_time_token=bool(_TIME_RE.search(text)),
        reject_reasons=reject_reasons or ["no_match"],
    )
    return None


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
