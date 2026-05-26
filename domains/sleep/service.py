"""
Sleep/wake logging domain — handles log_sleep and log_wake intents.

Functions:
  handle_sleep_log(msg)         — records a sleep event in b.sleep_wake_events; also
                                  auto-closes any open attention session first and
                                  emits its end block(s) above the sleep reply.
  handle_wake_log(msg)          — records a wake event in b.sleep_wake_events
  ensure_recent_wake_logged(...) — inserts an auto-inferred wake when none exists in
                                  the last 24h; idempotent within that window. Public
                                  cross-domain API so attention.service can trigger
                                  the reminder bubble without writing to sleep tables
                                  directly.
  _insert_sleep_event(msg, event_type) — inserts one sleep/wake boundary row
  _lock_sleep_wake_writes(cur)         — advisory lock used by ensure_recent_wake_logged
"""

import logging
from datetime import datetime, timedelta, timezone

import psycopg2.extras

from domains.attention.service import close_open_sessions_externally
from system.db import get_connection
from system.logging import log_event, log_failure
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Inserts a sleep or wake event into b.sleep_wake_events.
# Inputs: InboundMessage, event_type ("sleep" or "wake").
# Outputs: (reply string, pending_state dict | None). pending_state enables quoted corrections.
def _insert_sleep_event(msg: InboundMessage, event_type: str) -> tuple[str, dict | None]:
    occurred_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    meta = {
        "source": "telegram",
        "self_reported": True,
        "telegram_update_id": msg.update_id,
    }
    log_event(
        logger,
        logging.INFO,
        "sleep_wake_log_started",
        update_id=msg.update_id,
        event_type=event_type,
        occurred_at=occurred_at.isoformat(),
    )

    sleep_wake_event_id: int | None = None
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO b.sleep_wake_events (event_type, occurred_at, meta)
                        VALUES (%s, %s, %s)
                        RETURNING sleep_wake_event_id
                        """,
                        (event_type, occurred_at, psycopg2.extras.Json(meta)),
                    )
                    row = cur.fetchone()
                    sleep_wake_event_id = row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.ERROR,
            "sleep_wake_insert_failed",
            e,
            event_type=event_type,
            update_id=msg.update_id,
        )
        return ("Couldn't save that — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "sleep_wake_inserted",
        update_id=msg.update_id,
        event_type=event_type,
        occurred_at=occurred_at.isoformat(),
        sleep_wake_event_id=sleep_wake_event_id,
    )
    state = {
        "domain": "sleep_wake",
        "context": {
            "sleep_wake_event_ids": [sleep_wake_event_id],
            "event_type": event_type,
        },
    }
    if event_type == "wake":
        return ("🌅 Wake time logged.", state)
    return ("🌙 Sleep time logged.", state)


# Handles a wake logging request from B. Returns a list shape (always one entry) to
# match the router's other multi-reply handlers.
def handle_wake_log(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    return [_insert_sleep_event(msg, "wake")]


# Handles a sleep logging request from B. If an open attention session exists, it
# is auto-closed first and its end block(s) are emitted ABOVE the sleep reply —
# so going to bed without manually finishing a session still produces a clean
# end record. Sleep insert happens after the close so the close timestamp is
# strictly earlier than the sleep event for consistent ordering.
def handle_sleep_log(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    ended_at = msg.timestamp if msg.timestamp is not None else datetime.now(timezone.utc)
    attention_replies = close_open_sessions_externally(
        msg=msg,
        ended_at=ended_at,
        reason="auto_closed_on_sleep",
    )
    sleep_reply = _insert_sleep_event(msg, "sleep")
    return [*attention_replies, sleep_reply]


# Acquires a transaction-scoped advisory lock that serializes sleep/wake auto-insert
# writes. Concurrent ensure_recent_wake_logged calls queue at the lock rather than
# racing to both pass the dedup check and double-insert. The lock auto-releases on
# COMMIT or ROLLBACK; no manual release needed. Mirrors _lock_attention_writes in
# domains/attention/service.py.
def _lock_sleep_wake_writes(cur) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('b.sleep_wake_events'))")


# Ensures a wake event exists in the last 24h. If none, inserts a placeholder at
# now_utc - 5min flagged with meta.auto_inferred=true so B can quote-correct it
# (and analytics can ignore it). Returns the new (sleep_wake_event_id, occurred_at)
# on insert, or None when an existing wake was found (no action) or the insert
# failed (caller treats both as "no auto-wake happened").
#
# The dedup check and the insert run in the SAME transaction under an advisory
# lock — closes the TOCTOU race where two rapid-fire callers could both pass
# their independent existence checks before either inserts.
#
# This is the cross-domain API: callers like domains.attention.service trigger it
# when they detect activity without a recent wake. All sleep/wake row writes live
# in this module; the trigger decision (when/why) lives in the calling domain.
#
# `trigger` is a short string recorded in meta for analytics provenance — pass
# something like "attention_start_with_no_recent_wake" so future analytics can
# tell why this row was auto-created.
def ensure_recent_wake_logged(
    now_utc: datetime,
    msg: InboundMessage,
    trigger: str,
) -> tuple[int, datetime] | None:
    lookback_start = now_utc - timedelta(hours=24)
    occurred_at = now_utc - timedelta(minutes=5)
    meta = {
        "source": "telegram",
        "self_reported": False,
        "auto_inferred": True,
        "trigger": trigger,
        "triggering_telegram_update_id": msg.update_id,
    }
    event_id: int | None = None
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    # Serialize check+insert under one lock — concurrent handlers
                    # queue rather than racing to both pass the dedup check.
                    _lock_sleep_wake_writes(cur)
                    cur.execute(
                        """
                        SELECT 1
                        FROM b.sleep_wake_events
                        WHERE event_type = 'wake'
                          AND occurred_at >= %s
                          AND occurred_at <= %s
                        LIMIT 1
                        """,
                        (lookback_start, now_utc),
                    )
                    if cur.fetchone() is not None:
                        # Dedup hit — existing wake in window, no insert. Log so we
                        # can trace "why didn't the auto-wake reminder fire?" without
                        # ambiguity (silent-return otherwise looks identical to the
                        # insert-failed path).
                        log_event(
                            logger,
                            logging.INFO,
                            "sleep_auto_wake_skipped_existing",
                            update_id=msg.update_id,
                            trigger=trigger,
                            lookback_hours=24,
                        )
                        return None  # release lock via COMMIT, no insert
                    cur.execute(
                        """
                        INSERT INTO b.sleep_wake_events (event_type, occurred_at, meta)
                        VALUES ('wake', %s, %s)
                        RETURNING sleep_wake_event_id
                        """,
                        (occurred_at, psycopg2.extras.Json(meta)),
                    )
                    row = cur.fetchone()
                    event_id = row[0] if row else None
        finally:
            conn.close()
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "sleep_auto_wake_insert_failed",
            e,
            update_id=msg.update_id,
            trigger=trigger,
        )
        return None
    if event_id is None:
        return None
    log_event(
        logger,
        logging.INFO,
        "sleep_auto_wake_inserted",
        update_id=msg.update_id,
        sleep_wake_event_id=event_id,
        occurred_at=occurred_at.isoformat(),
        trigger=trigger,
    )
    return (event_id, occurred_at)
