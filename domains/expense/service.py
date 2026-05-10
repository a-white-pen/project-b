"""
Expense logging domain — handles log_expense intent.

Functions:
  handle_expense_log(msg) — stub: logs expense intent, returns acknowledgement
"""

import logging

from system.logging import log_event
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Handles an expense logging request from B.
# Inputs: InboundMessage with text or photo of receipt.
# Outputs: reply string. Stub until expense schema and extraction are built.
def handle_expense_log(msg: InboundMessage) -> tuple[str, None]:
    log_event(
        logger,
        logging.INFO,
        "expense_log_stub_called",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
        has_caption=bool(msg.caption),
    )
    return ("expense intent captured — logging not built yet", None)
