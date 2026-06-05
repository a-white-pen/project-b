"""
Aligner (Invisalign) logging domain — handles the IN/OUT keyboard taps and tray changes.

B tracks two things: time the aligners are OUT of the mouth (so worn-hours/day is derivable;
dentist target 22h worn = ≤2h out) and which tray each arch is on. Modelled to mirror the
attention domain: an open b.aligner_wear_events row (reinserted_at NULL) means "currently out",
guarded by a one-open invariant; b.aligner_tray_changes holds one row per (arch, tray) with
ended_at auto-set when the next tray starts.

Capture is a persistent Telegram reply keyboard with two buttons docked above the on-screen
keyboard — "🦷 IN" / "🍽️ OUT". Taps arrive as plain TEXT messages whose exact strings the
router maps deterministically to handle_aligner_in / handle_aligner_out (recording taps, not
commands). Throughout B's free-text, "aligners", "Invisalign", and "trays" are interchangeable.

Tray changes are initiated by quoting an ALIGNERS IN/OUT reply with the new tray number
("upper tray 8 now"). b.aligner_tray_changes is the single source of truth; the wear-event
upper/lower_tray_number columns are a DERIVED cache (tray active as-of removed_at) recomputed
from the timeline (_recompute_wear_snapshots) — never set directly, so they can't diverge.
The correction handler upserts the timeline (switch anchored at reinserted_at for an IN-quote,
removed_at for an OUT-quote) and re-stitches the chain. Free-text outside a quoted reply is
NOT routed here — that bloat went away.

OUT/IN replies follow B's mockup: a header with emoji + state + bold time, then per-arch
"upper tray N · day D / planned" lines, then either "aim to re-insert by HH:MM" (OUT,
removed_at + _OFF_BUDGET) or "off for Xm" (IN), and finally "aligners on for Xh Ym in the
last 24h" — a rolling-window figure derived from _get_aligners_on_minutes_24h.

All handlers return a list of (reply_text, pending_state, reply_markup) triples. The third
element re-asserts the persistent keyboard on every aligner reply so it stays docked (sending
the first aligner reply is also what first summons it). pending_state powers quoted-reply
corrections, exactly like the attention domain.

Public functions:
  render_keyboard()              — the persistent ReplyKeyboardMarkup dict (the two buttons)
  handle_aligner_out(msg)        — 🍽️ OUT tap: opens a wear event, snapshotting current trays
  handle_aligner_in(msg)         — 🦷 IN tap: closes the open wear event
  handle_aligner_status(msg)     — /aligner_status: current state + duration + trays; bootstraps the keyboard
  build_aligner_wear_state(id, kind)  — conversation_state for a wear-event reply (correction scoping)
  build_aligner_tray_state(id)        — conversation_state for a tray-change reply
  format_wear_event(verb, event) — renders a wear event for correction confirmations (imported by correction.py)
  format_tray(verb, tray)        — renders a tray row for correction confirmations (imported by correction.py)

Shared helpers imported by correction.py:
  _kb(reply, state)                          — wraps (reply, state) with the persistent keyboard
  _lock_wear_writes(cur) / _lock_tray_writes(cur) — advisory locks serialising wear / tray writes
  _restitch_arch_chain(cur, arch)            — re-links an arch's tray chain; returns rows rewritten
  _row_to_wear_event(row) / _row_to_tray(row) — DB row → dict mappers
  _format_tray_change(arch, new, prior)      — the "NEW … TRAY" reply (also for spawned-tray replies)
  _parse_json(raw)                           — strips code fences and json.loads

Internal — wear-event handlers/DB:
  _get_all_open_wear_events / _get_last_closed_wear_event / _insert_wear_event /
  _close_wear_event — wear-event reads/writes (open-session sweep, snapshot insert, close)
  _get_aligners_on_minutes_24h(cur, anchor) — rolling-24h on-time for the OUT/IN footer

Internal — tray DB:
  _get_trays_asof(cur, as_of)                — tray row per arch active as-of a timestamp
  _recompute_wear_snapshots(cur, arch)       — refresh wear tray-number cache from the timeline
  _format_tray_lines / _tray_line / _format_status / _format_out / _format_in /
  _format_out_guard / _format_in_guard       — reply formatters

Helpers: _format_time_12h, _format_datetime, _day_of_tray (0-based), _duration_minutes,
  _format_duration_short

Constants:
  BUTTON_IN_TEXT / BUTTON_OUT_TEXT — exact button strings the router matches deterministically
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from html import escape

import psycopg2.extras

from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage
from system.timezone import get_timezone

logger = logging.getLogger(__name__)

# Exact button labels. Single source of truth — the router imports these to match taps,
# and render_keyboard() lays them out. IN on the left (tooth — wearing them), OUT on the
# right (plate/utensils — most removals are for eating).
BUTTON_IN_TEXT = "🦷 IN"
BUTTON_OUT_TEXT = "🍽️ OUT"

# Two emoji constants drive the reply-side glyphs and stay synced with the button labels:
# _EMOJI is the general "aligners" mark (IN reply, tray reply, guards, corrections);
# _OUT_EMOJI is the OUT-specific mark used only in the OUT reply header and OUT guard.
_EMOJI = "🦷"
_OUT_EMOJI = "🍽️"

# Dentist's target = 22h worn / day -> 2h off-budget per day. Used to render the
# "aim to re-insert by HH:MM" line on every OUT reply (removed_at + this budget).
_OFF_BUDGET = timedelta(hours=2)

_VALID_ARCHES = ("upper", "lower")
# Minutes in 24 hours — sentinel for the "aligners on for X in the last 24h" math
# (computed as MINUTES_PER_DAY minus summed off-time in the rolling window).
_MINUTES_PER_DAY = 24 * 60

# Canonical SELECT/RETURNING column order for each table — the SINGLE source of truth that
# _row_to_wear_event / _row_to_tray index positionally. Every query in this module and in
# correction.py (which imports these) must use them, so a column reorder can't silently break
# the row mappers. correction.py also appends ", updated_at" for its optimistic version check.
_WEAR_COLS = (
    "aligner_wear_event_id, removed_at, reinserted_at, "
    "upper_tray_number, lower_tray_number, notes, meta"
)
_TRAY_COLS = (
    "aligner_tray_change_id, arch, tray_number, planned_days, "
    "started_at, ended_at, notes, meta"
)


# Returns the persistent reply keyboard dict — two buttons docked above the on-screen
# keyboard. is_persistent keeps it visible even after the user collapses it; once sent it
# stays until replaced, so re-attaching it on every aligner reply is harmless and keeps it
# docked (and is what first summons it when B logs their opening trays).
def render_keyboard() -> dict:
    return {
        "keyboard": [[{"text": BUTTON_IN_TEXT}, {"text": BUTTON_OUT_TEXT}]],
        "is_persistent": True,
        "resize_keyboard": True,
    }


# Wraps a (reply, state) pair with the persistent keyboard as the third tuple element.
def _kb(reply: str, state: dict | None) -> tuple[str, dict | None, dict]:
    return (reply, state, render_keyboard())


# ── Wear events (time out of mouth) ────────────────────────────────────────────────────

# Handles the "🍽️ OUT" tap — opens a wear event (aligners came out of the mouth),
# snapshotting the current upper/lower tray numbers onto the row. If one is already open,
# B missed an "in": we don't corrupt the data, we flag it and offer a quoted-reply fix.
#
# The reply also shows day-N-of-current-tray plus a rolling 24h on-time figure; we capture
# the current tray rows (with started_at + planned_days) and the 24h on-time inside the
# same DB transaction so the figures reflect the freshly inserted state.
def handle_aligner_out(msg: InboundMessage) -> list[tuple[str, dict | None, dict]]:
    log_event(logger, logging.INFO, "aligner_out_received", update_id=msg.update_id)
    removed_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    guard_event: dict | None = None
    overlap_event: dict | None = None
    new_event: dict | None = None
    tray_rows: dict[str, dict | None] = {"upper": None, "lower": None}
    on_minutes_24h = 0
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    _lock_wear_writes(cur)
                    open_events = _get_all_open_wear_events(cur, for_update=True)
                    if open_events:
                        guard_event = open_events[0]
                    else:
                        # Overlap guard for a backdated / out-of-order OUT: a new open event
                        # [removed_at, ∞) would overlap any existing event ending after removed_at
                        # (double-counting off-time). A normal now-OUT never trips this (all past
                        # events ended before now); with no open events, a match is a CLOSED event
                        # whose reinserted_at is after removed_at.
                        cur.execute(
                            "SELECT removed_at, reinserted_at FROM b.aligner_wear_events "
                            "WHERE COALESCE(reinserted_at, 'infinity'::timestamptz) > %s "
                            "ORDER BY removed_at LIMIT 1 FOR UPDATE",
                            (removed_at,),
                        )
                        overlap_row = cur.fetchone()
                        if overlap_row is not None:
                            overlap_event = {"removed_at": overlap_row[0], "reinserted_at": overlap_row[1]}
                        else:
                            # Snapshot the tray active AS-OF removed_at (point-in-time, from the
                            # timeline) — not "currently open" — so a backdated/out-of-order OUT or
                            # a concurrent tray change can't record a tray inconsistent with removed_at.
                            tray_rows = _get_trays_asof(cur, removed_at)
                            new_event = _insert_wear_event(
                                cur=cur,
                                removed_at=removed_at,
                                upper_tray_number=(tray_rows["upper"] or {}).get("tray_number"),
                                lower_tray_number=(tray_rows["lower"] or {}).get("tray_number"),
                                meta={
                                    "start": {
                                        "source": "telegram",
                                        "self_reported": True,
                                        "telegram_update_id": msg.update_id,
                                    }
                                },
                            )
                            # Computed AT removed_at: the just-inserted open event contributes 0
                            # off-minutes to the window (its duration so far is zero), so this
                            # figure is "your previous 24h were Xh on" at the moment of removal.
                            on_minutes_24h = _get_aligners_on_minutes_24h(cur, removed_at)
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_out_save_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't log that the aligners came out — try again.", None)]

    if guard_event is not None:
        log_event(
            logger,
            logging.INFO,
            "aligner_out_already_open",
            update_id=msg.update_id,
            open_event_id=guard_event["aligner_wear_event_id"],
        )
        return [_kb(
            _format_out_guard(guard_event, removed_at),
            build_aligner_wear_state(guard_event["aligner_wear_event_id"], "out_guard"),
        )]

    if overlap_event is not None:
        log_event(logger, logging.INFO, "aligner_out_rejected_overlap", update_id=msg.update_id)
        tz = get_timezone(overlap_event["removed_at"])
        start = _format_time_12h(overlap_event["removed_at"].astimezone(tz))
        end = (_format_time_12h(overlap_event["reinserted_at"].astimezone(tz))
               if overlap_event["reinserted_at"] is not None else "open")
        return [_kb(
            f"⚠️ That OUT time overlaps a logged wear event ({escape(start)}–{escape(end)}) — "
            f"not logged. Fix that event's times first.",
            None,
        )]

    log_event(
        logger,
        logging.INFO,
        "aligner_out_logged",
        update_id=msg.update_id,
        wear_event_id=new_event["aligner_wear_event_id"],
        upper_tray_number=new_event["upper_tray_number"],
        lower_tray_number=new_event["lower_tray_number"],
        on_minutes_24h=on_minutes_24h,
    )
    return [_kb(
        _format_out(new_event, tray_rows, on_minutes_24h),
        build_aligner_wear_state(new_event["aligner_wear_event_id"], "out"),
    )]


# Handles the "🦷 IN" tap — closes the open wear event (aligners back in the mouth).
# No open event means B is already wearing: harmless confirm, nothing to close.
#
# The reply mirrors the OUT shape: per-arch tray + day count, an "off for X" line for the
# just-closed event, and a rolling 24h on-time figure. Tray rows and on-time are captured
# inside the same transaction so they reflect the post-close state.
def handle_aligner_in(msg: InboundMessage) -> list[tuple[str, dict | None, dict]]:
    log_event(logger, logging.INFO, "aligner_in_received", update_id=msg.update_id)
    reinserted_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    no_open = False
    bad_timing = False
    last_closed: dict | None = None
    closed_event: dict | None = None
    tray_rows: dict[str, dict | None] = {"upper": None, "lower": None}
    on_minutes_24h = 0
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    _lock_wear_writes(cur)
                    open_events = _get_all_open_wear_events(cur, for_update=True)
                    if not open_events:
                        no_open = True
                        last_closed = _get_last_closed_wear_event(cur)
                    else:
                        # Reject closes that would make reinserted_at land at/before removal
                        # (negative-duration row). Mirrors the attention finish guard.
                        closeable = [e for e in open_events if e["removed_at"] < reinserted_at]
                        skipped = [e for e in open_events if e["removed_at"] >= reinserted_at]
                        if skipped:
                            log_event(
                                logger,
                                logging.WARNING,
                                "aligner_in_blocked_by_bad_timing",
                                update_id=msg.update_id,
                                blocking_event_ids=[e["aligner_wear_event_id"] for e in skipped],
                            )
                        if not closeable:
                            bad_timing = True
                        else:
                            # Close all closeable (normally one). The newest drives the reply;
                            # any stale extras are swept silently with a system-source meta.end.
                            closed_events = [
                                _close_wear_event(
                                    cur=cur,
                                    event=event,
                                    reinserted_at=reinserted_at,
                                    end_meta={
                                        "source": "telegram" if i == 0 else "system",
                                        "self_reported": i == 0,
                                        "reason": "explicit_in" if i == 0 else "stale_open_swept_on_in",
                                        "telegram_update_id": msg.update_id,
                                    },
                                )
                                for i, event in enumerate(closeable)
                            ]
                            closed_event = closed_events[0]
                            # Display the tray active AS-OF removal (the event's snapshot moment),
                            # consistent with the OUT reply; a tray switch B logs at reinsertion
                            # appears via the follow-up new-tray reply, not by mutating this event.
                            tray_rows = _get_trays_asof(cur, closed_event["removed_at"])
                            # Computed AT reinserted_at: the just-closed event contributes
                            # its full duration to off-time, so the figure reflects the new
                            # state (including the period just ended).
                            on_minutes_24h = _get_aligners_on_minutes_24h(cur, reinserted_at)
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_in_save_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't log that the aligners went back in — try again.", None)]

    if no_open:
        log_event(logger, logging.INFO, "aligner_in_nothing_open", update_id=msg.update_id)
        return [_kb(_format_in_guard(last_closed), None)]
    if bad_timing:
        # Also covers a same-second OUT→IN double-tap. Both recovery paths go through the OUT
        # reply (which carries the wear-event state) — this guard reply has state=None, so it
        # must NOT tell B to "reply delete" here (there'd be nothing to act on).
        return [_kb(
            "That 'in' time lands at or before the aligners came out — left it alone. "
            "Quote the 'out' message to fix its time, or quote it with \"delete\" if that OUT "
            "was a mistake.",
            None,
        )]

    log_event(
        logger,
        logging.INFO,
        "aligner_in_logged",
        update_id=msg.update_id,
        wear_event_id=closed_event["aligner_wear_event_id"],
        on_minutes_24h=on_minutes_24h,
    )
    return [_kb(
        _format_in(closed_event, tray_rows, on_minutes_24h),
        build_aligner_wear_state(closed_event["aligner_wear_event_id"], "in"),
    )]


# (Tray changes are no longer initiated by free-text messages — B updates tray
# numbers by quote-correcting an ALIGNERS IN/OUT reply. The correction handler in
# domains/aligner/correction.py spawns new b.aligner_tray_changes rows when needed.
# Tray-row helpers used by that path live further down this file.)


# ── Status command ─────────────────────────────────────────────────────────────────────

# Handles the /aligner_status command — minimal status read: are the aligners IN or OUT
# right now, how long since that state began, and which trays are currently on each arch.
# Side-effect: re-attaches the persistent reply keyboard so this command doubles as the
# keyboard bootstrap for a fresh deploy (taps need the keyboard to exist; the keyboard
# only appears once attached to an aligner reply; this command is the chicken-and-egg fix).
# Inputs: an InboundMessage carrying the /aligner_status command.
# Outputs: a single (reply, None, reply_markup) — no conversation_state since /aligner_status
# is a read, not a loggable action.
def handle_aligner_status(msg: InboundMessage) -> list[tuple[str, dict | None, dict]]:
    now_utc = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    log_event(logger, logging.INFO, "aligner_status_requested", update_id=msg.update_id)
    open_event: dict | None = None
    last_closed: dict | None = None
    tray_rows: dict[str, dict | None] = {"upper": None, "lower": None}
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    open_events = _get_all_open_wear_events(cur, for_update=False)
                    open_event = open_events[0] if open_events else None
                    if open_event is None:
                        last_closed = _get_last_closed_wear_event(cur)
                    tray_rows = _get_trays_asof(cur, now_utc)  # "current" = active as-of now
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.ERROR, "aligner_status_fetch_failed", e, update_id=msg.update_id)
        return [_kb("Couldn't read the aligner state — try again.", None)]

    log_event(logger, logging.INFO, "aligner_status_sent", update_id=msg.update_id,
              currently_out=open_event is not None,
              upper_tray=(tray_rows["upper"] or {}).get("tray_number"),
              lower_tray=(tray_rows["lower"] or {}).get("tray_number"))
    return [_kb(_format_status(now_utc, open_event, last_closed, tray_rows), None)]


# ── Conversation state ─────────────────────────────────────────────────────────────────

# Builds conversation_state for a wear-event reply so quoting it scopes a correction to that
# event. kind records what the reply was ("out", "in", "out_guard", "updated") for context.
def build_aligner_wear_state(wear_event_id: int, kind: str) -> dict:
    return {
        "domain": "aligner",
        "context": {"aligner_wear_event_ids": [wear_event_id], "kind": kind},
    }


# Builds conversation_state for a tray-change reply. The tray-change id is the only context
# needed — the correction handler routes by its presence and reads arch/fields from the DB row,
# so no arch/kind is stored here (they'd be dead weight).
def build_aligner_tray_state(tray_change_id: int) -> dict:
    return {
        "domain": "aligner",
        "context": {"aligner_tray_change_ids": [tray_change_id]},
    }


# ── DB access — wear events ──────────────────────────────────────────────────────────────

# Serializes all wear-event writes via a transaction-scoped advisory lock. With the partial
# unique index one_open_aligner_wear_event this guarantees at most one open event at a time.
def _lock_wear_writes(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('b.aligner_wear_events'))")


# Fetches all open (reinserted_at IS NULL) wear events, newest removal first. Normally ≤1
# thanks to the partial unique index; the handlers sweep any extras. for_update locks rows.
def _get_all_open_wear_events(cur, for_update: bool = False) -> list[dict]:
    sql = f"""
        SELECT {_WEAR_COLS}
        FROM b.aligner_wear_events
        WHERE reinserted_at IS NULL
        ORDER BY removed_at DESC
    """
    if for_update:
        sql += " FOR UPDATE"
    cur.execute(sql)
    return [_row_to_wear_event(row) for row in cur.fetchall()]


# Most recently reinserted (closed) wear event, for the "already wearing" guard's
# "last reinserted at …" line. Returns None when no closed event exists yet.
def _get_last_closed_wear_event(cur) -> dict | None:
    cur.execute(
        f"""
        SELECT {_WEAR_COLS}
        FROM b.aligner_wear_events
        WHERE reinserted_at IS NOT NULL
        ORDER BY reinserted_at DESC
        LIMIT 1
        """
    )
    row = cur.fetchone()
    return _row_to_wear_event(row) if row else None


# Returns the full tray row ACTIVE AS-OF `as_of` per arch — {'upper': row|None, 'lower': row|None}.
# The timeline (b.aligner_tray_changes) is the single source of truth, so "the tray in use at
# time T" = the arch's row with the greatest started_at <= T (the chain is contiguous, so that
# row's interval covers T). Used to snapshot a wear event as-of its removed_at, and to render
# the OUT/IN/status replies (which need started_at for day-of-tray math + planned_days). Passing
# "now" yields the current trays. Arches with no tray started by `as_of` come back as None.
def _get_trays_asof(cur, as_of: datetime) -> dict[str, dict | None]:
    cur.execute(
        f"SELECT DISTINCT ON (arch) {_TRAY_COLS} "
        "FROM b.aligner_tray_changes WHERE started_at <= %s "
        "ORDER BY arch, started_at DESC",
        (as_of,),
    )
    out: dict[str, dict | None] = {"upper": None, "lower": None}
    for row in cur.fetchall():
        tray = _row_to_tray(row)
        if tray["arch"] in out:
            out[tray["arch"]] = tray
    return out


# Recomputes the wear-event tray-number cache for one arch from the timeline: each wear event's
# <arch>_tray_number is set to the tray active as-of its removed_at (NULL if no tray had started
# by then). The wear snapshot is DERIVED — never edited independently — so it can never diverge
# from b.aligner_tray_changes. Call after ANY tray-timeline mutation (insert/renumber/retime/
# delete) for the affected arch, inside the same lock-held transaction. Only rows whose value
# actually changes are written (IS DISTINCT FROM guard). Caller holds the wear + tray locks.
# Output: the number of wear rows whose cache changed (for logging).
def _recompute_wear_snapshots(cur, arch: str) -> int:
    col = {"upper": "upper_tray_number", "lower": "lower_tray_number"}[arch]
    cur.execute(
        f"""
        UPDATE b.aligner_wear_events w
        SET {col} = (
            SELECT t.tray_number FROM b.aligner_tray_changes t
            WHERE t.arch = %s AND t.started_at <= w.removed_at
            ORDER BY t.started_at DESC LIMIT 1
        )
        WHERE {col} IS DISTINCT FROM (
            SELECT t.tray_number FROM b.aligner_tray_changes t
            WHERE t.arch = %s AND t.started_at <= w.removed_at
            ORDER BY t.started_at DESC LIMIT 1
        )
        """,
        (arch, arch),
    )
    return cur.rowcount


# Sums aligners-on minutes in the rolling 24h window ending at anchor_utc. Computed as
# (24h - sum of off-time in window). Off-time per event is clipped to [anchor-24h, anchor];
# an open event (reinserted_at NULL) is treated as ongoing up to anchor. Caller's tx must
# already include any just-inserted/just-closed wear event so the figure reflects the state
# the reply is about. Returns 0 on a heavily-off day (never goes negative).
def _get_aligners_on_minutes_24h(cur, anchor_utc: datetime) -> int:
    cur.execute(
        """
        SELECT COALESCE(SUM(EXTRACT(EPOCH FROM (
            LEAST(COALESCE(reinserted_at, %s::timestamptz), %s::timestamptz)
            - GREATEST(removed_at, %s::timestamptz - interval '24 hours')
        )) / 60.0), 0)::int
        FROM b.aligner_wear_events
        WHERE removed_at < %s::timestamptz
          AND COALESCE(reinserted_at, 'infinity'::timestamptz)
              > %s::timestamptz - interval '24 hours'
        """,
        (anchor_utc, anchor_utc, anchor_utc, anchor_utc, anchor_utc),
    )
    off_min = cur.fetchone()[0] or 0
    return max(0, _MINUTES_PER_DAY - int(off_min))


# Inserts an open wear event (reinserted_at left NULL) with the tray snapshot + start meta.
def _insert_wear_event(
    cur,
    removed_at: datetime,
    upper_tray_number: int | None,
    lower_tray_number: int | None,
    meta: dict,
) -> dict:
    cur.execute(
        f"""
        INSERT INTO b.aligner_wear_events
            (removed_at, upper_tray_number, lower_tray_number, meta)
        VALUES (%s, %s, %s, %s)
        RETURNING {_WEAR_COLS}
        """,
        (removed_at, upper_tray_number, lower_tray_number, psycopg2.extras.Json(meta)),
    )
    return _row_to_wear_event(cur.fetchone())


# Closes a wear event: sets reinserted_at, merges end provenance into meta, stamps updated_at.
def _close_wear_event(cur, event: dict, reinserted_at: datetime, end_meta: dict) -> dict:
    meta = dict(event.get("meta") or {})
    meta["end"] = end_meta
    cur.execute(
        f"""
        UPDATE b.aligner_wear_events
        SET reinserted_at = %s,
            meta = %s,
            updated_at = now()
        WHERE aligner_wear_event_id = %s
        RETURNING {_WEAR_COLS}
        """,
        (reinserted_at, psycopg2.extras.Json(meta), event["aligner_wear_event_id"]),
    )
    return _row_to_wear_event(cur.fetchone())


# Maps a b.aligner_wear_events row tuple (the _WEAR column order used throughout this
# module and correction.py) to the dict shape the handlers/formatters consume.
def _row_to_wear_event(row) -> dict:
    return {
        "aligner_wear_event_id": row[0],
        "removed_at": row[1],
        "reinserted_at": row[2],
        "upper_tray_number": row[3],
        "lower_tray_number": row[4],
        "notes": row[5],
        "meta": row[6] or {},
    }


# ── DB access — tray changes ─────────────────────────────────────────────────────────────

# Serializes all tray-change writes via a transaction-scoped advisory lock, so the
# close-prior-then-insert sequence (and corrections that re-stitch the chain) can't race.
def _lock_tray_writes(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('b.aligner_tray_changes'))")


# Maps a b.aligner_tray_changes row tuple (the _TRAY column order) to a dict.
def _row_to_tray(row) -> dict:
    return {
        "aligner_tray_change_id": row[0],
        "arch": row[1],
        "tray_number": row[2],
        "planned_days": row[3],
        "started_at": row[4],
        "ended_at": row[5],
        "notes": row[6],
        "meta": row[7] or {},
    }


# Re-links one arch's tray rows into a contiguous chain. Orders all rows for the arch by
# started_at and sets each row's ended_at to the next row's started_at, leaving the latest
# row open (ended_at NULL = current tray). Used by correction.py after a tray's started_at
# is edited or a tray row is deleted — operations that would otherwise leave an overlap, a
# gap, or no current tray. Only writes rows whose ended_at actually changes. Caller must
# hold the tray advisory lock (see _lock_tray_writes); inputs come from the DB.
# Output: the number of rows whose ended_at was rewritten (so callers can log the repair).
def _restitch_arch_chain(cur, arch: str) -> int:
    cur.execute(
        "SELECT aligner_tray_change_id, started_at, ended_at FROM b.aligner_tray_changes "
        "WHERE arch = %s ORDER BY started_at, aligner_tray_change_id",
        (arch,),
    )
    rows = cur.fetchall()
    rewritten = 0
    for i, (tray_id, _started_at, ended_at) in enumerate(rows):
        new_ended = rows[i + 1][1] if i + 1 < len(rows) else None
        if new_ended != ended_at:
            cur.execute(
                "UPDATE b.aligner_tray_changes SET ended_at = %s, updated_at = now() "
                "WHERE aligner_tray_change_id = %s",
                (new_ended, tray_id),
            )
            rewritten += 1
    return rewritten


# ── Rendering ────────────────────────────────────────────────────────────────────────────
#
# OUT/IN replies are flat (no <blockquote>) and follow B's mockup:
#
#   🍽️ ALIGNERS OUT · 8:58 PM
#
#   upper tray 7 · day 5 / 14
#   lower tray 7 · day 5 / 14
#
#   aim to re-insert by 10:58 PM        ← OUT only (removed_at + _OFF_BUDGET)
#   off for 16m                          ← IN only (just-closed event duration)
#   aligners on for 20h 40m in the last 24h
#
# Dynamic values are bolded; static labels are plain. All user-derived text is html-escaped
# per the contract in telegram/replies.py (no user free-text reaches these formatters anyway —
# everything here is numbers/timestamps from our DB rows).


# OUT reply: aligners just came out. anchor = removed_at, on_minutes_24h computed AT removal
# (the new open event contributes 0 to the window).
def _format_out(event: dict, trays: dict, on_minutes_24h: int) -> str:
    tz = get_timezone(event["removed_at"])
    removed_local = event["removed_at"].astimezone(tz)
    aim_local = (event["removed_at"] + _OFF_BUDGET).astimezone(tz)
    return "\n".join([
        f"{_OUT_EMOJI} <b>ALIGNERS OUT</b> · <b>{escape(_format_time_12h(removed_local))}</b>",
        "",
        _format_tray_lines(trays, anchor_utc=event["removed_at"], tz=tz),
        "",
        f"aim to re-insert by <b>{escape(_format_time_12h(aim_local))}</b>",
        f"aligners on for <b>{escape(_format_duration_short(on_minutes_24h))}</b> in the last 24h",
    ])


# OUT-guard reply: B tapped OUT but an event is already open. Per B's design this tap is a
# pure no-op — nothing is recorded; we just say "already out". The message is deliberately
# honest that the tap wasn't logged (so B isn't surprised the second OUT didn't register).
# The reply still carries the open event's correction state, so if B actually missed an IN
# earlier they can quote this with the reinsert time to close the open event (then tap OUT
# again if they're out now). We do NOT auto-recreate a second OUT — that would require
# guessing the missed IN time and risks corrupting wear totals.
def _format_out_guard(event: dict, attempted_at: datetime) -> str:
    tz = get_timezone(event["removed_at"])
    time_str = _format_time_12h(event["removed_at"].astimezone(tz))
    dur = _format_duration_short(_duration_minutes(event["removed_at"], attempted_at))
    return (
        f"{_OUT_EMOJI} Already <b>OUT</b> since <b>{escape(time_str)}</b> "
        f"({escape(dur)} ago) — this tap wasn't logged.\n"
        "Missed an IN earlier? Quote this with the time you put them back in "
        "(then tap OUT again if you're out now)."
    )


# IN reply: aligners just went back in. anchor = reinserted_at, on_minutes_24h reflects the
# just-closed event (which contributes its full duration to off-time).
def _format_in(event: dict, trays: dict, on_minutes_24h: int) -> str:
    tz = get_timezone(event["reinserted_at"])
    reinserted_local = event["reinserted_at"].astimezone(tz)
    off_dur = _format_duration_short(_duration_minutes(event["removed_at"], event["reinserted_at"]))
    return "\n".join([
        f"{_EMOJI} <b>ALIGNERS IN</b> · <b>{escape(_format_time_12h(reinserted_local))}</b>",
        "",
        _format_tray_lines(trays, anchor_utc=event["reinserted_at"], tz=tz),
        "",
        f"off for <b>{escape(off_dur)}</b>",
        f"aligners on for <b>{escape(_format_duration_short(on_minutes_24h))}</b> in the last 24h",
    ])


# IN-guard reply: B tapped IN but nothing is open. Tiny, non-alarming.
def _format_in_guard(last_closed: dict | None) -> str:
    msg = f"{_EMOJI} Already wearing — nothing to close."
    if last_closed is not None and last_closed.get("reinserted_at") is not None:
        tz = get_timezone(last_closed["reinserted_at"])
        time_str = _format_time_12h(last_closed["reinserted_at"].astimezone(tz))
        msg += f" Last reinserted at <b>{escape(time_str)}</b>."
    return msg


# /aligner_status reply. Minimal scope by B's spec: state + duration + current trays per arch.
# Inputs: now_utc (the moment the command arrived); open_event (the open wear-event row or
# None if currently wearing); last_closed (most recent closed wear-event row, for the IN-side
# "since" timestamp; None on a brand-new account); trays (current open tray row per arch).
#
# Output shape:
#   🍽️ ALIGNERS OUT · since 8:58 PM — 38m       ← when open_event is not None
#   🦷 ALIGNERS IN · since 9:14 PM — 1h 23m      ← when no open event AND last_closed exists
#   🦷 ALIGNERS IN · no transitions logged yet  ← fresh account fallback
#
#   upper tray 7 · lower tray 7                  ← tray line (or "no tray logged yet")
def _format_status(
    now_utc: datetime,
    open_event: dict | None,
    last_closed: dict | None,
    trays: dict,
) -> str:
    if open_event is not None:
        since_utc = open_event["removed_at"]
        tz = get_timezone(since_utc)
        since_local = since_utc.astimezone(tz)
        dur = _format_duration_short(_duration_minutes(since_utc, now_utc))
        header = (
            f"{_OUT_EMOJI} <b>ALIGNERS OUT</b> · since "
            f"<b>{escape(_format_time_12h(since_local))}</b> — <b>{escape(dur)}</b>"
        )
    elif last_closed is not None and last_closed.get("reinserted_at") is not None:
        since_utc = last_closed["reinserted_at"]
        tz = get_timezone(since_utc)
        since_local = since_utc.astimezone(tz)
        dur = _format_duration_short(_duration_minutes(since_utc, now_utc))
        header = (
            f"{_EMOJI} <b>ALIGNERS IN</b> · since "
            f"<b>{escape(_format_time_12h(since_local))}</b> — <b>{escape(dur)}</b>"
        )
    else:
        # Brand-new account: never tapped IN/OUT. Skip the duration line.
        header = f"{_EMOJI} <b>ALIGNERS IN</b> · no transitions logged yet"

    # Trays: compact one-line "upper tray N · lower tray N" using _tray_line (the integer-
    # only variant). Status is intentionally lean — no day-count or 24h math (use OUT/IN tap
    # if B wants those numbers).
    upper_num = trays["upper"]["tray_number"] if trays.get("upper") else None
    lower_num = trays["lower"]["tray_number"] if trays.get("lower") else None
    return f"{header}\n\n{escape(_tray_line(upper_num, lower_num))}"


# Renders the per-arch "upper tray N · day D / planned" lines used by the OUT and IN replies.
# Skips an arch with no logged tray (e.g. early in treatment when only upper is started); when
# planned_days is NULL drops the "/ planned" suffix so the line degrades to "day D".
def _format_tray_lines(trays: dict, anchor_utc: datetime, tz) -> str:
    rendered: list[str] = []
    for arch in _VALID_ARCHES:
        tray = trays.get(arch)
        if tray is None:
            continue
        day = _day_of_tray(tray["started_at"], anchor_utc, tz)
        planned = tray.get("planned_days")
        day_label = f"<b>{day} / {planned}</b>" if planned else f"<b>{day}</b>"
        rendered.append(f"{escape(arch)} tray <b>{tray['tray_number']}</b> · day {day_label}")
    if not rendered:
        return "no tray logged yet"
    return "\n".join(rendered)


# Tray-change reply: B advanced to a new tray. Matches OUT/IN style (flat, bold values).
def _format_tray_change(arch: str, new_row: dict, prior: dict | None) -> str:
    tz = get_timezone(new_row["started_at"])
    started_str = _format_datetime(new_row["started_at"].astimezone(tz))
    arch_label = escape(arch)
    planned = new_row.get("planned_days")
    planned_suffix = f" · plan <b>{planned} days</b>" if planned else ""
    if prior is not None:
        worn_days = max(0, (new_row["started_at"] - prior["started_at"]).days)
        return "\n".join([
            f"{_EMOJI} <b>NEW {arch_label.upper()} TRAY</b> · <b>{escape(started_str)}</b>",
            "",
            f"{arch_label} tray <b>{prior['tray_number']}</b> → "
            f"<b>{new_row['tray_number']}</b>{planned_suffix}",
            f"previous tray worn <b>{worn_days} day{'s' if worn_days != 1 else ''}</b>",
        ])
    return "\n".join([
        f"{_EMOJI} <b>{arch_label.upper()} TRAY {new_row['tray_number']}</b>"
        f" · <b>{escape(started_str)}</b>",
        "",
        f"started{planned_suffix}",
    ])


# Renders a wear event for correction confirmations. verb is "updated" or "removed".
def format_wear_event(verb: str, event: dict) -> str:
    if verb == "removed":
        return f"{_EMOJI} <b>aligner wear {escape(verb)}</b>"
    removed_at = event["removed_at"]
    reinserted_at = event.get("reinserted_at")
    tz = get_timezone(reinserted_at or removed_at)
    removed_str = _format_time_12h(removed_at.astimezone(tz))
    if reinserted_at is None:
        time_line = f"out since {escape(removed_str)}"
    else:
        reinserted_str = _format_time_12h(reinserted_at.astimezone(tz))
        dur = _format_duration_short(_duration_minutes(removed_at, reinserted_at))
        time_line = f"out {escape(removed_str)} → {escape(reinserted_str)} · {escape(dur)}"
    body = "\n".join([
        time_line,
        escape(_tray_line(event["upper_tray_number"], event["lower_tray_number"])),
    ])
    return f"{_EMOJI} <b>aligner wear {escape(verb)}</b>\n<blockquote>{body}</blockquote>"


# Renders a tray row for correction confirmations. verb is "updated" or "removed".
# Shows started_at + the planned-days schedule (or "plan: not set") so B can confirm a
# "14 days" correction actually saved.
def format_tray(verb: str, tray: dict) -> str:
    arch_label = escape(tray["arch"])
    if verb == "removed":
        return f"{_EMOJI} <b>{arch_label} tray {tray['tray_number']} {escape(verb)}</b>"
    tz = get_timezone(tray["started_at"])
    started_str = _format_datetime(tray["started_at"].astimezone(tz))
    planned = tray.get("planned_days")
    plan_line = f"plan: <b>{planned} days</b>" if planned else "plan: not set"
    body = f"started {escape(started_str)}\n{plan_line}"
    return (
        f"{_EMOJI} <b>{arch_label} tray {tray['tray_number']} {escape(verb)}</b>"
        f"\n<blockquote>{body}</blockquote>"
    )


# "upper tray 5 · lower tray 3"; "—" for an arch with no tray logged; both missing → a hint.
def _tray_line(upper: int | None, lower: int | None) -> str:
    if upper is None and lower is None:
        return "no tray logged yet"
    upper_str = f"upper tray {upper}" if upper is not None else "upper tray —"
    lower_str = f"lower tray {lower}" if lower is not None else "lower tray —"
    return f"{upper_str} · {lower_str}"


# ── Small formatting / parsing helpers (kept local, mirroring the attention domain) ──────

# Formats a tz-aware local datetime as "2:14 PM" (zero-stripped hour, 2-digit minute).
def _format_time_12h(dt_local: datetime) -> str:
    hour = dt_local.hour % 12 or 12
    meridiem = "AM" if dt_local.hour < 12 else "PM"
    return f"{hour}:{dt_local.minute:02d} {meridiem}"


# "Tue 2 Jun, 2:14 PM" — weekday + day-of-month (no leading zero) + month + 12h time.
def _format_datetime(dt_local: datetime) -> str:
    return f"{dt_local.strftime('%a %-d %b')}, {_format_time_12h(dt_local)}"


# 0-based day-of-tray in B's local timezone — day 0 is started_at's local date, day 1 is
# the next local midnight, etc. Used in the OUT/IN tray lines ("day 5 / 14"). Matches the
# Invisalign-app convention where the day of insertion is day 0.
def _day_of_tray(started_at: datetime, anchor_utc: datetime, tz) -> int:
    started_local_date = started_at.astimezone(tz).date()
    anchor_local_date = anchor_utc.astimezone(tz).date()
    return max(0, (anchor_local_date - started_local_date).days)


# Elapsed minutes between two timestamps, rounded and clamped at 0.
def _duration_minutes(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


# Formats whole minutes as "1h 35m" / "45m" / "2h".
def _format_duration_short(total_minutes: int) -> str:
    if total_minutes < 60:
        return f"{total_minutes}m"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}h" if minutes == 0 else f"{hours}h {minutes}m"


# Strips markdown code fences (if the LLM wrapped its reply) and parses the JSON object.
# Imported by correction.py too, so the JSON parsing stays identical across the domain.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    return json.loads(cleaned)
