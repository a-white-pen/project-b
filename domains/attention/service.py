"""
Attention logging domain — handles log_attention intent.

Functions:
  handle_attention_log(msg) — stub: logs attention intent, returns acknowledgement
"""

import logging

from system.logging import log_event
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Handles an attention logging request from B.
# Inputs: InboundMessage with text describing what B is working on or watching.
# Outputs: reply string. Stub until attention schema and extraction are built.
def handle_attention_log(msg: InboundMessage) -> tuple[str, None]:
    log_event(
        logger,
        logging.INFO,
        "attention_log_stub_called",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
    )
    return ("attention intent captured — logging not built yet", None)
