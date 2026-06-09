"""
Attention logging domain — handles log_attention intent.

Public functions:
  handle_attention_log(msg)              — parses an attention start/finish message and persists it
  handle_attention_status(msg)           — reports the current open session + today's per-main-category breakdown + untracked remainder (powers /attention_status); read-only, writes no state
  try_handle_wake_as_nap_end(msg)        — when B says "wake up" with an open rest session, close the nap instead of logging a wake event; returns None to fall through to sleep/wake
  close_open_sessions_externally(...)    — cross-domain API: closes any open attention session on behalf of another domain (e.g. sleep handler when B says "night night" without finishing first). Returns end-block replies; empty list when nothing open
  build_attention_state(...)             — constructs the conversation_state dict for one session reply; canonical state schema source so service.py and correction.py don't drift

Internal handlers:
  _extract_attention_event(...)          — asks the LLM to classify the attention lifecycle action
  _handle_start(...)                     — starts a session and auto-closes the previous open one; also emits the auto-wake reminder bubble when no recent wake exists
  _handle_finish(...)                    — closes the current open attention session
  _handle_completed(...)                 — inserts a completed past session from one message

DB access:
  _get_open_session(cur)                 — fetches the most recent open attention session, if one exists
  _get_all_open_sessions(cur)            — fetches ALL open sessions DESC by started_at; used by start/finish for sweep-close semantics
  _get_last_closed_session(cur)          — fetches the most recently closed session (latest ended_at); powers the "nothing open · last logged …" line
  _lock_attention_writes(cur)            — acquires a transaction-scoped advisory lock that serializes all attention writes
  _insert_session(...)                   — inserts a new b.attention_sessions row
  _close_session(...)                    — closes an open b.attention_sessions row
  _get_overlapping_sessions(...)         — fetches sessions that overlap a proposed interval
  _row_to_session(row)                   — converts a DB row into a session dict
  _get_daily_total_minutes(...)          — sums minutes for a main category over a UTC day-window; powers the "today · 1h 12m work" footer
  _get_window_category_minutes(...)      — sums per-main-category minutes CLIPPED to a UTC window (LEAST/GREATEST overlap); powers the /attention_status ledger as a true partition of waking time so far
  _get_wake_day_window_utc(...)          — computes [day_start, day_end) for the daily total, anchored to B's most recent morning wake (local-4am fallback)
  _get_most_recent_morning_wake_utc(...) — looks up the most recent wake-after-sleep in the last 24h

Rendering / formatting:
  _format_session_block(...)             — single-blockquote formatter for "activity <verb>" replies. Header + one combined blockquote (category, project, also lines, description, time(s)) + italic day-total footer (closed only) + blank line + expandable Categories menu (suppressed for "removed"). Shared with correction.py for "activity updated" / "activity removed"
  _format_attention_status(...)          — renders the /attention_status reply (design "v3", Option A): "Right now" block + divider + "Today so far · awake …" monospace <pre> ledger (label · time · █ bar · share-of-awake %) per main category + untracked residual
  _friendly_label(name)                  — taxonomy name → title-cased phrase for the "Right now"/"last" line (deep_work → "Deep work")
  _format_change_category_menu()         — renders the <blockquote expandable> labelled "Categories:" listing every (main, [subs]) taxonomy row. Attached to every reply except "removed"
  _build_session_reply(...)              — builds the (reply, state) tuple for one session block; state scopes the correction to that single session
  _format_category_label(...)            — "🟦 <b>work : deep_work</b>" — emoji-by-main-category + bold "main : sub" label (space-colon-space)
  _parse_co_categories(notes)            — extracts "+ main : sub" (or legacy "+ main / sub") markers from notes
  _strip_co_category_marker(...)         — removes a "+ main : sub" marker from notes; used by correction.py for co-category removal/editing
  _format_time_12h(dt_local)             — formats a tz-aware datetime as 12-hour "9:54 AM"
  _format_date_footer(...)               — "today" / "yesterday" / weekday / "24 May" for the end-reply footer
  _duration_minutes(...)                 — interval → whole minutes
  _format_duration_short(...)            — minutes → "1h 35m" / "45m" / "2h"

Helpers / utilities:
  _is_valid_pair / _resolve_pair         — taxonomy pair validators mirroring the DB CHECK
  _format_open_session_for_llm(...)      — formats the open session for extraction context
  _clean_optional_text(value)            — normalizes nullable model text fields
  _parse_optional_datetime(value)        — parses optional ISO timestamp values from the LLM
  _local_time_str(...)                   — returns B's local time for LLM context
  _parse_json(raw)                       — parses JSON from an LLM response

Imported (not defined here):
  get_timezone(as_of)                    — from system.timezone; resolves B's timezone as of an event timestamp
  ensure_recent_wake_logged(...)         — from domains.sleep.service; called inside _handle_start (local import to avoid top-level circular)
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.logging import log_event, log_failure
from system.messages import InboundMessage
from system.timezone import get_timezone

logger = logging.getLogger(__name__)

# v3 taxonomy: 8 main categories × 24 subcategories, strict pair validation.
# Mirrors the DB CHECK constraint attention_sessions_taxonomy_check and the table in
# domains/attention/TAXONOMY.md. When changing, edit the .md first, then propose the
# matching ALTER TABLE, wait for B to apply, then update this dict + the prompts.
_TAXONOMY: dict[str, list[str]] = {
    "work":      ["deep_work", "shallow_work", "meetings", "learning", "planning"],
    "social":    ["social_in_person", "social_messaging", "social_broadcast"],
    "self_care": ["exercise", "personal_care", "meditation"],
    "eat":       ["food_prep", "food_collection", "eating"],
    "downtime":  ["rest", "entertainment"],
    "admin":     ["shopping_online", "shopping_in_store", "errands", "life_admin", "health_admin"],
    "transit":   ["commute", "travel"],
    "other":     ["other"],
}

# Flattened set of valid (main, sub) tuples — 24 pairs in v3. Used by _is_valid_pair
# / _resolve_pair in this file, and by correction.py's pair check on the same import.
_VALID_PAIRS: set[tuple[str, str]] = {
    (main, sub) for main, subs in _TAXONOMY.items() for sub in subs
}

_VALID_ACTIONS = {"start_session", "finish_session", "log_completed"}

# Colored square per main category, matching B's hand-tuned legend within Telegram's
# 7-square palette (plus ⬛/⬜). The emoji is now keyed by MAIN category since
# subcategories share a colour with their main.
_CATEGORY_EMOJI: dict[str, str] = {
    "work":      "🟦",
    "social":    "🟪",
    "self_care": "🟩",
    "eat":       "🟧",
    "downtime":  "🟨",
    "admin":     "🟫",
    "transit":   "⬜",
    "other":     "⬛",
}

# Bullet for the residual "untracked" row in /attention_status (waking time no session
# covers). Deliberately a circle, not a colored square, so it reads as a meta/residual
# row distinct from the 8 taxonomy categories — none of which use a circle.
_UNTRACKED_EMOJI = "⚪"

# Typed horizontal rule between the "Right now" and "Today so far" sections of the
# /attention_status reply (U+2500 ×14). Telegram bot messages have no <hr>, so the divider
# is literal box-drawing text, per the "Attention Status Reply v3" design.
_STATUS_DIVIDER = "─" * 14


# Returns True when (category, subcategory) is a valid v3 taxonomy pair.
# Mirrors the DB-level pair CHECK so app code can fail fast with a useful message
# instead of letting Postgres reject the INSERT/UPDATE.
def _is_valid_pair(category: str | None, subcategory: str | None) -> bool:
    if category is None or subcategory is None:
        return False
    return (category, subcategory) in _VALID_PAIRS


# Normalises an (extracted_category, extracted_subcategory) pair to a guaranteed-valid
# (category, subcategory) tuple. Returns ('other', 'other') when the input is missing
# or invalid — used by extraction handlers so a hallucinated/invalid pair from the LLM
# does not break the DB CHECK or crash the request.
def _resolve_pair(category: str | None, subcategory: str | None) -> tuple[str, str]:
    if _is_valid_pair(category, subcategory):
        return (category, subcategory)
    return ("other", "other")


# Matches a "co-category" marker line in the notes field. Format: "+ main_category : subcategory"
# on its own line, e.g. "+ social : social". Multiple lines allowed. Used to surface
# user-requested additional categorisations of a single activity (e.g. tennis = exercise + social)
# in the Telegram bubble body. See _parse_co_categories.
#
# The separator accepts BOTH ":" (current write format, matches display) and "/" (legacy
# write format used in an earlier iteration of this session). Existing rows with the slash
# form are still recognised and rendered correctly.
_CO_CATEGORY_RE = re.compile(r"^\+\s*([a-z_]+)\s*[:/]\s*([a-z_]+)\s*$", re.MULTILINE)

_EXTRACT_PROMPT = """\
You are extracting an attention log event for B's personal attention tracker.

