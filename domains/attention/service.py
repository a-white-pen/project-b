"""
Attention logging domain — handles log_attention intent.

Functions:
  handle_attention_log(msg) — stub: logs attention intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles an attention logging request from B.
# Inputs: InboundMessage with text describing what B is working on or watching.
# Outputs: reply string. Stub until attention schema and extraction are built.
def handle_attention_log(msg: InboundMessage) -> tuple[str, None]:
    return ("attention intent captured — logging not built yet", None)
