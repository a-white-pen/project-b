"""
Attention correction handler — applies B's quoted correction to attention session rows.

Functions:
  handle_attention_correction(msg, state) — parses and applies a quoted attention correction
  _fetch_sessions(ids)                    — loads attention session rows by ID
  _format_sessions_for_llm(sessions)      — formats session rows for the correction prompt
  _apply_corrections(...)                 — updates or deletes attention session rows; returns (updated_ids, deleted_ids) — sessions actually touched (UPDATE rowcount > 0 or DELETE run). Untouched IDs are intentionally absent so the caller doesn't render spurious "activity updated" replies for legacy multi-id state.
  _build_update_payload(...)              — converts one parsed correction into DB updates
  _compute_proposed_interval(...)         — computes the (started_at, ended_at) pair after a correction
  _validate_interval(...)                 — validates and logs rejected time intervals
  _format_overlap_conflict(sessions)      — formats overlapping sessions into a user-readable string
  _parse_optional_datetime(value)         — parses optional ISO timestamp values from the LLM
  _build_correction_replies(...)          — builds the list of (reply, state) tuples — one per affected session — using the shared session-block formatter from service.py
  _append_correction_meta(...)            — records correction provenance in meta.corrections
  _clean_nullable_text(value)             — normalizes nullable correction text fields
"""

import json
import logging
from datetime import datetime, timezone as dt_timezone
from html import escape

import psycopg2.extras

from domains.attention.service import (
    _VALID_PAIRS,
    _format_session_block,
    _get_overlapping_sessions,
    _is_valid_pair,
    _lock_attention_writes,
    _parse_co_categories,
    _parse_json,
    _parse_optional_datetime,
    _strip_co_category_marker,
    build_attention_state,
)
from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.logging import log_event, log_failure
from system.messages import InboundMessage
from system.timezone import get_timezone

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
- change category + subcategory (must change together if either changes)
- change description, project, notes
- adjust started_at or ended_at
- reopen a session by clearing ended_at
- delete a session, but only if B explicitly says delete/remove/ignore that log
- add a SECONDARY categorisation of the same activity (e.g. "also social" when the
  primary is exercise — the activity is one event, just categorised two ways). This
  is NOT a separate concurrent session — it gets appended as a marker in notes.
