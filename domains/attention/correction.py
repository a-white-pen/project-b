"""
Attention correction handler — applies B's quoted correction to attention session rows.

Functions:
  handle_attention_correction(msg, state) — parses and applies a quoted attention correction
  _fetch_sessions(ids)                    — loads attention session rows by ID
  _format_sessions_for_llm(sessions)      — formats session rows for the correction prompt
  _apply_corrections(...)                 — updates or deletes attention session rows; returns (surviving_ids, rows_written)
  _build_update_payload(...)              — converts one parsed correction into DB updates
  _validate_interval(...)                 — validates and logs rejected time intervals
  _parse_optional_datetime(value)         — parses optional ISO timestamp values from the LLM
  _format_correction_reply(...)           — formats the correction acknowledgement
  _format_session_fields(session)         — formats labelled reply fields for one session
  _append_correction_meta(...)            — records correction provenance in meta.corrections
  _clean_nullable_text(value)             — normalizes nullable correction text fields
"""

import json
import logging
from datetime import datetime, timezone as dt_timezone

import psycopg2.extras

from domains.attention.service import (
    _VALID_CATEGORIES,
    _format_field,
    _format_duration,
    _get_timezone,
    _parse_json,
)
from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Raised by _apply_corrections when a proposed time interval is invalid (ended_at <= started_at).
# Caught specifically by handle_attention_correction to return a clear user-facing message.
class _CorrectionValidationError(Exception):
    pass

