"""
Weight logging domain — handles log_weight intent.

Functions:
  handle_weight_log(msg) — stub: logs weight intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles a weight logging request from B.
# Inputs: InboundMessage with text containing weight or body measurement.
# Outputs: reply string. Stub until weight schema and extraction are built.
def handle_weight_log(msg: InboundMessage) -> tuple[str, None]:
    return ("weight intent captured — logging not built yet", None)
