"""
Normalizes raw Telegram Update payloads into a clean internal format.

Functions:
  normalize(payload) — converts a raw Telegram Update dict to an InboundMessage
"""

import logging
from datetime import datetime, timezone

from system.messages import InboundMessage, MessageType

logger = logging.getLogger(__name__)


# Converts a raw Telegram Update dict to an InboundMessage.
# Inputs: payload dict from the webhook (already parsed from JSON).
# Outputs: InboundMessage with all known fields populated; unknowns set to None.
def normalize(payload: dict) -> InboundMessage:
    update_id = payload.get("update_id")

    cq = payload.get("callback_query")
    if cq:
        msg = cq.get("message") or {}
        chat = msg.get("chat") or {}
        sender = cq.get("from") or {}
        return InboundMessage(
            update_id=update_id,
            message_id=msg.get("message_id"),
            chat_id=chat.get("id"),
            sender_id=sender.get("id"),
            message_type=MessageType.CALLBACK_QUERY,
            text=None,
            caption=None,
            file_id=None,
            location=None,
            callback_data=cq.get("data"),
            timestamp=_parse_ts(msg.get("date")),
        )

    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        msg = payload.get(key)
        if not msg:
            continue

        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        base = dict(
            update_id=update_id,
            message_id=msg.get("message_id"),
            chat_id=chat.get("id"),
            sender_id=sender.get("id"),
            caption=msg.get("caption"),
            timestamp=_parse_ts(msg.get("date")),
        )

        if "text" in msg:
            return InboundMessage(**base, message_type=MessageType.TEXT,
                                  text=msg["text"], file_id=None, location=None,
                                  callback_data=None)
        if "photo" in msg:
            photos = msg["photo"]
            file_id = photos[-1]["file_id"] if photos else None
            return InboundMessage(**base, message_type=MessageType.PHOTO,
                                  text=None, file_id=file_id, location=None,
                                  callback_data=None)
        if "voice" in msg:
            return InboundMessage(**base, message_type=MessageType.VOICE,
                                  text=None, file_id=msg["voice"]["file_id"],
                                  location=None, callback_data=None)
        if "location" in msg:
            loc = msg["location"]
            return InboundMessage(**base, message_type=MessageType.LOCATION,
                                  text=None, file_id=None,
                                  location=(loc["latitude"], loc["longitude"]),
                                  callback_data=None)
        if "document" in msg:
            return InboundMessage(**base, message_type=MessageType.DOCUMENT,
                                  text=None, file_id=msg["document"]["file_id"],
                                  location=None, callback_data=None)
        if "video" in msg:
            return InboundMessage(**base, message_type=MessageType.VIDEO,
                                  text=None, file_id=msg["video"]["file_id"],
                                  location=None, callback_data=None)
        if "audio" in msg:
            return InboundMessage(**base, message_type=MessageType.AUDIO,
                                  text=None, file_id=msg["audio"]["file_id"],
                                  location=None, callback_data=None)
        if "sticker" in msg:
            return InboundMessage(**base, message_type=MessageType.STICKER,
                                  text=None, file_id=msg["sticker"]["file_id"],
                                  location=None, callback_data=None)

        logger.warning("update_id=%s unrecognised message content", update_id)
        return InboundMessage(**base, message_type=MessageType.UNKNOWN,
                              text=None, file_id=None, location=None,
                              callback_data=None)

    return InboundMessage(
        update_id=update_id, message_id=None, chat_id=None, sender_id=None,
        message_type=MessageType.UNKNOWN, text=None, caption=None,
        file_id=None, location=None, callback_data=None, timestamp=None,
    )


# Converts a Unix timestamp to a UTC datetime. Returns None if ts is None.
def _parse_ts(ts: int | None) -> datetime | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)