The tracker records one primary-attention session at a time. A message may either:
- start_session: B is starting, doing, working on, eating, watching, reading, commuting, etc.
- finish_session: B is done, finished, stopped, completed, or is ending the current thing.
- log_completed: B says an activity is already finished and gives a past start time.

Current local time: {local_time}
Current open session, if any:
{open_session}

Message from B: {text}

Return a JSON object with this exact structure:
{{
  "action": "<start_session, finish_session, or log_completed>",
  "category": "<one main category — see taxonomy below>",
  "subcategory": "<one subcategory under that main category — see taxonomy below>",
  "description": "<short plain description of the activity>",
  "project": "<project/context tag or null>",
  "notes": "<optional note or null>",
  "started_at": "<ISO 8601 timestamp with timezone, only for log_completed; otherwise null>",
  "ended_at": "<ISO 8601 timestamp with timezone, only for log_completed; otherwise null>"
}}

Taxonomy — pick exactly ONE main category AND ONE subcategory from the same row:
- work / deep_work, shallow_work, meetings, learning, planning
- social / social_in_person, social_messaging, social_broadcast
- self_care / exercise, personal_care, meditation
- eat / food_prep, food_collection, eating
- downtime / rest, entertainment
- admin / shopping_online, shopping_in_store, errands, life_admin, health_admin
- transit / commute, travel
- other / other

Subcategory examples (use these to disambiguate):
- deep_work: coding, writing, debugging, analysis, focused building (incl. with Codex)
- shallow_work: email, Slack, low-focus admin within work, routine coordination
- meetings: meeting, standup, catch-up call with manager/colleague, interview
- learning: watching tutorial video, reading textbook, DataCamp lesson, course
- planning: planning gym session, planning day, plan travel, plan trip
- social_in_person: lunch/dinner with friends, run with friends, racket sports with friends, AI meetup, hanging out with real people physically
- social_messaging: reply messages to friends, email friends, phone call with friends, video call / FaceTime / Zoom with friends, catch-up call. ANY call counts as messaging — synchronous communication through a device, not in person.
- social_broadcast: update IG story, update Strava, post to friends story, public-facing post on personal accounts
- exercise: gym, run, weight training, any workout
- personal_care: shower ("pong pong"), brush teeth, wash face, grooming, massage, physio, poop, pee
- meditation: meditate, breathing exercise
- food_prep: prep breakfast/dinner, heat up food, wash dishes, make protein shake, wash fruit. Anything where B is preparing food at home or cleaning up after.
- food_collection: collect lunch from downstairs, order food on Grab/Lineman/Robinhood, picking up a Grab order. The acquisition step, not the preparation.
- eating: eat breakfast/lunch/dinner ("mum mum"), post-workout snack, drinking protein shake
- rest: nap, taking a break, resting, coffee break (unless logging the drink as nutrition)
- entertainment: watching tv, scrolling Instagram/TikTok
- shopping_online: Shopee/Lazada order, researching products online, comparing items online, searching for flights
- shopping_in_store: shopping at Tops, in-store grocery, 7-11 run (when buying things), walk-in browsing
- errands: laundry, throw rubbish, change toilet light bulb, collect parcel (NOT shopping — a 7-11 run for purchases is shopping_in_store)
- life_admin: update finances, renew passport, track delivery
- health_admin: doctor visit, dentist, therapy session, pharmacy
- commute: MRT, Grab/taxi to nearby, bus, car to office
- travel: flight, overnight train, long drive between cities
- other: anything that genuinely doesn't fit

Rules:
- Pick the PRIMARY category. Co-activities (e.g., "tennis with friends" = both exercise and social) are NOT extracted here — only the primary one. B will explicitly say "also add X" via a correction if a secondary applies.
- Use log_completed when B says they finished/done/completed an activity and also gives a past start time in the same message, e.g. "Finish pooping, I started at around 7am". Set started_at to that past local time and ended_at to the message/current time.
- Use finish_session ONLY when the message is purely about ending the current activity with NO new activity mentioned, e.g. "finish lunch", "done with Project B", "stop watching", "finish poop".
- Use start_session whenever the message mentions a NEW activity, even if it also mentions finishing the current one. The system auto-closes the prior open session at the new start time, so a compound "finish X and do Y" message should resolve to start_session for Y. Examples:
  - "finish lunch and start coding" → start_session, category=work, subcategory=deep_work, description="coding"
  - "done with project b, going to get thai massage now" → start_session, category=self_care, subcategory=personal_care, description="thai massage"
  - "finish breakfast, go work on nutrition module" → start_session, category=work, subcategory=deep_work, description="work on nutrition module"
