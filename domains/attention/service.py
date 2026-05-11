"""
Attention logging domain — handles log_attention intent.

Functions:
  handle_attention_log(msg)       — parses an attention start/finish message and persists it
  _extract_attention_event(...)   — asks the LLM to classify the attention lifecycle action
  _handle_start(...)              — starts a session and auto-closes the previous open one
  _handle_finish(...)             — closes the current open attention session
  _get_open_session(cur)          — fetches the current open attention session, if one exists
  _insert_session(...)            — inserts a new b.attention_sessions row
  _close_session(...)             — closes an open b.attention_sessions row
  _row_to_session(row)            — converts a DB row into a session dict
  _format_log_reply(...)          — formats the attention acknowledgement sent back to B
  _format_session_summary(...)    — formats one attention session for replies
  _format_closed_time_lines(...)  — formats ended/duration rows for closed sessions
  _format_duration(...)           — formats a duration in minutes/hours
  _format_field(...)              — formats an escaped labelled field
  _format_optional_value(...)     — formats nullable values as text, using none when empty
  _format_open_session_for_llm(...) — formats the open session for extraction context
  _clean_optional_text(value)     — normalizes nullable model text fields
  _get_timezone(...)              — resolves B's timezone as of an event timestamp
  _local_time_str(...)            — returns B's local time for LLM context
  _parse_json(raw)                — parses JSON from an LLM response
"""

import json
import logging
import re
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

_FALLBACK_TZ = ZoneInfo("Asia/Singapore")

_VALID_CATEGORIES = {
    "deep_work",
    "shallow_work",
    "planning",
    "learning",
    "exercise",
    "cooking",
    "eating",
    "commute",
    "life_admin",
    "personal_care",
    "social",
    "entertainment",
    "rest",
    "meditation",
    "other",
}

_VALID_ACTIONS = {"start_session", "finish_session"}

