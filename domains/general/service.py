"""
General LLM domain — handles ask_general intent.

Functions:
  handle_general_ask(msg) — stub: captures general question intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles a general question from B unrelated to personal data.
# Inputs: InboundMessage with any question or request to use the assistant as an LLM.
# Outputs: reply string. Stub until general LLM passthrough is built.
def handle_general_ask(msg: InboundMessage) -> tuple[str, None]:
    return ("general question captured — LLM passthrough not built yet", None)