- Pure-start messages also use start_session: "working on Project B", "prep breakfast", "eat lunch", "go mum mum", "watching Succession", "scrolling TikTok", "learning about RAG", "go poop", "go pong pong", "nap nap" / "taking a nap" / "napping".
- Actual night sleep is not an attention session. If B says she is sleeping, still choose the closest action only if the router already sent it here, but use category "downtime" subcategory "rest" and description "sleep mention routed to attention".
- Naps are downtime / rest. Examples: "nap nap" → start_session, category=downtime, subcategory=rest, description="nap"; "taking a power nap" → same with description="power nap".
- "pong pong" means shower/bathe; use self_care / personal_care with description "shower".
- Keep description concise but specific. Preserve Chinese characters if B used them.
- Do not invent a project. Use null when there is no clear project/context.
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles an attention logging request from B.
# Inputs: InboundMessage with text/caption describing an attention start or finish.
# Outputs: list of (reply, state) tuples — one Telegram message per session block. A
# pure start gives one entry; a "finish X and start Y" message gives two (end block
# first, start block second); errors return a single (error_text, None) entry. Each
# state.context.attention_session_ids holds ONLY the session ID for that block so
# quoting any reply scopes the correction to that block.
def handle_attention_log(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    text = msg.text or msg.caption
    if not text:
        return [("What are we logging? Give me the thing your attention is on.", None)]

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
        return [("Couldn't check the current attention session — try again.", None)]
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
        return [("Couldn't parse that attention update — rephrase it for me?", None)]

    action = extracted.get("action")
    if action not in _VALID_ACTIONS:
        log_event(logger, logging.WARNING, "attention_invalid_action", update_id=msg.update_id, action=action)
        return [("I couldn't tell if that starts or finishes something. Say it a bit more bluntly.", None)]

    # Resolve (category, subcategory) to a guaranteed-valid pair; LLM hallucinations
    # fall back to (other, other) with a warning rather than failing the DB CHECK.
    raw_category = extracted.get("category")
    raw_subcategory = extracted.get("subcategory")
    category, subcategory = _resolve_pair(raw_category, raw_subcategory)
    if (category, subcategory) != (raw_category, raw_subcategory):
        log_event(
            logger,
            logging.WARNING,
            "attention_invalid_taxonomy_pair",
            update_id=msg.update_id,
            raw_category=raw_category,
            raw_subcategory=raw_subcategory,
        )
    extracted = {**extracted, "category": category, "subcategory": subcategory}

    log_event(
        logger,
        logging.INFO,
        "attention_event_extracted",
        update_id=msg.update_id,
        action=action,
        category=category,
        subcategory=subcategory,
        has_project=bool(extracted.get("project")),
        open_session_id=open_session["attention_session_id"] if open_session else None,
    )

    if action == "finish_session":
        return _handle_finish(msg, occurred_at, extracted)
    if action == "log_completed":
        return _handle_completed(msg, occurred_at, extracted)
    return _handle_start(msg, occurred_at, extracted)


# Handles the /attention_status read command. Renders the "Attention Status Reply v3"
# design (Option A): a "Right now" block (open session, or "Nothing open" + the last logged
# session), a divider, and a "Today so far · awake <Xh Ym>" monospace ledger showing minutes,
# a █ bar, and share-of-awake % per MAIN category plus an "untracked" residual.
# Read-only — writes nothing and returns no pending state (None), so quoting the reply
# does not trigger a correction.
# Inputs: InboundMessage from the /attention_status dispatch in telegram/router.py.
# Outputs: a single-element list [(reply_text, None)] (matches the other handlers' shape).
#
# "Awake / since waking" uses the same wake-day window as the end-block footer
# (_get_wake_day_window_utc — anchored to B's most recent morning wake, local-4am
# fallback). The currently-open session's running time is folded into its category total
# so the breakdown reflects time spent so far including the in-progress activity, and the
# percentages are each category's share of waking time so far.
def handle_attention_status(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    now_utc = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    log_event(logger, logging.INFO, "attention_status_requested", update_id=msg.update_id)

    # Resolve the wake-day window BEFORE opening our own connection: _get_wake_day_window_utc
    # opens its own connection for the morning-wake lookup, so keeping the two calls sequential
    # avoids holding two connections at once.
    tz = get_timezone(now_utc)
    day_start_utc, day_end_utc = _get_wake_day_window_utc(now_utc, tz)

    last_closed: dict | None = None
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    open_sessions = _get_all_open_sessions(cur, for_update=False)
                    # Only need the last logged session when nothing is currently open.
                    if not open_sessions:
                        last_closed = _get_last_closed_session(cur)
                    # Clip closed-session time to [day_start, min(now, day_end)] so the ledger
                    # is a true partition of waking time so far (shares ≤ 100%, untracked ≥ 0).
                    category_totals = _get_window_category_minutes(
                        cur, day_start_utc, min(now_utc, day_end_utc)
                    )
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_status_fetch_failed", e, update_id=msg.update_id)
        return [("Couldn't read your attention state — try again.", None)]

    open_session = open_sessions[0] if open_sessions else None

    # Fold the open session's running time into its main-category total. Counted from
    # max(started_at, day_start) so an overnight-open session contributes only the slice
    # inside today's window — matching the by-ended_at attribution used for closed sessions.
    totals = dict(category_totals)
    if open_session is not None:
        running = _duration_minutes(max(open_session["started_at"], day_start_utc), now_utc)
        if running > 0:
            cat = open_session["category"]
            totals[cat] = totals.get(cat, 0) + running

    # Untracked = waking time so far not covered by any session. Closed totals are clipped to
    # the waking window and the open-session fold is clipped to [day_start, now], and closed
    # sessions never overlap each other or the open one (one-open invariant + overlap-rejecting
    # corrections) — so sum(totals) ≤ elapsed and the displayed category lines plus this
    # remainder are a complete partition of waking time so far. max(0, …) stays as a
    # belt-and-suspenders against per-category integer rounding.
    elapsed_minutes = _duration_minutes(day_start_utc, now_utc)
    untracked_minutes = max(0, elapsed_minutes - sum(totals.values()))

    reply = _format_attention_status(
        now_utc, open_session, last_closed, totals, untracked_minutes, elapsed_minutes
    )
    log_event(
        logger,
        logging.INFO,
        "attention_status_sent",
        update_id=msg.update_id,
        has_open_session=open_session is not None,
        open_session_id=open_session["attention_session_id"] if open_session else None,
        category_count=sum(1 for m in totals.values() if m > 0),
        untracked_minutes=untracked_minutes,
        awake_minutes=elapsed_minutes,
    )
    return [(reply, None)]


# Closes an open category=rest attention session when B says "wake up" mid-nap.
# Inputs: InboundMessage arriving at the LOG_WAKE dispatch path in telegram/router.py.
# Outputs: list of (reply, state) tuples when a rest session was just closed (single
# end block); None when no rest session is open so the caller falls through to normal
# sleep/wake logging in domains/sleep/.
#
# This exists because "B wake up" said during an open nap should end the nap, not write
# a sleep_wake_events row. The router calls this before handle_wake_log; if it returns
# a list, that's the reply set, otherwise normal wake routing proceeds.
#
# The check + close run in a SINGLE locked transaction. An earlier version did an
# unlocked peek, then delegated to _handle_finish which closed whatever was open after
# acquiring its own lock — that allowed a racing message to swap the open session in
# between, ending a non-rest activity by mistake and suppressing the real wake log.
def try_handle_wake_as_nap_end(msg: InboundMessage) -> list[tuple[str, dict | None]] | None:
    ended_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    # Three possible return modes:
    #   None              — no open rest session, OR a pre-nap-check infra failure
    #                       (couldn't connect at all). Router falls through to
    #                       handle_wake_log as it always did.
    #   [(error, None)]   — a rest session WAS observed but we couldn't close it
    #                       (bad timing, DB error mid-transaction, etc.). Router
    #                       MUST NOT fall through to handle_wake_log — that would
    #                       write a spurious wake event while the nap stays open.
    #   [(reply, state)]  — nap closed successfully.
    nap_was_present = False
    closed_session: dict | None = None
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Acquire the attention write lock BEFORE reading the open session
                    # so the check and the close see a consistent view.
                    _lock_attention_writes(cur)
                    open_session = _get_open_session(cur, for_update=True)
                    # Naps live at (category=downtime, subcategory=rest).
                    # NOTE: only the MOST RECENT open session is inspected. If a stale
                    # older rest session sits behind a newer non-rest open, falling
                    # through to handle_wake_log is correct — "wake up" semantically
                    # targets the current activity, not a forgotten earlier nap. The
                    # partial unique index makes multi-open the abnormal case anyway.
                    if open_session is None or open_session.get("subcategory") != "rest":
                        return None
                    # Past this point: nap exists, we are committed to closing it.
                    nap_was_present = True
                    if open_session["started_at"] >= ended_at:
                        # Wake message timestamp lands before the nap started — should
                        # be impossible from a real Telegram update. We can't close
                        # (would produce a negative-duration session) but we also can't
                        # let a wake event write with the nap still open.
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_wake_nap_end_skipped_bad_timing",
                            update_id=msg.update_id,
                            open_session_id=open_session["attention_session_id"],
                        )
                        return [(
                            "Found an open nap but the timing is off — its start time "
                            "is after your wake message. Quote the nap and fix its start, "
                            "then try again.",
                            None,
                        )]
                    closed_session = _close_session(
                        cur=cur,
                        session=open_session,
                        ended_at=ended_at,
                        end_meta={
                            "source": "telegram",
                            "self_reported": True,
                            "reason": "wake_ended_nap",
                            "telegram_update_id": msg.update_id,
                        },
                    )
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_nap_end_save_failed", e, update_id=msg.update_id)
        if nap_was_present:
            # Confirmed nap, then mid-transaction failure. Hold back the wake event.
            return [(
                "Found an open nap but couldn't close it. Try saying 'wake up' again. "
                "Not logging a wake event because the nap is still open.",
                None,
            )]
        # Pre-nap-check infra failure (e.g. can't open connection): we never confirmed
        # a nap, so the safe behavior is to fall through to normal wake routing — same
        # as the prior code, which the router and existing wake test rely on.
        return None

    if closed_session is None:
        # Defensive: should be unreachable. All paths above either return early or
        # populate closed_session. Fall through to avoid leaving the user with no reply.
        return None

    log_event(
        logger,
        logging.INFO,
        "attention_wake_redirected_to_nap_end",
        update_id=msg.update_id,
        closed_session_id=closed_session["attention_session_id"],
    )
    return [_build_session_reply("end", closed_session, now_utc=ended_at)]


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
# Outputs: list of (reply, state) tuples — one per session block. When a session was
# auto-closed, the first entry is its end block; the final entry is always the new
# session's start block. Each reply carries state pointing only to its own session ID
# so quoting any block scopes the correction to just that session.
def _handle_start(msg: InboundMessage, started_at: datetime, extracted: dict) -> list[tuple[str, dict | None]]:
    description = (extracted.get("description") or "").strip()
    if not description:
        return [("I caught the intent, not the activity. What are you doing?", None)]

    # extracted's category/subcategory are already validated in handle_attention_log.
    category, subcategory = _resolve_pair(extracted.get("category"), extracted.get("subcategory"))
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
                    # Serialize all attention writes — concurrent handlers queue here
                    # rather than racing to violate the one-open-session invariant.
                    _lock_attention_writes(cur)
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
                    # our new session, all others did too — the check covers all rows under
                    # the DESC ordering invariant. The explicit filter is defense-in-depth
                    # against non-DESC ordering (data corruption, schema drift, etc.) and
                    # mirrors the safe-skip pattern used in close_open_sessions_externally.
                    # Checking inside the loop would risk committing partial closes if a
                    # mid-loop row triggered the guard (return inside `with conn:` commits).
                    skipped = [s for s in open_sessions if s["started_at"] >= started_at]
                    closeable = [s for s in open_sessions if s["started_at"] < started_at]
                    if skipped:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_start_blocked_by_future_open_session",
                            update_id=msg.update_id,
                            new_started_at=started_at.isoformat(),
                            blocking_session_ids=[s["attention_session_id"] for s in skipped],
                        )
                        return [("Timing got weird — an open session starts after this update. I didn't save a duplicate.", None)]
                    closed_sessions = []
                    for open_session in closeable:
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
                        subcategory=subcategory,
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
        return [("Couldn't save that attention session — try again.", None)]

    log_event(
        logger,
        logging.INFO,
        "attention_session_started",
        update_id=msg.update_id,
        new_session_id=new_session["attention_session_id"],
        auto_closed_session_ids=[s["attention_session_id"] for s in closed_sessions],
        category=category,
    )
    # One reply per session block. closed_sessions arrives DESC (most recent first); we
    # reverse for chronological order so older auto-closed sessions appear above newer
    # ones in the chat, with the new start last. Each reply carries state pointing only
    # to its own session ID so quoting any block scopes the correction to that one.
    # now_utc = started_at because the close timestamp IS this message's arrival time;
    # the today/yesterday footer is anchored to when the user logged this update.
    now_utc = started_at
    results: list[tuple[str, dict | None]] = []
    for closed in reversed(closed_sessions):
        results.append(_build_session_reply("end", closed, now_utc))
    results.append(_build_session_reply("start", new_session, now_utc))
    # Auto-wake reminder: if no wake event in the last 24h, the sleep domain
    # inserts one at now-5min and we append a second reply pointing at it so B
    # can quote-correct the time. Runs only on start blocks ("only do something
    # when awake") and dedups across same-day starts (existing wake in window
    # → no insert). All sleep/wake row writes live in domains/sleep — attention
    # only owns the decision to trigger.
    from domains.sleep.service import ensure_recent_wake_logged
    auto_wake = ensure_recent_wake_logged(
        now_utc, msg, trigger="attention_start_with_no_recent_wake"
    )
    if auto_wake is not None:
        wake_event_id, wake_at_utc = auto_wake
        tz = get_timezone(wake_at_utc)
        wake_at_local = wake_at_utc.astimezone(tz)
        reminder_text = (
            "⚠️ No wake event logged in the last 24h — I auto-logged your wake "
            f"at <b>{escape(_format_time_12h(wake_at_local))}</b>.\n"
            "Quote this to correct the time, or say <i>delete</i> to remove."
        )
        reminder_state = {
            "domain": "sleep_wake",
            "context": {
                "sleep_wake_event_ids": [wake_event_id],
                "event_type": "wake",
                "auto_inferred": True,
            },
        }
        results.append((reminder_text, reminder_state))
    return results


