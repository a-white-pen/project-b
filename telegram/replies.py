"""
Single send path for all outbound Telegram messages.

Functions:
  send_reply(chat_id, text, reply_to_message_id, parse_mode, bot_token, reply_markup) — sends a
      text message to the given Telegram chat, optionally quoting a specific message and/or
      attaching a reply_markup (e.g. the aligner persistent keyboard).
      Returns (telegram_message_id, sent_payload) so the caller can log to telegram_outbound.
      telegram_message_id is None if the send failed.
      Pass bot_token explicitly to bypass get_config() (e.g. from scripts without full env).
  get_latest_chat_id()            — reads B's chat_id from system.telegram_outbound.
  store_outbound(message_id, payload) — logs a proactive outbound message to system.telegram_outbound.
  _detect_parse_mode(text)        — enables Telegram HTML parsing when formatted tags are present.
"""

import json
import logging
import re

import httpx

from system.config import get_config
from system.db import get_connection
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|s|strike|del|code|pre|a)(?:\s+[^>]*)?>")


# Sends a text message to a Telegram chat via the sendMessage API.
# Inputs: chat_id (from the inbound update), text (message to send),
#         reply_to_message_id (optional — quotes a specific message as a Telegram reply),
#         parse_mode (optional — Telegram parse_mode; auto-detected for safe HTML tags),
#         bot_token (optional — pass explicitly to skip get_config(); useful in scripts
#                   that don't have the full env set, e.g. replay.py).
#         reply_markup (optional — Telegram reply_markup dict, e.g. a ReplyKeyboardMarkup.
#                   Used by the aligner domain to dock the persistent 🦷 IN / 🍽️ OUT keyboard.
#                   A persistent keyboard, once sent, stays until replaced; replies sent
#                   without reply_markup leave the existing keyboard intact).
# Outputs: (telegram_message_id, sent_payload).
#   telegram_message_id — Telegram's message_id for the sent message; None on failure.
#   sent_payload        — the full JSON body sent to the API (for outbound logging).
# Never raises — logs a warning on failure and returns (None, payload).
def send_reply(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
    bot_token: str | None = None,
    reply_markup: dict | None = None,
) -> tuple[int | None, dict]:
    token = bot_token or get_config().telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    resolved_parse_mode = parse_mode or _detect_parse_mode(text)
    if resolved_parse_mode:
        payload["parse_mode"] = resolved_parse_mode
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        log_event(
            logger,
            logging.INFO,
            "reply_sent",
            chat_id=chat_id,
            message_id=message_id,
            reply_to_message_id=reply_to_message_id,
            text_chars=len(text),
            has_reply_markup=reply_markup is not None,
        )
        return message_id, payload
    except httpx.HTTPError as e:
        log_failure(
            logger,
            logging.WARNING,
            "reply_send_failed",
            e,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            text_chars=len(text),
        )
        return None, payload


# Reads the most recent chat_id from system.telegram_outbound.
# Used to find B's chat_id for proactive (unprompted) messages — both processors call this.
# Inputs: none.
# Outputs: chat_id as int, or None if no usable rows exist.
def get_latest_chat_id() -> int | None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT (payload->>'chat_id')::bigint
                    FROM system.telegram_outbound
                    WHERE payload->>'chat_id' IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                return row[0] if row else None
    finally:
        conn.close()


# Inserts a row into system.telegram_outbound for a proactive (unprompted) message.
# telegram_update_id is NULL because there is no inbound update that triggered this.
# Inputs: Telegram message_id from the API response, full sent payload dict.
def store_outbound(message_id: int, payload: dict) -> None:
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO system.telegram_outbound"
                    " (message_id, telegram_update_id, payload)"
                    " VALUES (%s, NULL, %s)",
                    (message_id, json.dumps(payload)),
                )
    finally:
        conn.close()


# Detects whether a reply uses Telegram-compatible HTML tags and sets parse_mode="HTML".
# Used by the attention domain (activity ended/started/updated/removed replies), the food
# domain (per-item replies), and the aligner domain (wear/tray/status replies).
#
# CONTRACT: any domain that produces replies with HTML formatting tags MUST ensure that
# all user-provided or LLM-provided content is passed through html.escape() before being
# embedded in the reply string. If unescaped content containing '<', '>', or '&' reaches
# Telegram with parse_mode="HTML", Telegram returns a 400 and the reply is never delivered —
# the user sees nothing and the outbound message_id is never saved (breaking correction threading).
#
# The attention service satisfies this contract by calling html.escape() on all user/LLM
# content inside _format_session_block() (the shared formatter in domains/attention/service.py)
# and inside _format_overlap_conflict() (in domains/attention/correction.py). Any new domain
# adding HTML-formatted replies must do the same.
#
# Inputs: reply text string.
# Outputs: "HTML" when known safe formatting tags are present, otherwise None.
def _detect_parse_mode(text: str) -> str | None:
    if _HTML_TAG_RE.search(text):
        return "HTML"
    return None
