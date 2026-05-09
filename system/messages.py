"""
Shared internal message types — source-agnostic, used across telegram/, domains/, and system/.

Classes:
  MessageType    — enum of recognised inbound message types
  InboundMessage — normalised representation of a message from any input source
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class MessageType(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VOICE = "voice"
    LOCATION = "location"
    DOCUMENT = "document"
    VIDEO = "video"
    AUDIO = "audio"
    STICKER = "sticker"
    CALLBACK_QUERY = "callback_query"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class InboundMessage:
    update_id: int | None
    message_id: int | None
    chat_id: int | None
    sender_id: int | None
    message_type: MessageType
    text: str | None          # MessageType.TEXT only
    caption: str | None       # optional text on media messages
    file_id: str | None       # Telegram file_id for media types
    location: tuple[float, float] | None  # (latitude, longitude) for LOCATION
    callback_data: str | None  # button payload for CALLBACK_QUERY
    timestamp: datetime | None
    quoted_message_id: int | None = None  # message_id of the bot reply B is quoting (reply_to_message)