# Closes any open attention session(s) on behalf of an external trigger (e.g. the
# sleep handler when B logs "night night" without first finishing the current
# session). Inputs: inbound message that caused the close, the timestamp to close
# at, and a short reason string recorded in end_meta. Outputs: a list of end-block
# (reply, state) tuples — empty when no open sessions existed. Never raises;
# DB failures log + return [] so the caller can still send its own reply.
#
# Behaviour parallels _handle_finish but:
#   - No "no open session" error reply — silently returns [] (this is opportunistic)
#   - No started_at >= ended_at guard — sleep arrival timestamp is treated as truth
#   - end_meta marks source=system and includes the supplied reason
def close_open_sessions_externally(
    msg: InboundMessage,
    ended_at: datetime,
    reason: str,
) -> list[tuple[str, dict | None]]:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    _lock_attention_writes(cur)
                    open_sessions = _get_all_open_sessions(cur, for_update=True)
                    if not open_sessions:
                        return []
                    # Skip rows whose started_at would produce a negative duration —
                    # leave them open rather than corrupt the timeline. Logged so the
                    # condition is debuggable.
                    closeable = [s for s in open_sessions if s["started_at"] < ended_at]
                    skipped = [s for s in open_sessions if s["started_at"] >= ended_at]
                    if skipped:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_external_close_skipped_future_starts",
                            update_id=msg.update_id,
                            reason=reason,
                            skipped_ids=[s["attention_session_id"] for s in skipped],
                        )
                    closed_sessions = [
                        _close_session(
                            cur=cur,
                            session=open_session,
                            ended_at=ended_at,
                            end_meta={
                                "source": "system",
                                "self_reported": False,
                                "reason": reason,
                                "triggering_telegram_update_id": msg.update_id,
                            },
                        )
                        for open_session in closeable
                    ]
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "attention_external_close_failed",
            e,
            update_id=msg.update_id,
            reason=reason,
        )
        return []

    if not closed_sessions:
        return []
    log_event(
        logger,
        logging.INFO,
        "attention_external_close_succeeded",
        update_id=msg.update_id,
        reason=reason,
        closed_ids=[s["attention_session_id"] for s in closed_sessions],
    )
    # Reverse to chronological order (oldest end first) so output ordering matches
    # _handle_start's auto-close path.
    return [_build_session_reply("end", closed, ended_at) for closed in reversed(closed_sessions)]


# Finishes the current open attention session.
# Inputs: inbound message, finish timestamp, and parsed LLM extraction.
# Outputs: list of (reply, state) tuples — single end block on success; single error
# tuple on validation/save failure. List shape matches _handle_start so the caller
# (handle_attention_log and try_handle_wake_as_nap_end) can return uniformly.
def _handle_finish(msg: InboundMessage, ended_at: datetime, extracted: dict) -> list[tuple[str, dict | None]]:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize all attention writes — see _lock_attention_writes.
                    _lock_attention_writes(cur)
                    open_sessions = _get_all_open_sessions(cur, for_update=True)
                    if not open_sessions:
                        return [("No open attention session to close. Tiny clerical void.", None)]
                    if len(open_sessions) > 1:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_multiple_open_sessions_found",
                            update_id=msg.update_id,
                            open_session_ids=[s["attention_session_id"] for s in open_sessions],
                        )
                    # Reject the operation if any open session has started_at >= ended_at.
                    # Under DESC ordering, checking open_sessions[0] would catch all rows
                    # since older sessions necessarily started earlier — but the explicit
                    # filter is defense-in-depth against non-DESC ordering (data corruption,
                    # schema drift) and mirrors the safe-skip pattern used in
                    # close_open_sessions_externally. Protects against producing
                    # negative-duration rows.
                    skipped = [s for s in open_sessions if s["started_at"] >= ended_at]
                    closeable = [s for s in open_sessions if s["started_at"] < ended_at]
                    if skipped:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_finish_blocked_by_future_open_session",
                            update_id=msg.update_id,
                            new_ended_at=ended_at.isoformat(),
                            blocking_session_ids=[s["attention_session_id"] for s in skipped],
                        )
                        return [("That finish time lands before the session started. I left it alone.", None)]
                    # Close ALL closeable open sessions, not just the most recent. The
                    # partial unique index normally prevents multiple opens, but defensive
                    # cleanup matches _handle_start: if stale opens exist (manual edit,
                    # dropped index, etc.), this restores the invariant in the same
                    # transaction. closed_sessions[0] is the newest one — that's the
                    # one B's "finish" message is semantically about, and it drives the
                    # reply. Older ones are auto-closed silently with a system-source
                    # meta.end so they don't pollute the user-facing message.
                    closed_sessions = [
                        _close_session(
                            cur=cur,
                            session=open_session,
                            ended_at=ended_at,
                            end_meta={
                                "source": "telegram" if i == 0 else "system",
                                "self_reported": i == 0,
                                "reason": "explicit_finish" if i == 0 else "stale_open_swept_on_finish",
                                "telegram_update_id": msg.update_id,
                            },
                        )
                        for i, open_session in enumerate(closeable)
                    ]
                    closed_session = closed_sessions[0]
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_finish_save_failed", e, update_id=msg.update_id)
        return [("Couldn't close the attention session — try again.", None)]

    log_event(
        logger,
        logging.INFO,
        "attention_session_finished",
        update_id=msg.update_id,
        session_id=closed_session["attention_session_id"],
        category=closed_session["category"],
        also_swept_stale_open_ids=[s["attention_session_id"] for s in closed_sessions[1:]],
    )
    return [_build_session_reply("end", closed_session, now_utc=ended_at)]