- remove an existing SECONDARY categorisation ("no longer also social", "drop the
  social co-category").
- edit a SECONDARY categorisation — emit BOTH the remove (old pair) and the add (new
  pair) in the same correction.

Return a JSON object with this exact structure:
{{
  "sessions": [
    {{
      "attention_session_id": <int>,
      "action": "<update or delete>",
      "category": "<optional main category — see taxonomy>",
      "subcategory": "<optional subcategory under that main category — see taxonomy>",
      "description": "<optional corrected description>",
      "project": "<optional corrected project, or null only if B explicitly removes it>",
      "started_at": "<optional ISO 8601 timestamp with timezone>",
      "ended_at": "<optional ISO 8601 timestamp with timezone, or null only if B explicitly says it is still open>",
      "notes": "<optional corrected note>",
      "co_category_to_add": {{ "category": "<main>", "subcategory": "<sub>" }},
      "co_category_to_remove": {{ "category": "<main>", "subcategory": "<sub>" }}
    }}
  ]
}}

Taxonomy — when emitting category/subcategory or co_category_to_add, pick exactly one row:
- work / deep_work, shallow_work, meetings, learning, planning
- social / social_in_person, social_messaging, social_broadcast
- self_care / exercise, personal_care, meditation
- eat / food_prep, food_collection, eating
- downtime / rest, entertainment
- admin / shopping_online, shopping_in_store, errands, life_admin, health_admin
- transit / commute, travel
- other / other

Rules:
- Only include fields B actually changed. Omit unchanged fields entirely.
- If category changes, ALWAYS also emit the new subcategory (and vice versa) so the
  pair stays valid. Never emit one without the other.
- Use update unless B explicitly asks to delete/remove/ignore the log.
- For "also X" / "this was also social" / "add Y as a second category", emit
  co_category_to_add with that pair. Do NOT create a new session and do NOT change
  the primary category.
- For "no longer also X" / "remove X co-category" / "drop the social tag", emit
  co_category_to_remove with that pair. Match the pair B refers to — usually they
  name only the subcategory (e.g. "remove social"), look up the main category from
  the session's current co-category markers (any "+ main : sub" lines in notes).
- For "change X co-category to Y" / "actually it was Y not X as the second one",
  emit BOTH co_category_to_remove (X pair) and co_category_to_add (Y pair) in the
  same session entry. Do NOT touch the primary category.
- If B gives a relative time like "10 minutes earlier", calculate the corrected ISO
  timestamp from the logged session times.
- Preserve Chinese characters if B used them.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles a correction to previously logged attention session rows.
# Inputs: quoted InboundMessage plus conversation_state containing attention_session_ids.
# Outputs: list of (reply, state) tuples — one Telegram message per affected session.
# After the per-block reply refactor, a quoted reply has exactly one session in state,
# so the list almost always contains a single "activity updated" or "activity removed"
# entry. The list shape exists for two reasons: (1) consistency with handle_attention_log,
# and (2) legacy multi-id conversation_state rows saved before the per-block refactor
# can still produce multiple replies in one correction.
def handle_attention_correction(msg: InboundMessage, state: dict) -> list[tuple[str, dict | None]]:
    context = state.get("context") or {}
    session_ids = context.get("attention_session_ids") or []
    correction_text = msg.text or msg.caption

    if not session_ids:
        return [("Nothing to correct — couldn't find the attention session IDs.", None)]
    if not correction_text:
        return [("What should change? Send the correction in text or voice.", None)]

    try:
        current_sessions = _fetch_sessions(session_ids)
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_correction_fetch_failed", e, update_id=msg.update_id)
        return [("Couldn't load the attention session — try again.", None)]

    if not current_sessions:
        return [("Nothing to correct — those attention sessions are already gone.", None)]

    try:
        # Resolve current local time using B's location — gives the LLM a concrete "now"
        # so relative corrections ("just finished", "10 min ago") produce correct timestamps.
        now_utc = datetime.now(tz=dt_timezone.utc)
        tz = get_timezone(now_utc)
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
        return [("Couldn't understand the correction — say it more directly?", None)]

    parsed_sessions = parsed.get("sessions") or []
    if not parsed_sessions:
        return [("I couldn't see a concrete change there. Annoying, but honest.", None)]

    # Multi-session corrections in one message are no longer reachable from the per-block
    # reply flow (each quoted reply scopes to a single session). The only path here is
    # legacy conversation_state rows saved before the per-block refactor. _apply_corrections
    # applies updates one at a time, so a batch that REQUIRES atomic moves (e.g. "S1 ended
    # 10:30, S2 started 10:30" when S1 originally ran past 10:30) hits a mid-batch overlap
    # block and rejects the whole correction with an unhelpful message. Surface a clearer
    # one and stop before any write attempt.
    parsed_ids_in_scope = {
        p.get("attention_session_id")
        for p in parsed_sessions
        if p.get("attention_session_id") in {s["attention_session_id"] for s in current_sessions}
    }
    if len(parsed_ids_in_scope) > 1:
        log_event(
            logger,
            logging.WARNING,
            "attention_correction_multi_session_rejected",
            update_id=msg.update_id,
            parsed_ids=sorted(i for i in parsed_ids_in_scope if i is not None),
        )
        return [(
            "This correction touches multiple sessions, which I can't apply atomically. "
            "Quote and correct one session at a time.",
            None,
        )]

    try:
        updated_ids, deleted_ids = _apply_corrections(
            current_sessions=current_sessions,
            parsed_sessions=parsed_sessions,
            correction_text=correction_text,
            update_id=msg.update_id,
        )
    except _CorrectionValidationError as e:
        return [(str(e), None)]
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_correction_apply_failed", e, update_id=msg.update_id)
        return [("Correction parsed but failed to save — try again.", None)]

    if not updated_ids and not deleted_ids:
        # LLM produced output but nothing was actually written — invalid fields, wrong IDs,
        # no-op updates, or all changes silently dropped (e.g. self-collision co-category).
        return [("I couldn't find a concrete change to apply. Try describing it more directly.", None)]

    try:
        updated_sessions = _fetch_sessions(updated_ids) if updated_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "attention_correction_refetch_failed", e, update_id=msg.update_id)
        updated_sessions = []

    # Deleted rows are gone from the DB by the time we get here; recover their pre-correction
    # state from current_sessions so the "activity removed" block still shows what was removed.
    # Untouched sessions in current_sessions (legacy multi-id state where only some were
    # corrected) intentionally don't appear in either list — they get no reply.
    deleted_id_set = set(deleted_ids)
    deleted_sessions = [
        s for s in current_sessions if s["attention_session_id"] in deleted_id_set
    ]

    # now_utc anchors the today/yesterday footer. Use the correction message's arrival
    # time when available; fall back to current UTC.
    correction_now_utc = msg.timestamp if msg.timestamp is not None else datetime.now(tz=dt_timezone.utc)
    return _build_correction_replies(
        updated_sessions=updated_sessions,
        deleted_sessions=deleted_sessions,
        now_utc=correction_now_utc,
        parent_telegram_reply_message_id=state["telegram_reply_message_id"],
    )


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
                    SELECT attention_session_id, category, subcategory, description, project,
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
            "subcategory": r[2],
            "description": r[3],
            "project": r[4],
            "started_at": r[5],
            "ended_at": r[6],
            "notes": r[7],
            "meta": r[8] or {},
        }
        for r in rows
    ]