_EXTRACT_PROMPT = """\
You are extracting an attention log event for B's personal attention tracker.

The tracker records one primary-attention session at a time. A message may either:
- start_session: B is starting, doing, working on, eating, watching, reading, commuting, etc.
- finish_session: B is done, finished, stopped, completed, or is ending the current thing.

Current local time: {local_time}
Current open session, if any:
{open_session}

Message from B: {text}

Return a JSON object with this exact structure:
{{
  "action": "<start_session or finish_session>",
  "category": "<one of: deep_work, shallow_work, planning, learning, exercise, cooking, eating, commute, life_admin, personal_care, social, entertainment, rest, meditation, other>",
  "description": "<short plain description of the activity>",
  "project": "<project/context tag or null>",
  "notes": "<optional note or null>"
}}

Rules:
- Use finish_session for messages like "finish lunch", "finish mum mum", "done with Project B", "just finished and pushed to git", "stop watching", "finish poop", "finish pong pong".
- Use start_session for messages like "working on Project B", "prep breakfast", "eat lunch", "go mum mum", "watching Succession", "scrolling TikTok", "learning about RAG", "go poop", "go pong pong".
- Actual night sleep is not an attention session. If B says she is sleeping, still choose the closest action only if the router already sent it here, but use category "rest" and description "sleep mention routed to attention".
- Naps are allowed as category "rest".
- Coding, writing, analysis, debugging, and building with Codex are deep_work.
- Meetings, email, admin, Slack, and routine coordination are shallow_work.
- Cooking, meal prep, cooking dinner, chopping/prepping ingredients, and making food are "cooking".
- Eating, breakfast/lunch/dinner as an eating activity, and "mum mum" are "eating".
- Coffee break is "rest" unless B is logging the specific drink as nutrition.
- Ordering food is "life_admin".
- "pong pong" means shower/bathe; use category "personal_care" and description "shower".
- Washing dishes, laundry, tidying, cleaning, trash, and chores are "life_admin".
- Showering, brushing teeth, cutting fingernails, grooming, and poop/pee breaks are "personal_care".
- Keep description concise but specific. Preserve Chinese characters if B used them.
- Do not invent a project. Use null when there is no clear project/context.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles an attention logging request from B.
# Inputs: InboundMessage with text/caption describing an attention start or finish.
# Outputs: (reply string, pending_state dict | None). pending_state is used for quoted correction.
def handle_attention_log(msg: InboundMessage) -> tuple[str, dict | None]:
    text = msg.text or msg.caption
    if not text:
        return ("What are we logging? Give me the thing your attention is on.", None)

    occurred_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    open_session = _get_open_session(cur, for_update=False)
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_open_session_fetch_failed", e, update_id=msg.update_id)
        return ("Couldn't check the current attention session — try again.", None)
    log_event(
        logger,
        logging.INFO,
        "attention_open_session_checked",
        update_id=msg.update_id,
        has_open_session=open_session is not None,
        open_session_id=open_session["attention_session_id"] if open_session else None,
    )

    try:
        extracted = _extract_attention_event(text, occurred_at, open_session)
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_extract_failed", e, update_id=msg.update_id)
        return ("Couldn't parse that attention update — rephrase it for me?", None)

    action = extracted.get("action")
    if action not in _VALID_ACTIONS:
        log_event(logger, logging.WARNING, "attention_invalid_action", update_id=msg.update_id, action=action)
        return ("I couldn't tell if that starts or finishes something. Say it a bit more bluntly.", None)

    category = extracted.get("category")
    if category not in _VALID_CATEGORIES:
        log_event(logger, logging.WARNING, "attention_invalid_category", update_id=msg.update_id, category=category)
        extracted = {**extracted, "category": "other"}
        category = "other"

    log_event(
        logger,
        logging.INFO,
        "attention_event_extracted",
        update_id=msg.update_id,
        action=action,
        category=category,
        has_project=bool(extracted.get("project")),
        open_session_id=open_session["attention_session_id"] if open_session else None,
    )

    if action == "finish_session":
        return _handle_finish(msg, occurred_at, extracted)
    return _handle_start(msg, occurred_at, extracted)


# Extracts action/category/description/project from B's message using the LLM.
# Inputs: message text, event timestamp, and the currently open session for context.
# Outputs: parsed dict with action, category, description, project, and notes.
def _extract_attention_event(text: str, occurred_at: datetime, open_session: dict | None) -> dict:
    raw = generate_text(
        _EXTRACT_PROMPT.format(
            local_time=_local_time_str(occurred_at),
            open_session=_format_open_session_for_llm(open_session),
            text=text,
        ),
        model=MODEL_FLASH,
    )
    return _parse_json(raw)


# Starts a new attention session, auto-closing the current open session when needed.
# Inputs: inbound message, start timestamp, and parsed LLM extraction.
# Outputs: (reply string, pending_state dict | None).
def _handle_start(msg: InboundMessage, started_at: datetime, extracted: dict) -> tuple[str, dict | None]:
    description = (extracted.get("description") or "").strip()
    if not description:
        return ("I caught the intent, not the activity. What are you doing?", None)

    category = extracted.get("category") if extracted.get("category") in _VALID_CATEGORIES else "other"
    project = _clean_optional_text(extracted.get("project"))
    notes = _clean_optional_text(extracted.get("notes"))
    start_meta = {
        "start": {
            "source": "telegram",
            "self_reported": True,
            "telegram_update_id": msg.update_id,
        },
        "classification": {
            "model": MODEL_FLASH,
            "action": "start_session",
        },
    }

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Close ALL open sessions, not just one. Under normal operation there
                    # is at most one, but a race or manual edit could leave extras; closing
                    # all of them restores the invariant in the same transaction.
                    open_sessions = _get_all_open_sessions(cur, for_update=True)
                    if len(open_sessions) > 1:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_multiple_open_sessions_found",
                            update_id=msg.update_id,
                            open_session_ids=[s["attention_session_id"] for s in open_sessions],
                        )
                    # Guard before the loop, not inside it. Sessions are ordered DESC so
                    # open_sessions[0] is the most recently started one. If it started before
                    # our new session, all others did too — the check covers all rows.
                    # Checking inside the loop would risk committing partial closes if a
                    # mid-loop row triggered the guard (return inside `with conn:` commits).
                    if open_sessions and open_sessions[0]["started_at"] >= started_at:
                        return ("Timing got weird — an open session starts after this update. I didn't save a duplicate.", None)
                    closed_sessions = []
                    for open_session in open_sessions:
                        closed_sessions.append(
                            _close_session(
                                cur=cur,
                                session=open_session,
                                ended_at=started_at,
                                end_meta={
                                    "source": "system",
                                    "self_reported": False,
                                    "reason": "superseded_by_new_start",
                                    "triggering_telegram_update_id": msg.update_id,
                                },
                            )
                        )
                    new_session = _insert_session(
                        cur=cur,
                        category=category,
                        description=description,
                        project=project,
                        started_at=started_at,
                        notes=notes,
                        meta=start_meta,
                    )
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_start_save_failed", e, update_id=msg.update_id)
        return ("Couldn't save that attention session — try again.", None)

    # Use the most-recently-started closed session for the reply (what B was doing before).
    closed_session = closed_sessions[0] if closed_sessions else None
    log_event(
        logger,
        logging.INFO,
        "attention_session_started",
        update_id=msg.update_id,
        new_session_id=new_session["attention_session_id"],
        auto_closed_session_ids=[s["attention_session_id"] for s in closed_sessions],
        category=category,
    )
    reply = _format_log_reply(closed_session=closed_session, opened_session=new_session)
    session_ids = [new_session["attention_session_id"]]
    for s in closed_sessions:
        session_ids.append(s["attention_session_id"])
    state = {
        "domain": "attention",
        "context": {"attention_session_ids": session_ids},
    }
    return (reply, state)


# Finishes the current open attention session.
# Inputs: inbound message, finish timestamp, and parsed LLM extraction.
# Outputs: (reply string, pending_state dict | None).
def _handle_finish(msg: InboundMessage, ended_at: datetime, extracted: dict) -> tuple[str, dict | None]:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    open_sessions = _get_all_open_sessions(cur, for_update=True)
                    if not open_sessions:
                        return ("No open attention session to close. Tiny clerical void.", None)
                    if len(open_sessions) > 1:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_multiple_open_sessions_found",
                            update_id=msg.update_id,
                            open_session_ids=[s["attention_session_id"] for s in open_sessions],
                        )
                    # Close the most recently started open session (first in DESC order).
                    open_session = open_sessions[0]
                    if open_session["started_at"] >= ended_at:
                        return ("That finish time lands before the session started. I left it alone.", None)
                    closed_session = _close_session(
                        cur=cur,
                        session=open_session,
                        ended_at=ended_at,
                        end_meta={
                            "source": "telegram",
                            "self_reported": True,
                            "reason": "explicit_finish",
                            "telegram_update_id": msg.update_id,
                        },
                    )
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_finish_save_failed", e, update_id=msg.update_id)
        return ("Couldn't close the attention session — try again.", None)

    log_event(
        logger,
        logging.INFO,
        "attention_session_finished",
        update_id=msg.update_id,
        session_id=closed_session["attention_session_id"],
        category=closed_session["category"],
    )
    reply = _format_log_reply(closed_session=closed_session, opened_session=None)
    state = {
        "domain": "attention",
        "context": {"attention_session_ids": [closed_session["attention_session_id"]]},
    }
    return (reply, state)


# Fetches ALL open attention sessions (ended_at IS NULL).
# Inputs: an open DB cursor and whether the rows should be locked for update.
# Outputs: list of session dicts ordered by started_at DESC (most recent first).
#
# Using LIMIT 1 here would be unsafe: if a race or manual edit left two open sessions,
# _handle_start would only close one, preserving the second open session and violating
# the one-open-session invariant. Fetching all open rows ensures we close every stale
# session. The lock covers all returned rows so no concurrent writer can sneak a new
# open session between our fetch and our inserts.
#
# Note: a DB partial unique index (CREATE UNIQUE INDEX ON b.attention_sessions ((true))
# WHERE ended_at IS NULL) would enforce the invariant at the schema level, but adding
# that index requires bringing the proxy up and running a migration — left as a follow-up.
def _get_all_open_sessions(cur, for_update: bool = False) -> list[dict]:
    sql = """
        SELECT attention_session_id, category, description, project,
               started_at, ended_at, notes, meta
        FROM b.attention_sessions
        WHERE ended_at IS NULL
        ORDER BY started_at DESC
    """
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql)
    return [_row_to_session(row) for row in cur.fetchall()]


# Fetches the most-recent open attention session (convenience wrapper around _get_all_open_sessions).
# Inputs: an open DB cursor and whether the row should be locked for update.
# Outputs: session dict or None.
def _get_open_session(cur, for_update: bool = False) -> dict | None:
    rows = _get_all_open_sessions(cur, for_update=for_update)
    return rows[0] if rows else None


# Inserts a new b.attention_sessions row.
# Inputs: DB cursor plus normalized session fields.
# Outputs: inserted session dict.
def _insert_session(
    cur,
    category: str,
    description: str,
    project: str | None,
    started_at: datetime,
    notes: str | None,
    meta: dict,
) -> dict:
    cur.execute(
        """
        INSERT INTO b.attention_sessions
            (category, description, project, started_at, notes, meta)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING attention_session_id, category, description, project,
                  started_at, ended_at, notes, meta
        """,
        (
            category,
            description,
            project,
            started_at,
            notes,
            psycopg2.extras.Json(meta),
        ),
    )
    return _row_to_session(cur.fetchone())


# Closes an existing b.attention_sessions row and writes meta.end provenance.
# Inputs: DB cursor, current session dict, close timestamp, and meta.end payload.
# Outputs: updated session dict.
def _close_session(cur, session: dict, ended_at: datetime, end_meta: dict) -> dict:
    meta = dict(session.get("meta") or {})
    meta["end"] = end_meta
    cur.execute(
        """
        UPDATE b.attention_sessions
        SET ended_at = %s,
            meta = %s,
            updated_at = now()
        WHERE attention_session_id = %s
        RETURNING attention_session_id, category, description, project,
                  started_at, ended_at, notes, meta
        """,
        (
            ended_at,
            psycopg2.extras.Json(meta),
            session["attention_session_id"],
        ),
    )
    return _row_to_session(cur.fetchone())


# Converts a DB row tuple into a session dict.
# Inputs: row from b.attention_sessions SELECT/RETURNING.
# Outputs: dict with stable keys used by reply/correction code.
def _row_to_session(row) -> dict:
    return {
        "attention_session_id": row[0],
        "category": row[1],
        "description": row[2],
        "project": row[3],
        "started_at": row[4],
        "ended_at": row[5],
        "notes": row[6],
        "meta": row[7] or {},
    }


# Formats the attention acknowledgement reply.
# Inputs: optional closed session and optional newly opened session.
# Outputs: Telegram reply text.
def _format_log_reply(closed_session: dict | None, opened_session: dict | None) -> str:
    lines: list[str] = []
    if closed_session is not None:
        lines.append("<b>Attention end</b>")
        lines.append("")
        lines.extend(_format_session_summary(closed_session, include_end=True))
    if opened_session is not None:
        if lines:
            lines.append("")
        lines.append("<b>Attention start</b>")
        lines.append("")
        lines.extend(_format_session_summary(opened_session, include_end=False))
    lines.append("")
    lines.append("Quote to fix.")
    return "\n".join(lines)


# Formats one attention session for replies.
# Inputs: session dict and whether to include start/end/duration.
# Outputs: readable escaped field lines.
def _format_session_summary(session: dict, include_end: bool) -> list[str]:
    started_at = session["started_at"]
    ended_at = session.get("ended_at")
    tz = _get_timezone(ended_at or started_at)
    lines = [
        _format_field("Description", session["description"]),
        _format_field("Category", session["category"].replace("_", " ")),
        _format_field("Project", session.get("project")),
        _format_field("Started", started_at.astimezone(tz).strftime("%H:%M")),
    ]
    if include_end:
        lines.extend(_format_closed_time_lines(session, tz))
    return lines


# Formats ended/duration rows for a closed attention session.
# Inputs: session dict and timezone for local display.
# Outputs: list of escaped field lines.
def _format_closed_time_lines(session: dict, tz: ZoneInfo) -> list[str]:
    started_at = session["started_at"]
    ended_at = session.get("ended_at")
    if ended_at is None:
        return [
            _format_field("Ended", None),
            _format_field("Duration", None),
        ]
    return [
        _format_field("Ended", ended_at.astimezone(tz).strftime("%H:%M")),
        _format_field("Duration", _format_duration(started_at, ended_at)),
    ]


# Formats elapsed time as minutes or hours/minutes.
# Inputs: start and end timestamps.
# Outputs: compact human-readable duration.
def _format_duration(started_at: datetime, ended_at: datetime) -> str:
    total_minutes = max(0, round((ended_at - started_at).total_seconds() / 60))
    if total_minutes < 60:
        return f"{total_minutes} min"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours} hr"
    return f"{hours} hr {minutes} min"


# Formats one escaped labelled reply field.
# Inputs: label and value from a session row.
# Outputs: "Label: value" with empty values shown as none.
def _format_field(label: str, value) -> str:
    return f"{label}: {escape(_format_optional_value(value))}"


# Formats nullable values for attention replies.
# Inputs: arbitrary value from a session row.
# Outputs: string value, or "none" when empty.
def _format_optional_value(value) -> str:
    if value is None:
        return "none"
    cleaned = str(value).strip()
    if not cleaned:
        return "none"
    return cleaned


# Formats the open session for the LLM prompt.
# Inputs: current open session dict or None.
# Outputs: concise text representation.
def _format_open_session_for_llm(open_session: dict | None) -> str:
    if open_session is None:
        return "None"
    parts = [
        f"id={open_session['attention_session_id']}",
        f"category={open_session['category']}",
        f"description={open_session['description']}",
        f"started_at={open_session['started_at'].isoformat()}",
    ]
    if open_session.get("project"):
        parts.append(f"project={open_session['project']}")
    return "; ".join(parts)


# Normalizes optional LLM text fields.
# Inputs: arbitrary LLM value.
# Outputs: stripped string or None.
def _clean_optional_text(value) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return cleaned


# Returns B's timezone as-of a given event timestamp, falling back to Asia/Singapore.
# Inputs: event timestamp or None.
# Outputs: ZoneInfo instance.
#
# Fallback chain (in order):
#   1. Most recent b.location row at or before as_of  (correct as-of lookup)
#   2. Most recent b.location row regardless of time  (handles no prior-to-event row)
#   3. Asia/Singapore hardcoded                       (no location ever shared)
def _get_timezone(as_of: datetime | None = None) -> ZoneInfo:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    if as_of is not None:
                        cur.execute(
                            "SELECT timezone FROM b.location"
                            " WHERE created_at <= %s ORDER BY created_at DESC LIMIT 1",
                            (as_of,),
                        )
                        row = cur.fetchone()
                        if row:
                            log_event(logger, logging.INFO, "attention_timezone_resolved",
                                      source="as_of", timezone=row[0], as_of=as_of.isoformat())
                        else:
                            # No location at-or-before this event — use the most recent one anyway.
                            log_event(logger, logging.WARNING, "attention_timezone_as_of_miss",
                                      as_of=as_of.isoformat(), as_of_tzinfo=str(as_of.tzinfo))
                            cur.execute("SELECT timezone FROM b.latest_location")
                            row = cur.fetchone()
                            if row:
                                log_event(logger, logging.INFO, "attention_timezone_resolved",
                                          source="latest_location", timezone=row[0])
                    else:
                        cur.execute("SELECT timezone FROM b.latest_location")
                        row = cur.fetchone()
                        if row:
                            log_event(logger, logging.INFO, "attention_timezone_resolved",
                                      source="latest_location_no_as_of", timezone=row[0])
                    if row:
                        return ZoneInfo(row[0])
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "attention_timezone_lookup_failed",
            e,
            as_of=as_of.isoformat() if as_of else None,
        )
    return _FALLBACK_TZ


# Returns B's local time at the given event timestamp for the LLM prompt.
# Inputs: event timestamp or None.
# Outputs: readable time string.
def _local_time_str(as_of: datetime | None = None) -> str:
    tz = _get_timezone(as_of)
    if as_of is not None:
        local_now = as_of.astimezone(tz)
    else:
        local_now = datetime.now(tz=tz)
    return local_now.strftime("%H:%M on %A")


# Strips markdown code fences if the LLM wraps its response, then parses JSON.
# Inputs: raw model response.
# Outputs: parsed JSON object.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    return json.loads(cleaned)