# Inserts a completed attention session from one message.
# Inputs: inbound message, fallback end timestamp, and parsed LLM extraction with started_at.
# Outputs: list of (reply, state) tuples — single end block on success; single error
# tuple on validation/save failure. List shape matches _handle_start / _handle_finish.
def _handle_completed(msg: InboundMessage, ended_at: datetime, extracted: dict) -> list[tuple[str, dict | None]]:
    description = (extracted.get("description") or "").strip()
    if not description:
        return [("I caught that it finished, not what finished. Annoying little blank.", None)]

    try:
        started_at = _parse_optional_datetime(extracted.get("started_at"))
        completed_at = _parse_optional_datetime(extracted.get("ended_at")) or ended_at
    except (ValueError, TypeError) as e:
        log_failure(logger, logging.WARNING, "attention_completed_timestamp_parse_failed", e, update_id=msg.update_id)
        return [("Couldn't parse the timestamps — make sure you include when it started.", None)]
    if started_at is None:
        return [("I can log the finished thing if you include when it started.", None)]
    if started_at >= completed_at:
        return [("That completed session would end before it starts. Time did a little backflip.", None)]

    # extracted's category/subcategory are already validated in handle_attention_log.
    category, subcategory = _resolve_pair(extracted.get("category"), extracted.get("subcategory"))
    project = _clean_optional_text(extracted.get("project"))
    notes = _clean_optional_text(extracted.get("notes"))
    meta = {
        "start": {
            "source": "telegram",
            "self_reported": True,
            "telegram_update_id": msg.update_id,
        },
        "end": {
            "source": "telegram",
            "self_reported": True,
            "reason": "single_message_completed",
            "telegram_update_id": msg.update_id,
        },
        "classification": {
            "model": MODEL_FLASH,
            "action": "log_completed",
        },
    }

    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize all attention writes — see _lock_attention_writes.
                    _lock_attention_writes(cur)
                    overlapping_sessions = _get_overlapping_sessions(
                        cur=cur,
                        started_at=started_at,
                        ended_at=completed_at,
                        for_update=True,
                    )
                    if overlapping_sessions:
                        log_event(
                            logger,
                            logging.WARNING,
                            "attention_completed_overlap_found",
                            update_id=msg.update_id,
                            overlapping_session_ids=[
                                s["attention_session_id"] for s in overlapping_sessions
                            ],
                            started_at=started_at.isoformat(),
                            ended_at=completed_at.isoformat(),
                        )
                        return [(
                            "That overlaps an existing attention session. Fix the existing row first; "
                            "time bookkeeping is already spicy enough.",
                            None,
                        )]
                    completed_session = _insert_session(
                        cur=cur,
                        category=category,
                        subcategory=subcategory,
                        description=description,
                        project=project,
                        started_at=started_at,
                        ended_at=completed_at,
                        notes=notes,
                        meta=meta,
                    )
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "attention_completed_save_failed", e, update_id=msg.update_id)
        return [("Couldn't save that completed attention session — try again.", None)]

    log_event(
        logger,
        logging.INFO,
        "attention_session_completed_logged",
        update_id=msg.update_id,
        session_id=completed_session["attention_session_id"],
        category=category,
        started_at=started_at.isoformat(),
        ended_at=completed_at.isoformat(),
    )
    # now_utc = message arrival time (ended_at function arg), NOT the parsed completed_at
    # which may be far in the past — the footer's today/yesterday is relative to when B
    # is sending the log, not when the session itself ended.
    return [_build_session_reply("end", completed_session, now_utc=ended_at)]


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
# The invariant is also enforced at the schema level by the partial unique index
# `b.one_open_attention_session` (CREATE UNIQUE INDEX ON b.attention_sessions ((true))
# WHERE ended_at IS NULL) — see _lock_attention_writes for the full defense-in-depth story.
def _get_all_open_sessions(cur, for_update: bool = False) -> list[dict]:
    sql = """
        SELECT attention_session_id, category, subcategory, description, project,
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


# Fetches the most recently CLOSED attention session (latest ended_at). Read-only — powers
# the "nothing open · last logged …" line in /attention_status when no session is running.
# Inputs: an open DB cursor. Outputs: session dict, or None when nothing has ever been closed.
def _get_last_closed_session(cur) -> dict | None:
    cur.execute(
        """
        SELECT attention_session_id, category, subcategory, description, project,
               started_at, ended_at, notes, meta
        FROM b.attention_sessions
        WHERE ended_at IS NOT NULL
        ORDER BY ended_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return _row_to_session(row) if row else None


# Acquires a transaction-scoped advisory lock that serializes all attention writes.
# Inputs: an open DB cursor inside a transaction.
# Outputs: none — the call blocks (couple of ms typical) until any other attention write
# transaction commits, then returns with the lock held.
#
# Call as the FIRST statement inside every write transaction in this module and in
# correction.py. The lock auto-releases on COMMIT or ROLLBACK; no manual release needed.
# Together with the partial unique index `b.one_open_attention_session`, this guarantees
# that at most one attention session is open at any time — concurrent handlers queue
# at the lock rather than racing to violate the invariant.
def _lock_attention_writes(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('b.attention_sessions'))")


# Fetches attention sessions that overlap a proposed interval.
# Inputs:
#   started_at         — proposed interval start.
#   ended_at           — proposed interval end, or None for an open-ended (reopen)
#                        proposal which is treated as +infinity. Matters because a
#                        reopened session at 9:00 with ended_at=None overlaps every
#                        later session, not just sessions concurrent at 9:00.
#   for_update         — apply FOR UPDATE row lock so concurrent writers wait.
#   exclude_session_id — optional id to exclude (the row being edited itself).
# Outputs: list of overlapping session dicts.
def _get_overlapping_sessions(
    cur,
    started_at: datetime,
    ended_at: datetime | None,
    for_update: bool = False,
    exclude_session_id: int | None = None,
) -> list[dict]:
    sql = """
        SELECT attention_session_id, category, subcategory, description, project,
               started_at, ended_at, notes, meta
        FROM b.attention_sessions
        WHERE started_at < COALESCE(%s, 'infinity'::timestamptz)
          AND COALESCE(ended_at, 'infinity'::timestamptz) > %s
    """
    params: list = [ended_at, started_at]
    if exclude_session_id is not None:
        sql += " AND attention_session_id != %s"
        params.append(exclude_session_id)
    sql += " ORDER BY started_at, attention_session_id"
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql, tuple(params))
    return [_row_to_session(row) for row in cur.fetchall()]