# Formats current attention sessions as readable context for the correction LLM.
# Inputs: session dicts loaded from b.attention_sessions.
# Outputs: one text block for the prompt.
def _format_sessions_for_llm(sessions: list[dict]) -> str:
    lines = []
    for session in sessions:
        tz = get_timezone(session["started_at"])
        started_local = session["started_at"].astimezone(tz)
        ended_at = session.get("ended_at")
        ended_text = "open"
        if ended_at is not None:
            ended_text = f"{ended_at.isoformat()} / local {ended_at.astimezone(tz).strftime('%Y-%m-%d %H:%M')}"
        parts = [
            f"[id={session['attention_session_id']}]",
            f"description={session['description']}",
            f"category={session['category']}",
            f"subcategory={session.get('subcategory')}",
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
# Outputs: (updated_ids, deleted_ids) — sessions ACTUALLY touched in this correction.
#   updated_ids — UPDATE statements where rowcount > 0 (genuine field changes).
#   deleted_ids — DELETE statements run.
# Sessions in current_sessions that appear in NEITHER list were untouched (e.g. legacy
# multi-id state where only some sessions were corrected). The caller MUST NOT render
# untouched sessions as "activity updated" — they should not appear in the reply at all.
def _apply_corrections(
    current_sessions: list[dict],
    parsed_sessions: list[dict],
    correction_text: str,
    update_id: int | None,
) -> tuple[list[int], list[int]]:
    sessions_by_id = {s["attention_session_id"]: s for s in current_sessions}
    allowed_ids = set(sessions_by_id)
    deleted_ids: list[int] = []
    updated_ids: list[int] = []

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Serialize all attention writes — see _lock_attention_writes in service.py.
                _lock_attention_writes(cur)
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
                        deleted_ids.append(session_id)
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

                    # Block edits that would push this session into the time range of an
                    # existing one. Runs for BOTH closed and open proposed intervals — a
                    # reopen (ended_at = None) is treated as a [start, +infinity) range,
                    # so any later session is correctly detected as a conflict. Excludes
                    # the session being edited so a no-op or self-only change does not
                    # trigger the guard. _get_overlapping_sessions treats ended_at=None
                    # as +infinity via COALESCE in its WHERE clause.
                    proposed_started, proposed_ended = _compute_proposed_interval(
                        updates, sessions_by_id[session_id]
                    )
                    overlapping = _get_overlapping_sessions(
                        cur=cur,
                        started_at=proposed_started,
                        ended_at=proposed_ended,
                        for_update=True,
                        exclude_session_id=session_id,
                    )
                    if overlapping:
                        conflict_summary = _format_overlap_conflict(overlapping)
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_correction_overlap_blocked",
                            update_id=update_id,
                            session_id=session_id,
                            proposed_started_at=proposed_started.isoformat(),
                            proposed_ended_at=proposed_ended.isoformat() if proposed_ended else None,
                            conflicting_session_ids=[
                                s["attention_session_id"] for s in overlapping
                            ],
                        )
                        # For reopen attempts, surface a more specific message — the user
                        # asked to clear ended_at, and the conflict is the later sessions
                        # that would now sit inside the reopened range.
                        if proposed_ended is None:
                            raise _CorrectionValidationError(
                                f"Can't reopen — that would overlap later session(s): "
                                f"{conflict_summary}. Move or delete those first."
                            )
                        raise _CorrectionValidationError(
                            f"That correction would overlap an existing session: "
                            f"{conflict_summary}. Fix the conflicting session first."
                        )

                    set_clause = ", ".join(f"{col} = %s" for col in updates)
                    values = list(updates.values()) + [session_id]
                    cur.execute(
                        f"UPDATE b.attention_sessions SET {set_clause}, updated_at = now() "
                        "WHERE attention_session_id = %s",
                        values,
                    )
                    if cur.rowcount > 0:
                        updated_ids.append(session_id)
    finally:
        conn.close()

    log_event(
        logger,
        logging.INFO,
        "attention_correction_applied",
        update_id=update_id,
        updated_count=len(updated_ids),
        deleted_count=len(deleted_ids),
        updated_ids=sorted(updated_ids),
        deleted_ids=sorted(deleted_ids),
    )
    return sorted(updated_ids), sorted(deleted_ids)


