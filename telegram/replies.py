"""
Single send path for all outbound Telegram messages.

Functions:
  send_reply(chat_id, text, reply_to_message_id) — sends a text message to the given Telegram chat,
      optionally quoting a specific message
"""

import logging

import httpx

from system.config import get_config

logger = logging.getLogger(__name__)


# Sends a text message to a Telegram chat via the sendMessage API.
# Inputs: chat_id (from the inbound update), text (message to send),
#         reply_to_message_id (optional — quotes a specific message as a Telegram reply).
# Outputs: none. Logs a warning if the API call fails.
def send_reply(chat_id: int, text: str, reply_to_message_id: int | None = None) -> None:
    config = get_config()
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}

    try:
        response = httpx.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logger.info("reply sent to chat_id=%s", chat_id)
    except httpx.HTTPError as e:
        logger.warning("failed to send reply to chat_id=%s: %s", chat_id, e)
