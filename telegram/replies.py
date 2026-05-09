"""
Single send path for all outbound Telegram messages.

Functions:
  send_reply(chat_id, text, reply_to_message_id) — sends a text message to the given Telegram chat,
      optionally quoting a specific message.
      Returns (telegram_message_id, sent_payload) so the caller can log to telegram_outbound.
      telegram_message_id is None if the send failed.
"""

import logging

import httpx

from system.config import get_config

logger = logging.getLogger(__name__)


# Sends a text message to a Telegram chat via the sendMessage API.
# Inputs: chat_id (from the inbound update), text (message to send),
#         reply_to_message_id (optional — quotes a specific message as a Telegram reply).
# Outputs: (telegram_message_id, sent_payload).
#   telegram_message_id — Telegram's message_id for the sent message; None on failure.
#   sent_payload        — the full JSON body sent to the API (for outbound logging).
# Never raises — logs a warning on failure and returns (None, payload).
def send_reply(
    chat_id: int,
    text: str,
    reply_to_message_id: int | None = None,
) -> tuple[int | None, dict]:
    config = get_config()
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}

    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        message_id = response.json().get("result", {}).get("message_id")
        logger.info("reply sent chat_id=%s message_id=%s", chat_id, message_id)
        return message_id, payload
    except httpx.HTTPError as e:
        logger.warning("failed to send reply to chat_id=%s: %s", chat_id, e)
        return None, payload