# Inserts a new b.attention_sessions row.
# Inputs: DB cursor plus normalized session fields. (category, subcategory) must form
# a valid pair under _TAXONOMY — the DB CHECK constraint rejects bad pairs as a safety
# net but callers should resolve via _resolve_pair() first.
# Outputs: inserted session dict.
def _insert_session(
    cur,
    category: str,
    subcategory: str,
    description: str,
    project: str | None,
    started_at: datetime,
    notes: str | None,
    meta: dict,
    ended_at: datetime | None = None,
) -> dict:
    cur.execute(
        """
        INSERT INTO b.attention_sessions
            (category, subcategory, description, project, started_at, ended_at, notes, meta)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING attention_session_id, category, subcategory, description, project,
                  started_at, ended_at, notes, meta
        """,
        (
            category,
            subcategory,
            description,
            project,
            started_at,
            ended_at,
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
        RETURNING attention_session_id, category, subcategory, description, project,
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
        "subcategory": row[2],
        "description": row[3],
        "project": row[4],
        "started_at": row[5],
        "ended_at": row[6],
        "notes": row[7],
        "meta": row[8] or {},
    }


# Formats one session block. Layout depends on whether the session is closed.
# Inputs:
#   verb     — appears in "activity <verb>" header. Values: "ended" (log finish or
#              auto-close), "started" (log new open), "updated" (correction confirmation),
#              "removed" (correction deletion confirmation).
#   session  — attention session dict (must include category, subcategory).
#   now_utc  — message arrival time in UTC; used for the today/yesterday footer label
#              and as the anchor day for the daily-total query. Ignored when the
#              session is open (no footer in that case).
# Outputs: Telegram HTML reply text.
#
# Closed session layout (single combined blockquote, italic footer, then Categories menu):
#   activity <verb>                          ← bold header on its own line
#   ┃ 🟧 eat : eating                        ← category : subcategory
#   ┃ project: Project B                     ← when present
#   ┃ also: social : social_in_person        ← one line per co-cat marker in notes
#   ┃ "description"                          ← description (italic + quotes)
#   ┃ 9:40 AM → 9:54 AM · 14m                ← time range + duration on the SAME line
#   <i>Today: 1h 12m in eat</i>              ← italic footer (no blockquote, closed only)
#   ┃ <b>Categories:</b> ▾                   ← expandable menu (suppressed for "removed")
#
# Open session layout (same body order; no time range, no duration, no footer):
#   activity <verb>
#   ┃ 🟦 work : deep_work
#   ┃ project: Project B                     ← when present
#   ┃ also: social : social_in_person        ← when present
#   ┃ "description"
#   ┃ 12:43 PM
#   ┃ <b>Categories:</b> ▾
def _format_session_block(verb: str, session: dict, now_utc: datetime) -> str:
    started_at = session["started_at"]
    ended_at = session.get("ended_at")
    tz = get_timezone(ended_at or started_at)
    started_local = started_at.astimezone(tz)
    is_closed = ended_at is not None

    category = session["category"]
    subcategory = session.get("subcategory") or "other"
    description = escape(session.get("description") or "(no description)")
    project = session.get("project")
    co_cats = _parse_co_categories(session.get("notes"))
    menu = _format_change_category_menu() if verb != "removed" else ""

    header = f"<b>activity {escape(verb)}</b>"

    # Single combined blockquote body. Order:
    #   category : subcategory
    #   project: X            (when present)
    #   also: X : Y           (one line per co-cat marker)
    #   "description"         (italic + quotes)
    #   start [→ end · duration]   (single time for open; range + duration on the
    #                               SAME line for closed — no separate category-line dot)
    lines = [_format_category_label(category, subcategory)]
    if project:
        lines.append(f"project: {escape(str(project))}")
    for co_cat, co_sub in co_cats:
        # Reuse _format_category_label so the also line carries the same colour
        # block + bold "main : sub" treatment as the primary category. The
        # "also: " prefix keeps the visual subordination clear.
        lines.append(f"also: {_format_category_label(co_cat, co_sub)}")
    lines.append(f'<i>"{description}"</i>')
    if is_closed:
        ended_local = ended_at.astimezone(tz)
        duration_str = _format_duration_short(_duration_minutes(started_at, ended_at))
        lines.append(
            f"{_format_time_12h(started_local)} → {_format_time_12h(ended_local)}"
            f" · {escape(duration_str)}"
        )
    else:
        lines.append(_format_time_12h(started_local))
    body = "<blockquote>" + "\n".join(lines) + "</blockquote>"

    # Open sessions have no italic footer — only closed blocks carry "Today: ... in <main>".
    # When the Categories menu is present, prepend a blank line for breathing room
    # (\n\n produces one empty line of visual separation in Telegram).
    if not is_closed:
        return f"{header}\n{body}\n\n{menu}" if menu else f"{header}\n{body}"

    # Footer: "Today: 1h 12m in eat" — daily total for this main category, anchored to
    # B's most recent "morning wake" (the wake event whose immediately preceding
    # sleep/wake row is a sleep — nap-end wakes are excluded). When no qualifying wake
    # is found in the last 24h, falls back to local 4am cutoff so the footer still
    # shows something sensible.
    now_local = now_utc.astimezone(tz)
    ended_local = ended_at.astimezone(tz)
    date_label = _format_date_footer(ended_local, now_local)
    main_text = escape(category.replace("_", " "))
    day_start_utc, day_end_utc = _get_wake_day_window_utc(ended_at, tz)
    daily_minutes = _get_daily_total_minutes(
        category=category,
        day_start_utc=day_start_utc,
        day_end_utc=day_end_utc,
    )
    daily_str = _format_duration_short(daily_minutes)
    footer = f"<i>{escape(date_label)}: {escape(daily_str)} in {main_text}</i>"

    parts = [header, body, footer]
    if menu:
        # Empty string in the parts list becomes a blank line via "\n".join — gives the
        # Categories blockquote visual breathing room from the footer above.
        parts.append("")
        parts.append(menu)
    return "\n".join(parts)


# Rounds half up (toward +∞) for non-negative inputs, matching the design's JS Math.round so
# the /attention_status percentages and bar lengths land exactly as in the approved mock.
# Python's built-in round() uses banker's rounding, which differs by one on exact .5 cases.
def _round_half_up(value: float) -> int:
    return int(value + 0.5)


# Renders a stored taxonomy name (main or sub) as a friendly title-cased phrase for the
# "Right now" / "last" line: underscores → spaces, first letter capitalised (deep_work →
# "Deep work", social_in_person → "Social in person"). Only the first character is touched,
# so any intentional casing later in the word is preserved.
def _friendly_label(name: str) -> str:
    spaced = name.replace("_", " ")
    return spaced[:1].upper() + spaced[1:] if spaced else spaced


# Renders the /attention_status reply — the "Attention Status Reply v3" design, Option A.
# One Telegram-HTML bubble, three stacked sections (no blockquotes):
#   "Right now"  — open session: colour square + friendly subcategory, italic description,
#                  "since <time> · <elapsed>". Nothing open: "Nothing open" + the last logged
#                  session ('<sq> last · <Sub> "<desc>"' / 'ended <time> · <N> ago'); a bare
#                  "Nothing open" only when nothing has ever been closed.
#   divider      — the typed ────────────── rule (_STATUS_DIVIDER).
#   "Today so far · awake <Xh Ym>" — header, then a monospace <pre> ledger: one row per non-zero
#                  main category plus the untracked residual, each
#                  "<sq> <label> <time>  <█-bar> <pct>%". Columns are space-aligned; the bar is
#                  monochrome █ scaled so the biggest row gets 7 squares; pct is share of awake.
#                  Rows sorted biggest-first (name tiebreak; "" pins untracked ahead on a tie).
#                  "nothing yet" when every row is zero.
# Colour comes only from the one leading square per row (Telegram can't colour text or █); a
# single constant-width square per row keeps the <pre> columns aligned.
# Inputs: now (UTC); the open session dict (or None); the last closed session (or None, used only
# when nothing is open); {main_category: minutes} totals already inclusive of the open session's
# running time; the untracked remainder; and awake_minutes (waking time so far) for the header
# and the percentages. Output: Telegram-HTML string.
def _format_attention_status(
    now_utc: datetime,
    open_session: dict | None,
    last_closed: dict | None,
    category_totals: dict[str, int],
    untracked_minutes: int,
    awake_minutes: int,
) -> str:
    tz = get_timezone(now_utc)

    # ── Right now ────────────────────────────────────────────────────────────────
    if open_session is not None:
        started_local = open_session["started_at"].astimezone(tz)
        elapsed = _format_duration_short(_duration_minutes(open_session["started_at"], now_utc))
        emoji = _CATEGORY_EMOJI.get(open_session["category"], "⬜")
        sub = escape(_friendly_label(open_session.get("subcategory") or "other"))
        description = escape(open_session.get("description") or "(no description)")
        now_block = (
            "<b>Right now</b>\n"
            f"{emoji} <b>{sub}</b>\n"
            f"<i>“{description}”</i>\n"
            f"since {escape(_format_time_12h(started_local))} · <b>{escape(elapsed)}</b>"
        )
    elif last_closed is not None:
        ended_local = last_closed["ended_at"].astimezone(tz)
        ago_min = _duration_minutes(last_closed["ended_at"], now_utc)
        ago = "just now" if ago_min == 0 else f"{_format_duration_short(ago_min)} ago"
        emoji = _CATEGORY_EMOJI.get(last_closed["category"], "⬜")
        sub = escape(_friendly_label(last_closed.get("subcategory") or "other"))
        description = escape(last_closed.get("description") or "(no description)")
        now_block = (
            "<b>Right now</b>\n"
            "<i>Nothing open</i>\n"
            f"{emoji} last · <b>{sub}</b> <i>“{description}”</i>\n"
            f"ended {escape(_format_time_12h(ended_local))} · {escape(ago)}"
        )
    else:
        now_block = "<b>Right now</b>\n<i>Nothing open</i>"

    # ── Today so far ─────────────────────────────────────────────────────────────
    header = f"<b>Today so far · awake {escape(_format_duration_short(max(0, awake_minutes)))}</b>"

    # rows: (minutes, sort_name, ledger_label). untracked uses "" as sort_name so it pins
    # ahead of a same-minute category, matching the breakdown's tiebreak elsewhere.
    rows: list[tuple[int, str, str]] = [
        (mins, cat, cat.replace("_", " ")) for cat, mins in category_totals.items() if mins > 0
    ]
    if untracked_minutes > 0:
        rows.append((untracked_minutes, "", "untracked"))
    rows.sort(key=lambda r: (-r[0], r[1]))

    if rows:
        pcts = [_round_half_up(m / awake_minutes * 100) if awake_minutes > 0 else 0 for m, _, _ in rows]
        times = [_format_duration_short(m) for m, _, _ in rows]
        max_pct = max(pcts) or 1
        label_w = max(len(lbl) for _, _, lbl in rows)
        time_w = max(len(t) for t in times)
        lines = []
        for (_, name, lbl), tstr, pct in zip(rows, times, pcts):
            emoji = _UNTRACKED_EMOJI if name == "" else _CATEGORY_EMOJI.get(name, "⬜")
            squares = max(1, min(7, _round_half_up(pct / max_pct * 7)))
            bar = ("█" * squares).ljust(7)
            # Two-space gutter between label and time so the widest label (e.g. "untracked")
            # never butts against a full-width time like "1h 12m"; times stay right-aligned.
            line = f"{emoji} {lbl.ljust(label_w)}  {tstr.rjust(time_w)}  {bar} {(str(pct) + '%').rjust(4)}"
            lines.append(escape(line))
        ledger = "<pre>" + "\n".join(lines) + "</pre>"
    else:
        ledger = "<i>nothing yet</i>"

    return f"{now_block}\n{_STATUS_DIVIDER}\n{header}\n{ledger}"


# Renders the "change category" expandable blockquote attached to the bottom of every
# started/ended/updated reply. Uses Telegram's <blockquote expandable> attribute so
# the menu collapses to a short preview with a "▾" caret; tapping expands the full
# 8-main-categories listing. No callbacks involved — the menu is purely a reference;
# B changes a category by quoting the reply and saying the new sub (e.g. "shallow_work"),
# which routes through the existing correction flow.
#
# Returns the HTML fragment to append to the reply. Does NOT include a leading newline
# — caller is responsible for the separator.
def _format_change_category_menu() -> str:
    # Leading blank line inside the blockquote gives "Categories:" breathing room
    # above so the first colored emoji square doesn't visually crowd the heading.
    # NB: Telegram strips a pure-empty leading line inside <blockquote>, so we use
    # a non-breaking space (U+00A0) to force a visible empty line.
    lines = [" ", "<b>Categories:</b>", ""]
    for main, subs in _TAXONOMY.items():
        emoji = _CATEGORY_EMOJI.get(main, "⬜")
        main_label = escape(main.replace("_", " "))
        sub_list = escape(" ".join(subs))
        lines.append(f"{emoji} <b>{main_label}</b>")
        lines.append(f"<code>{sub_list}</code>")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


# Builds the conversation_state dict for one attention session reply.
# Inputs: the session id this reply is about, and (optionally) the parent telegram reply
# message id when this reply is itself the result of a quoted correction.
# Outputs: state dict with the canonical attention shape. Single source of truth for the
# state schema so service.py and correction.py don't drift.
def build_attention_state(session_id: int, parent_telegram_reply_message_id: int | None = None) -> dict:
    state: dict = {
        "domain": "attention",
        "context": {"attention_session_ids": [session_id]},
    }
    if parent_telegram_reply_message_id is not None:
        state["parent_telegram_reply_message_id"] = parent_telegram_reply_message_id
    return state


# Builds the per-session (reply, state) tuple used by every attention write handler.
# Inputs: kind ("end" or "start"), the session dict, and message arrival time in UTC.
# Outputs: (reply_text, state) where state.context.attention_session_ids holds ONLY this
# session's ID so quoting the reply routes the correction to just this session.
def _build_session_reply(kind: str, session: dict, now_utc: datetime) -> tuple[str, dict]:
    verb = {"end": "ended", "start": "started"}.get(kind)
    if verb is None:
        raise ValueError(f"unknown reply kind: {kind}")
    reply = _format_session_block(verb, session, now_utc)
    return (reply, build_attention_state(session["attention_session_id"]))


# Formats a (main category, subcategory) pair as "🟦 <b>work : deep_work</b>" — emoji
# prefix keyed by main category, plus the bolded "main : sub" label with space-colon-space
# separator. Underscores in the main category are converted to spaces for display (e.g.
# self_care → "self care"); subcategory is shown verbatim with underscores preserved
# (matches the mockup pills).
def _format_category_label(category: str, subcategory: str) -> str:
    emoji = _CATEGORY_EMOJI.get(category, "⬜")
    main_label = escape(category.replace("_", " "))
    sub_label = escape(subcategory)
    return f"{emoji} <b>{main_label} : {sub_label}</b>"


# Extracts co-category markers from a notes field. A co-category marker is a line of
# the form "+ main_category : subcategory" (e.g. "+ social : social_in_person").
# Legacy "/" separator is still accepted by the regex for backward compat with rows
# written before the v3 separator change. Multiple lines allowed. Only valid taxonomy
# pairs are returned — bogus markers are silently dropped.
# Inputs: notes text (or None for no notes).
# Outputs: list of (category, subcategory) tuples, in source-order.
def _parse_co_categories(notes: str | None) -> list[tuple[str, str]]:
    if not notes:
        return []
    out: list[tuple[str, str]] = []
    for match in _CO_CATEGORY_RE.finditer(notes):
        pair = (match.group(1), match.group(2))
        if pair in _VALID_PAIRS:
            out.append(pair)
    return out


# Removes a "+ category / subcategory" marker line from notes. Used by the correction
# handler when B asks to remove a co-categorisation. Idempotent — calling on notes that
# don't contain the marker returns the input unchanged. Returns None when removal leaves
# notes empty/whitespace so the DB column flips back to NULL (cleaner than empty string).
# Matches on the structured marker only — free-form note lines that happen to start with
# "+ " are not stripped unless they match _CO_CATEGORY_RE shape exactly.
def _strip_co_category_marker(notes: str | None, category: str, subcategory: str) -> str | None:
    if not notes:
        return None
    kept: list[str] = []
    for line in notes.split("\n"):
        match = _CO_CATEGORY_RE.match(line)
        if match and match.group(1) == category and match.group(2) == subcategory:
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    return cleaned if cleaned else None


# Sums durations (in whole minutes) of CLOSED b.attention_sessions rows that share the
# given main category and whose ended_at falls within the UTC half-open range
# [day_start_utc, day_end_utc). Used to render "today · 1h 12m work" in the footer of
# end blocks. Opens its own connection (same pattern as get_timezone). Returns 0 on
# any DB failure — degraded fallback so the reply still sends even if the count is missing.
#
# Midnight-spanning semantics: each session is attributed in full to its ended_at day;
# durations are NEVER split across day boundaries. A 3-hour deep-work block that started
# at 11pm and ended at 2am counts entirely in the "ended_at" day's total. This matches
# the wake-day anchoring used by the caller — totals attach to the day in which work
# concluded, not the day it began.
def _get_daily_total_minutes(category: str, day_start_utc: datetime, day_end_utc: datetime) -> int:
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (ended_at - started_at)) / 60.0), 0)::int
                        FROM b.attention_sessions
                        WHERE category = %s
                          AND ended_at IS NOT NULL
                          AND ended_at >= %s
                          AND ended_at < %s
                        """,
                        (category, day_start_utc, day_end_utc),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row and row[0] is not None else 0
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "attention_daily_total_failed",
            e,
            category=category,
            day_start_utc=day_start_utc.isoformat(),
        )
        return 0


# Sums minutes per MAIN category that CLOSED b.attention_sessions spend INSIDE the half-open
# window [window_start, window_end) — each session clipped to its overlap with the window via
# LEAST/GREATEST, never counted beyond it. Powers the "Today so far · awake" ledger in
# /attention_status, where the totals must be a true partition of waking time so far: clipping
# keeps every category's share ≤ 100% and the untracked remainder ≥ 0.
#
# NOTE: this deliberately differs from _get_daily_total_minutes (the end-block footer), which
# attributes each session's FULL duration to its ended_at day. The footer answers "total time
# in this category today"; this answers "share of the waking window so far" — so a session
# straddling the wake anchor (e.g. 8–10am with a 9am wake) contributes only its in-window slice
# here, not its whole length. Callers should pass window_end = min(now, day_end).
#
# Inputs: an OPEN DB cursor (caller owns the connection/transaction) and the UTC window bounds.
# Outputs: {main_category: minutes} containing only categories with in-window time; categories
# with none are absent (so the caller renders non-zero rows only).
def _get_window_category_minutes(cur, window_start: datetime, window_end: datetime) -> dict[str, int]:
    cur.execute(
        """
        SELECT category,
               SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                   LEAST(ended_at, %(win_end)s) - GREATEST(started_at, %(win_start)s)
               )) / 60.0))::int AS minutes
        FROM b.attention_sessions
        WHERE ended_at IS NOT NULL
          AND ended_at   > %(win_start)s
          AND started_at < %(win_end)s
        GROUP BY category
        """,
        {"win_start": window_start, "win_end": window_end},
    )
    return {row[0]: int(row[1]) for row in cur.fetchall() if row[1] is not None}


# Returns (day_start_utc, day_end_utc) for the "wake-day" total window anchored to
# B's most recent morning-wake at or before `anchor_utc`. A morning wake is a wake
# row whose immediately-preceding sleep_wake_events row is a sleep (filters nap-end
# wakes). Window is [wake, wake + 24h). Fallback when no qualifying wake in the
# last 24h: local 4am cutoff (today's 4am if anchor is after 4am, else yesterday's).
# Read-only — never inserts. Auto-insert lives in _ensure_morning_wake_logged.
def _get_wake_day_window_utc(anchor_utc: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    wake_utc = _get_most_recent_morning_wake_utc(anchor_utc)
    if wake_utc is None:
        anchor_local = anchor_utc.astimezone(tz)
        four_am_today = anchor_local.replace(hour=4, minute=0, second=0, microsecond=0)
        if anchor_local < four_am_today:
            four_am_today -= timedelta(days=1)
        day_start_utc = four_am_today.astimezone(timezone.utc)
    else:
        day_start_utc = wake_utc
    return (day_start_utc, day_start_utc + timedelta(days=1))


# Looks up the most recent "morning wake" in b.sleep_wake_events at or before
# anchor_utc, within a 24h lookback window. A morning wake = wake row whose
# immediately preceding row (by occurred_at, across all rows) is a sleep — so
# nap-end wakes (which write a wake without a preceding sleep) are excluded.
# Returns the wake's occurred_at as UTC, or None when nothing qualifies. Returns
# None on any DB failure (degraded fallback so the footer still renders).
def _get_most_recent_morning_wake_utc(anchor_utc: datetime) -> datetime | None:
    lookback_start = anchor_utc - timedelta(hours=24)
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT w.occurred_at
                        FROM b.sleep_wake_events w
                        WHERE w.event_type = 'wake'
                          AND w.occurred_at >= %s
                          AND w.occurred_at <= %s
                          AND (
                            SELECT s.event_type
                            FROM b.sleep_wake_events s
                            WHERE s.occurred_at < w.occurred_at
                            ORDER BY s.occurred_at DESC
                            LIMIT 1
                          ) = 'sleep'
                        ORDER BY w.occurred_at DESC
                        LIMIT 1
                        """,
                        (lookback_start, anchor_utc),
                    )
                    row = cur.fetchone()
                    return row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "attention_morning_wake_lookup_failed",
            e,
            anchor_utc=anchor_utc.isoformat(),
        )
        return None