# Computes the proposed (started_at, ended_at) pair after a correction is applied.
# Inputs: update payload (only changed fields present) and the existing session row.
# Outputs: (proposed_started_at, proposed_ended_at). ended_at is None when the proposed
# state is an open session (either unchanged-and-was-open, or explicitly reopened).
def _compute_proposed_interval(updates: dict, session: dict) -> tuple[datetime, datetime | None]:
    proposed_started = updates.get("started_at") or session["started_at"]
    # ended_at may be explicitly set to None (reopen), kept from session, or changed.
    if "ended_at" in updates:
        proposed_ended = updates["ended_at"]
    else:
        proposed_ended = session.get("ended_at")
    return proposed_started, proposed_ended


# Validates that the proposed started_at/ended_at interval for a session is coherent.
# Uses the update payload for any fields being changed, falls back to the existing session values.
# Logs and raises _CorrectionValidationError with a user-readable message if the interval is invalid.
def _validate_interval(updates: dict, session: dict, session_id: int, update_id: int | None) -> None:
    proposed_started, proposed_ended = _compute_proposed_interval(updates, session)

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


# Formats a short, user-readable summary of sessions that conflict with a proposed interval.
# Inputs: list of overlapping session dicts (typically 1-3 entries).
# Outputs: HTML string with each description wrapped in <code> so it renders distinctly
# AND the reply is guaranteed to be sent in parse_mode=HTML (telegram/replies.py auto-
# detects HTML tags). Wrapping in <code> is necessary because session descriptions are
# LLM/user-derived and may contain '<', '>', or '&' — without HTML mode + html.escape,
# Telegram's auto-detection can flip and the message would either fail to deliver (400)
# or render entities literally.
def _format_overlap_conflict(sessions: list[dict]) -> str:
    parts = []
    for s in sessions:
        tz = get_timezone(s["started_at"])
        started_local = s["started_at"].astimezone(tz).strftime("%H:%M")
        ended_at = s.get("ended_at")
        ended_local = ended_at.astimezone(tz).strftime("%H:%M") if ended_at else "open"
        description = escape(str(s.get("description") or "(no description)"))
        parts.append(
            f"id={s['attention_session_id']} "
            f"(<code>{description}</code>, {started_local}–{ended_local})"
        )
    return "; ".join(parts)