_CORRECTION_PROMPT = """\
You are correcting attention session rows for B's personal attention tracker.

Current local time: {current_local_time}

Previously logged sessions:
{logged_sessions}

Correction from B: {correction_text}

Determine exactly what B wants to change. B may want to:
- change category, description, project, notes
- adjust started_at or ended_at
- reopen a session by clearing ended_at
- delete a session, but only if B explicitly says delete/remove/ignore that log

Return a JSON object with this exact structure:
{{
  "sessions": [
    {{
      "attention_session_id": <int>,
      "action": "<update or delete>",
      "category": "<optional category>",
      "description": "<optional corrected description>",
      "project": "<optional corrected project, or null only if B explicitly removes it>",
      "started_at": "<optional ISO 8601 timestamp with timezone>",
      "ended_at": "<optional ISO 8601 timestamp with timezone, or null only if B explicitly says it is still open>",
      "notes": "<optional corrected note>"
    }}
  ]
}}

Rules:
- Only include fields B actually changed. Omit unchanged fields entirely.
- Valid categories: deep_work, shallow_work, planning, learning, exercise, cooking, eating, commute, life_admin, personal_care, social, entertainment, rest, meditation, other.
- Use update unless B explicitly asks to delete/remove/ignore the log.
- If B gives a relative time like "10 minutes earlier", calculate the corrected ISO timestamp from the logged session times.
- Preserve Chinese characters if B used them.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles a correction to previously logged attention session rows.
# Inputs: quoted InboundMessage plus conversation_state containing attention_session_ids.
# Outputs: (reply_text, pending_state) for saving the next correction state.
def handle_attention_correction(msg: InboundMessage, state: dict) -> tuple[str, dict | None]:
    context = state.get("context") or {}
    session_ids = context.get("attention_session_ids") or []
    correction_text = msg.text or msg.caption

    if not session_ids:
        return ("Nothing to correct — couldn't find the attention session IDs.", None)
    if not correction_text:
        return ("What should change? Send the correction in text or voice.", None)

    try:
        current_sessions = _fetch_sessions(session_ids)
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_correction_fetch_failed", e, update_id=msg.update_id)
        return ("Couldn't load the attention session — try again.", None)

    if not current_sessions:
        return ("Nothing to correct — those attention sessions are already gone.", None)

    try:
        # Resolve current local time using B's location — gives the LLM a concrete "now"
        # so relative corrections ("just finished", "10 min ago") produce correct timestamps.
        now_utc = datetime.now(tz=dt_timezone.utc)
        tz = _get_timezone(now_utc)
        current_local_time = now_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")
        raw = generate_text(
            _CORRECTION_PROMPT.format(
                current_local_time=current_local_time,
                logged_sessions=_format_sessions_for_llm(current_sessions),
                correction_text=correction_text,
            ),
            model=MODEL_FLASH,
        )
        parsed = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_correction_parse_failed", e, update_id=msg.update_id)
        return ("Couldn't understand the correction — say it more directly?", None)

    parsed_sessions = parsed.get("sessions") or []
    if not parsed_sessions:
        return ("I couldn't see a concrete change there. Annoying, but honest.", None)

    try:
        surviving_ids, rows_written = _apply_corrections(
            current_sessions=current_sessions,
            parsed_sessions=parsed_sessions,
            correction_text=correction_text,
            update_id=msg.update_id,
        )
    except _CorrectionValidationError as e:
        return (str(e), None)
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_correction_apply_failed", e, update_id=msg.update_id)
        return ("Correction parsed but failed to save — try again.", None)

    deleted_count = len(current_sessions) - len(surviving_ids)
    if rows_written == 0 and deleted_count == 0:
        # LLM produced output but nothing was actually written — invalid fields, wrong IDs, etc.
        return ("I couldn't find a concrete change to apply. Try describing it more directly.", None)

    try:
        updated_sessions = _fetch_sessions(surviving_ids) if surviving_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "attention_correction_refetch_failed", e, update_id=msg.update_id)
        updated_sessions = []

    reply = _format_correction_reply(updated_sessions, deleted_count)
    new_state = {
        "domain": "attention",
        "context": {"attention_session_ids": surviving_ids},
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (reply, new_state)


# Loads attention sessions by ID.
# Inputs: list of attention_session_id values from conversation_state.
# Outputs: list of session dicts ordered by start time.
def _fetch_sessions(session_ids: list[int]) -> list[dict]:
    if not session_ids:
        return []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT attention_session_id, category, description, project,
                           started_at, ended_at, notes, meta
                    FROM b.attention_sessions
                    WHERE attention_session_id = ANY(%s)
                    ORDER BY started_at, attention_session_id
                    """,
                    (session_ids,),
                )
                rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "attention_session_id": r[0],
            "category": r[1],
            "description": r[2],
            "project": r[3],
            "started_at": r[4],
            "ended_at": r[5],
            "notes": r[6],
            "meta": r[7] or {},
        }
        for r in rows
    ]


# Formats current attention sessions as readable context for the correction LLM.
# Inputs: session dicts loaded from b.attention_sessions.
# Outputs: one text block for the prompt.
def _format_sessions_for_llm(sessions: list[dict]) -> str:
    lines = []
    for session in sessions:
        tz = _get_timezone(session["started_at"])
        started_local = session["started_at"].astimezone(tz)
        ended_at = session.get("ended_at")
        ended_text = "open"
        if ended_at is not None:
            ended_text = f"{ended_at.isoformat()} / local {ended_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"
        parts = [
            f"[id={session['attention_session_id']}]",
            f"description={session['description']}",
            f"category={session['category']}",
            f"project={session.get('project') or 'null'}",
            f"started_at={session['started_at'].isoformat()} / local {started_local.strftime('%Y-%m-%d %H:%M')}",
            f"ended_at={ended_text}",
        ]
        if session.get("notes"):
            parts.append(f"notes={session['notes']}")
        lines.append("; ".join(parts))
    return "\n".join(lines)