# Formats a timezone-aware datetime as 12-hour "9:54 AM" / "12:43 PM".
# Inputs: a tz-aware datetime already converted to the display timezone.
# Outputs: zero-stripped hour, two-digit minute, uppercase meridiem.
def _format_time_12h(dt_local: datetime) -> str:
    hour = dt_local.hour % 12 or 12
    meridiem = "AM" if dt_local.hour < 12 else "PM"
    return f"{hour}:{dt_local.minute:02d} {meridiem}"


# Formats the date prefix shown in the "today · 14 min cooking" footer line.
# Inputs: the session event's local datetime and "now" in the same local timezone.
# Outputs:
#   "today" / "yesterday" — same/previous calendar day
#   "Mon" / "Tue" / ...   — within the last week (2-6 days back)
#   "24 May" / "3 Jan"    — older than a week
# Future-dated events fall through to the date format.
def _format_date_footer(event_local: datetime, now_local: datetime) -> str:
    delta_days = (now_local.date() - event_local.date()).days
    if delta_days == 0:
        return "Today"
    if delta_days == 1:
        return "Yesterday"
    if 2 <= delta_days <= 6:
        return event_local.strftime("%a")  # Mon/Tue/... already capitalised
    # %-d strips the leading zero on day-of-month; Linux/macOS only — Cloud Run is Linux.
    return event_local.strftime("%-d %b")


