"""
Single send path for all outbound Telegram messages.

Functions:
  send_reply(chat_id, text, reply_to_message_id) — sends a text message to the given Telegram chat,
      optionally quoting a specific message.
      Returns (telegram_message_id, sent_payload) so the caller can log to telegram_outbound.
      telegram_message_id is None if the send failed.
  _detect_parse_mode(text) — enables Telegram HTML parsing when formatted tags are present
"""

import logging
import re

import httpx

from system.config import get_config
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_HTML_TAG_RE = re.compile(r"</?(?:b|strong|i|em|u|s|strike|del|code|pre|a)(?:\s+[^>]*)?>")


# Sends a text message to a Telegram chat via the sendMessage API.
# Inputs: chat_id (from the inbound update), text (message to send),
#         reply_to_message_id (optional — quotes a specific message as a Telegram reply),
#         parse_mode (optional — Telegram parse_mode; auto-detected for safe HTML tags).
# Outputs: (telegram_message_id, sent_payload).
#   telegram_message_id — Telegram's message_id for the sent message; None on failure.
#   sent_payload        — the full JSON body sent to the API (for outbound logging).
# Never raises — logs a warning on failure and returns (None, payload).
def send_reply(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
    parse_mode: str | None = None,
) -> tuple[int | None, dict]:
    config = get_config()
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    resolved_parse_mode = parse_mode or _detect_parse_mode(text)
    if resolved_parse_mode:
        payload["parse_mode"] = resolved_parse_mode
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}

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


# Detects whether a reply uses Telegram-compatible HTML tags and sets parse_mode="HTML".
# Currently only the attention domain uses HTML tags (<b>Attention end</b> etc.).
#
# CONTRACT: any domain that produces replies with HTML formatting tags MUST ensure that
# all user-provided or LLM-provided content is passed through html.escape() before being
# embedded in the reply string. If unescaped content containing '<', '>', or '&' reaches
# Telegram with parse_mode="HTML", Telegram returns a 400 and the reply is never delivered —
# the user sees nothing and the outbound message_id is never saved (breaking correction threading).
#
# The attention service satisfies this contract via html.escape() in _format_field().
# Any new domain adding HTML-formatted replies must do the same.
#
# Inputs: reply text string.
# Outputs: "HTML" when known safe formatting tags are present, otherwise None.
def _detect_parse_mode(text: str) -> str | None:
    if _HTML_TAG_RE.search(text):
        return "HTML"
    return None
