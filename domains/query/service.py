"""
Data query domain — handles query_data intent.

Functions:
  handle_query_data(msg) — stub: captures data query intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles a data query from B about their own stored data.
# Inputs: InboundMessage with a question about nutrition, weight, expenses, etc.
# Outputs: reply string. Stub until query engine is built.
def handle_query_data(msg: InboundMessage) -> str:
    return "data query captured — queries not built yet"