# Applies parsed attention corrections in one transaction.
# Validates time intervals in app code before writing — raises _CorrectionValidationError
# on invalid intervals so the caller can return a clear user message rather than a DB error.
# Inputs: current DB rows, parsed LLM sessions, original correction text, Telegram update_id.
# Outputs: (surviving_ids, rows_written) — rows_written is 0 when no fields were actually changed.
def _apply_corrections(
    current_sessions: list[dict],
    parsed_sessions: list[dict],
    correction_text: str,
    update_id: int | None,
) -> tuple[list[int], int]:
    sessions_by_id = {s["attention_session_id"]: s for s in current_sessions}
    allowed_ids = set(sessions_by_id)
    deleted_ids: set[int] = set()
    rows_written = 0

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for parsed in parsed_sessions:
                    session_id = parsed.get("attention_session_id")
                    if session_id not in allowed_ids:
                        continue
                    action = parsed.get("action", "update")
                    if action == "delete":
                        cur.execute(
                            "DELETE FROM b.attention_sessions WHERE attention_session_id = %s",
                            (session_id,),
                        )
                        deleted_ids.add(session_id)
                        continue

                    updates = _build_update_payload(
                        session=sessions_by_id[session_id],
                        parsed=parsed,
                        correction_text=correction_text,
                        update_id=update_id,
                    )
                    if not updates:
                        continue

                    # Validate the resulting time interval before writing.
                    # Raises _CorrectionValidationError so the caller can give a clear reply.
                    _validate_interval(updates, sessions_by_id[session_id], session_id, update_id)

                    # If this correction reopens a session (sets ended_at = None), check that
                    # no OTHER session is already open. Allowing two open sessions would break
                    # the one-open invariant and corrupt subsequent start/finish operations.
                    if "ended_at" in updates and updates["ended_at"] is None:
                        cur.execute(
                            """
                            SELECT 1 FROM b.attention_sessions
                            WHERE ended_at IS NULL
                              AND attention_session_id != %s
                            LIMIT 1
                            """,
                            (session_id,),
                        )
                        if cur.fetchone() is not None:
                            raise _CorrectionValidationError(
                                "Can't reopen that session — another attention session is already open. "
                                "Finish the open session first, then reopen this one."
                            )

                    set_clause = ", ".join(f"{col} = %s" for col in updates)
                    values = list(updates.values()) + [session_id]
                    cur.execute(
                        f"UPDATE b.attention_sessions SET {set_clause}, updated_at = now() "
                        "WHERE attention_session_id = %s",
                        values,
                    )
                    rows_written += cur.rowcount
    finally:
        conn.close()

    surviving = [sid for sid in allowed_ids if sid not in deleted_ids]
    log_event(
        logger,
        logging.INFO,
        "attention_correction_applied",
        update_id=update_id,
        rows_written=rows_written,
        deleted_count=len(deleted_ids),
        surviving_count=len(surviving),
        surviving_ids=sorted(surviving),
    )
    return sorted(surviving), rows_written


# Validates that the proposed started_at/ended_at interval for a session is coherent.
# Uses the update payload for any fields being changed, falls back to the existing session values.
# Logs and raises _CorrectionValidationError with a user-readable message if the interval is invalid.
def _validate_interval(updates: dict, session: dict, session_id: int, update_id: int | None) -> None:
    proposed_started = updates.get("started_at") or session["started_at"]
    # ended_at may be explicitly set to None (reopen), kept from session, or changed.
    if "ended_at" in updates:
        proposed_ended = updates["ended_at"]
    else:
        proposed_ended = session.get("ended_at")

    if proposed_ended is None:
        return  # Open session — no interval to validate.
    if proposed_ended <= proposed_started:
        log_event(
            logger,
            logging.WARNING,
            "attention_correction_invalid_interval",
            update_id=update_id,
            session_id=session_id,
            proposed_started_at=proposed_started.isoformat(),
            proposed_ended_at=proposed_ended.isoformat(),
        )
        raise _CorrectionValidationError(
            f"The end time would be before or equal to the start time for session {session_id}. "
            "Double-check the times and try again."
        )