# Computes elapsed time between two timestamps in whole minutes (rounded), clamped at 0.
# Inputs: start and end timestamps.
# Outputs: minutes as int.
def _duration_minutes(started_at: datetime, ended_at: datetime) -> int:
    return max(0, round((ended_at - started_at).total_seconds() / 60))


# Formats an integer minute count as "1h 35m" / "45m" / "2h".
# Inputs: whole minutes.
# Outputs: compact short-form duration string.
def _format_duration_short(total_minutes: int) -> str:
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"




# Formats the open session for the LLM prompt.
# Inputs: current open session dict or None.
# Outputs: concise text representation.
def _format_open_session_for_llm(open_session: dict | None) -> str:
    if open_session is None:
        return "None"
    parts = [
        f"id={open_session['attention_session_id']}",
        f"category={open_session['category']}",
        f"subcategory={open_session.get('subcategory')}",
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


# Parses an optional ISO timestamp emitted by the LLM.
# Inputs: ISO string with timezone, None, or an empty-ish string.
# Outputs: timezone-aware datetime or None.
def _parse_optional_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp must include timezone")
        return value
    cleaned = str(value).strip()
    if not cleaned or cleaned.lower() in {"null", "none", "open"}:
        return None
    parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed


# Returns B's local time at the given event timestamp for the LLM prompt.
# Inputs: event timestamp or None.
# Outputs: readable time string.
def _local_time_str(as_of: datetime | None = None) -> str:
    tz = get_timezone(as_of)
    if as_of is not None:
        local_now = as_of.astimezone(tz)
    else:
        local_now = datetime.now(tz=tz)
    return local_now.strftime("%Y-%m-%d %H:%M %Z")


# Strips markdown code fences if the LLM wraps its response, then parses JSON.
# Inputs: raw model response.
# Outputs: parsed JSON object.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    return json.loads(cleaned)