# Builds a safe update payload from one parsed correction object.
# Inputs: existing session, parsed correction, correction text, and Telegram update_id.
# Outputs: dict of column updates.
#
# Taxonomy handling:
# - If both category AND subcategory are emitted by the LLM AND they form a valid pair,
#   both columns are updated together. Either alone is rejected (would violate the DB
#   pair CHECK; the prompt instructs the LLM to always emit both together).
# - If only one is emitted, both are silently dropped from the update (the prompt should
#   prevent this, but defensive).
#
# Co-category handling (all three operate on the notes field via "+ main : sub" markers):
# - `co_category_to_remove: {category, subcategory}` → strip the matching marker line
#   from notes. Processed BEFORE add so an edit ("change social to entertainment co-cat")
#   resolves naturally to remove+add. Idempotent — removing a marker that isn't there
#   is a no-op.
# - `co_category_to_add: {category, subcategory}` → append a new marker line to notes
#   (one line, idempotent — same marker not added twice).
# - SELF-COLLISION GUARD: if co_category_to_add equals the EFFECTIVE primary pair (i.e.
#   the new primary after this correction, or current primary if unchanged), the add is
#   silently dropped — no point tagging an activity with its own primary categorisation.
def _build_update_payload(
    session: dict,
    parsed: dict,
    correction_text: str,
    update_id: int | None,
) -> dict:
    updates: dict[str, object] = {}

    # Category/subcategory update — only applied as a valid pair.
    new_category = parsed.get("category") if "category" in parsed else None
    new_subcategory = parsed.get("subcategory") if "subcategory" in parsed else None
    if new_category is not None and new_subcategory is not None:
        if _is_valid_pair(new_category, new_subcategory):
            updates["category"] = new_category
            updates["subcategory"] = new_subcategory
    if "description" in parsed and parsed["description"]:
        updates["description"] = str(parsed["description"]).strip()
    if "project" in parsed:
        updates["project"] = _clean_nullable_text(parsed["project"])
    if "started_at" in parsed:
        updates["started_at"] = _parse_optional_datetime(parsed["started_at"])
    if "ended_at" in parsed:
        updates["ended_at"] = _parse_optional_datetime(parsed["ended_at"])

    # Notes: if the LLM emits a notes field directly, use it as the base. Then process
    # co_category_to_remove BEFORE co_category_to_add — this makes "change X co-cat to Y"
    # work naturally even when both fields target the same notes string.
    base_notes_source = parsed["notes"] if "notes" in parsed else session.get("notes")
    notes_value = _clean_nullable_text(base_notes_source) if base_notes_source is not None else None

    co_to_remove = parsed.get("co_category_to_remove")
    if isinstance(co_to_remove, dict):
        rm_cat = co_to_remove.get("category")
        rm_sub = co_to_remove.get("subcategory")
        if _is_valid_pair(rm_cat, rm_sub):
            notes_value = _strip_co_category_marker(notes_value, rm_cat, rm_sub)

    co_to_add = parsed.get("co_category_to_add")
    if isinstance(co_to_add, dict):
        co_cat = co_to_add.get("category")
        co_sub = co_to_add.get("subcategory")
        if _is_valid_pair(co_cat, co_sub):
            # Self-collision guard: refuse to tag a session with its own primary.
            # Compare against the EFFECTIVE primary — the one that will exist after this
            # correction applies (respects any in-flight category/subcategory change).
            effective_cat = updates.get("category", session["category"])
            effective_sub = updates.get("subcategory", session.get("subcategory"))
            if (co_cat, co_sub) != (effective_cat, effective_sub):
                # Dedup via parser — recognises both "+ X : Y" (new write format) and
                # legacy "+ X / Y" markers as equivalent. Without this a legacy-stored
                # marker would be re-added because the raw-string compare would miss it.
                existing_pairs = set(_parse_co_categories(notes_value))
                if (co_cat, co_sub) not in existing_pairs:
                    marker = f"+ {co_cat} : {co_sub}"
                    if notes_value is None:
                        notes_value = marker
                    else:
                        notes_value = f"{notes_value}\n{marker}"

    # Only emit a notes update if the value actually differs from existing — avoids
    # writing a no-op meta blob just because the LLM echoed notes back unchanged.
    if notes_value != session.get("notes"):
        updates["notes"] = notes_value

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


# _parse_optional_datetime is now sourced from domains/attention/service to avoid
# drift between two near-identical implementations. The service version is stricter:
# it also rejects datetime instances missing tzinfo (this file's earlier copy let
# those pass through unchecked). The import above pulls it into this module's
# namespace so existing call sites and tests keep working without rewrites.


# Builds one (reply, state) tuple per affected session, mirroring handle_attention_log's
# per-block reply shape. Each updated session becomes its own quotable "activity updated"
# message; each deleted session becomes its own "activity removed" message that shows
# what was removed (recovered from the pre-correction snapshot).
# Inputs:
#   updated_sessions — sessions still present after the correction, re-fetched fresh.
#   deleted_sessions — sessions that were deleted, with their pre-correction state.
#   now_utc          — message arrival time in UTC for the today/yesterday footer.
#   parent_telegram_reply_message_id — message id of the bot reply B quoted to trigger
#                       this correction. Carried into each updated reply's state so the
#                       correction chain can be traced back.
# Outputs: list of (reply, state) tuples. Updated entries carry per-session state so
# further quoted corrections stay scoped to that one session. Removed entries carry
# state=None — there is no row left to correct, so quoting falls through to normal routing.
def _build_correction_replies(
    updated_sessions: list[dict],
    deleted_sessions: list[dict],
    now_utc: datetime,
    parent_telegram_reply_message_id: int,
) -> list[tuple[str, dict | None]]:
    results: list[tuple[str, dict | None]] = []
    for session in updated_sessions:
        reply = _format_session_block("updated", session, now_utc)
        new_state = build_attention_state(
            session["attention_session_id"],
            parent_telegram_reply_message_id=parent_telegram_reply_message_id,
        )
        results.append((reply, new_state))
    for session in deleted_sessions:
        reply = _format_session_block("removed", session, now_utc)
        results.append((reply, None))
    return results


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
