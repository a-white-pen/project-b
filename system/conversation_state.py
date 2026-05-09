"""
Manages conversation state for correction chains.

A root row is written whenever a domain handler returns non-None pending_state.
Currently only food does this — weight, expense, and attention will add state
when those domains are built.
Correction rows link back via parent_telegram_reply_message_id.
Thread is reconstructed via recursive CTE joining telegram_outbound and telegram_inbound.

Functions:
  save_state(...)   — inserts a row into system.conversation_state
  load_state(...)   — fetches one row by telegram_reply_message_id; returns None if not found
  get_thread(...)   — returns the full correction chain for a given node in the chain
"""

import logging

import psycopg2.extras

from system.db import get_connection

logger = logging.getLogger(__name__)


# Inserts a row into system.conversation_state.
# Called by webhook._process_and_reply when a domain handler returns non-None pending_state.
# Currently only the food handler does this; other domains will add state as they are built.
# Inputs:
#   telegram_reply_message_id       — Telegram message_id of the bot reply (FK to telegram_outbound)
#   triggering_telegram_update_id   — update_id of the inbound message that triggered this reply
#   domain                          — domain string (currently only "food" in practice)
#   context                         — domain-specific structured data (e.g. {food_log_ids, meal_type})
#   parent_telegram_reply_message_id — None for root rows; quoted bot message_id for correction rows
def save_state(
    telegram_reply_message_id: int,
    triggering_telegram_update_id: int,
    domain: str,
    context: dict,
    parent_telegram_reply_message_id: int | None = None,
) -> None:
    sql = """
        INSERT INTO system.conversation_state
            (telegram_reply_message_id, parent_telegram_reply_message_id,
             triggering_telegram_update_id, domain, context)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (telegram_reply_message_id) DO NOTHING
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (
                    telegram_reply_message_id,
                    parent_telegram_reply_message_id,
                    triggering_telegram_update_id,
                    domain,
                    psycopg2.extras.Json(context),
                ))
    finally:
        conn.close()


# Fetches one conversation_state row by telegram_reply_message_id.
# Returns a dict with all columns, or None if no row exists.
# Used by the correction router to check whether a quoted bot message has loggable state.
def load_state(telegram_reply_message_id: int) -> dict | None:
    sql = """
        SELECT telegram_reply_message_id, parent_telegram_reply_message_id,
               triggering_telegram_update_id, domain, context
        FROM system.conversation_state
        WHERE telegram_reply_message_id = %s
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (telegram_reply_message_id,))
                row = cur.fetchone()
                if row is None:
                    return None
                return {
                    "telegram_reply_message_id": row[0],
                    "parent_telegram_reply_message_id": row[1],
                    "triggering_telegram_update_id": row[2],
                    "domain": row[3],
                    "context": row[4],
                }
    finally:
        conn.close()


# Returns the full correction chain reachable from any node, ordered oldest-first.
# Phase 1: walk UP from the given node to find the root (no parent).
# Phase 2: walk DOWN from root to collect every descendant.
# This means get_thread(any_node) always returns the complete chain regardless of which turn
# you call it from — root, a correction, or a correction of a correction.
# Each row joins in the triggering user text (telegram_inbound.payload) and the bot reply
# (telegram_outbound.payload) so the correction LLM has full context.
# Inputs: any telegram_reply_message_id in the chain.
# Outputs: list of dicts, one per turn, oldest first.
def get_thread(telegram_reply_message_id: int) -> list[dict]:
    sql = """
        WITH RECURSIVE
        -- Phase 1: walk UP from the given node to the root (parent IS NULL)
        ancestors AS (
            SELECT
                cs.telegram_reply_message_id,
                cs.parent_telegram_reply_message_id,
                cs.triggering_telegram_update_id,
                cs.domain,
                cs.context
            FROM system.conversation_state cs
            WHERE cs.telegram_reply_message_id = %s

            UNION ALL

            SELECT
                cs.telegram_reply_message_id,
                cs.parent_telegram_reply_message_id,
                cs.triggering_telegram_update_id,
                cs.domain,
                cs.context
            FROM system.conversation_state cs
            JOIN ancestors a ON cs.telegram_reply_message_id = a.parent_telegram_reply_message_id
        ),
        root AS (
            SELECT * FROM ancestors WHERE parent_telegram_reply_message_id IS NULL
        ),
        -- Phase 2: walk DOWN from root to collect all descendants
        chain AS (
            SELECT * FROM root

            UNION ALL

            SELECT
                cs.telegram_reply_message_id,
                cs.parent_telegram_reply_message_id,
                cs.triggering_telegram_update_id,
                cs.domain,
                cs.context
            FROM system.conversation_state cs
            JOIN chain c ON cs.parent_telegram_reply_message_id = c.telegram_reply_message_id
        )
        SELECT
            chain.telegram_reply_message_id,
            chain.parent_telegram_reply_message_id,
            chain.triggering_telegram_update_id,
            chain.domain,
            chain.context,
            ti.payload  AS inbound_payload,
            tob.payload AS outbound_payload
        FROM chain
        LEFT JOIN system.telegram_inbound ti
            ON ti.update_id = chain.triggering_telegram_update_id
        LEFT JOIN system.telegram_outbound tob
            ON tob.message_id = chain.telegram_reply_message_id
        ORDER BY chain.telegram_reply_message_id ASC
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (telegram_reply_message_id,))
                rows = cur.fetchall()
                return [
                    {
                        "telegram_reply_message_id": r[0],
                        "parent_telegram_reply_message_id": r[1],
                        "triggering_telegram_update_id": r[2],
                        "domain": r[3],
                        "context": r[4],
                        "inbound_payload": r[5],
                        "outbound_payload": r[6],
                    }
                    for r in rows
                ]
    finally:
        conn.close()
