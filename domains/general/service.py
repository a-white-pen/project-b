"""
General LLM domain — handles ask_general intent.

Functions:
  handle_general_ask(msg) — stub: captures general question intent, returns acknowledgement
"""

import logging

from system.logging import log_event
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Handles a general question from B unrelated to personal data.
# Inputs: InboundMessage with any question or request to use the assistant as an LLM.
# Outputs: reply string. Stub until general LLM passthrough is built.
def handle_general_ask(msg: InboundMessage) -> tuple[str, None]:
    log_event(
        logger,
        logging.INFO,
        "general_ask_stub_called",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
    )
    return ("general question captured — LLM passthrough not built yet", None)