# Builds a safe update payload from one parsed correction object.
# Inputs: existing session, parsed correction, correction text, and Telegram update_id.
# Outputs: dict of column updates.
def _build_update_payload(
    session: dict,
    parsed: dict,
    correction_text: str,
    update_id: int | None,
) -> dict:
    updates: dict[str, object] = {}

    if "category" in parsed and parsed["category"] in _VALID_CATEGORIES:
        updates["category"] = parsed["category"]
    if "description" in parsed and parsed["description"]:
        updates["description"] = str(parsed["description"]).strip()
    if "project" in parsed:
        updates["project"] = _clean_nullable_text(parsed["project"])
    if "notes" in parsed:
        updates["notes"] = _clean_nullable_text(parsed["notes"])
    if "started_at" in parsed:
        updates["started_at"] = _parse_optional_datetime(parsed["started_at"])
    if "ended_at" in parsed:
        updates["ended_at"] = _parse_optional_datetime(parsed["ended_at"])

    if updates:
        updates["meta"] = psycopg2.extras.Json(
            _append_correction_meta(
                session=session,
                updates=updates,
                correction_text=correction_text,
                update_id=update_id,
            )
        )
    return updates


# Parses an optional ISO timestamp emitted by the LLM.
# Inputs: ISO string with timezone, None, or an empty-ish string.
# Outputs: timezone-aware datetime or None.
def _parse_optional_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none", "open"}:
        return None
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


# Formats the correction confirmation reply.
# Inputs: updated surviving sessions and number of deleted rows.
# Outputs: Telegram reply text.
def _format_correction_reply(sessions: list[dict], deleted_count: int) -> str:
    if not sessions:
        return "<b>Attention removed</b>\n\nQuote to fix."

    lines = ["<b>Attention updated</b>"]
    for session in sessions:
        if len(lines) > 1:
            lines.append("")
        lines.extend(_format_session_fields(session))
    if deleted_count:
        lines.append("")
        lines.append(_format_field("Removed", deleted_count))
    lines.append("")
    lines.append("Quote to fix.")
    return "\n".join(lines)


# Formats labelled reply fields for one corrected attention session.
# Inputs: session dict.
# Outputs: escaped labelled fields with none for empty values.
def _format_session_fields(session: dict) -> list[str]:
    tz = _get_timezone(session["started_at"])
    ended_at = session.get("ended_at")
    duration = _format_duration(session["started_at"], ended_at) if ended_at else None
    return [
        _format_field("Description", session["description"]),
        _format_field("Category", session["category"].replace("_", " ")),
        _format_field("Project", session.get("project")),
        _format_field("Started", session["started_at"].astimezone(tz).strftime("%H:%M")),
        _format_field("Ended", ended_at.astimezone(tz).strftime("%H:%M") if ended_at else None),
        _format_field("Duration", duration),
    ]


# Adds correction provenance to a copied meta dict.
# Inputs: existing session, update payload, correction text, Telegram update_id.
# Outputs: updated meta dict.
def _append_correction_meta(
    session: dict,
    updates: dict,
    correction_text: str,
    update_id: int | None,
) -> dict:
    meta = dict(session.get("meta") or {})
    corrections = meta.get("corrections")
    if not isinstance(corrections, list):
        corrections = []
    corrections.append(
        {
            "source": "telegram",
            "self_reported": True,
            "telegram_update_id": update_id,
            "text": correction_text,
            "fields": sorted(k for k in updates if k != "meta"),
            "model": MODEL_FLASH,
        }
    )
    meta["corrections"] = corrections

    if "ended_at" in updates:
        if updates["ended_at"] is None:
            meta.pop("end", None)
        elif "end" not in meta:
            meta["end"] = {
                "source": "telegram",
                "self_reported": True,
                "reason": "manual_correction",
                "telegram_update_id": update_id,
            }
    return meta


# Normalizes nullable text fields from correction JSON.
# Inputs: arbitrary parsed value.
# Outputs: stripped string or None.
def _clean_nullable_text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return cleaned
