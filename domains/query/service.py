"""
Data query domain — handles query_data intent.

Functions:
  handle_query_data(msg) — stub: captures data query intent, returns acknowledgement
"""

import logging

from system.logging import log_event
from system.messages import InboundMessage

logger = logging.getLogger(__name__)


# Handles a data query from B about their own stored data.
# Inputs: InboundMessage with a question about nutrition, weight, expenses, etc.
# Outputs: reply string. Stub until query engine is built.
def handle_query_data(msg: InboundMessage) -> tuple[str, None]:
    log_event(
        logger,
        logging.INFO,
        "query_data_stub_called",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
    )
    return ("data query captured — queries not built yet", None)
