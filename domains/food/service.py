"""
Food logging domain — handles log_food intent.

Functions:
  handle_food_log(msg) — stub: logs food intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles a food logging request from B.
# Inputs: InboundMessage with text or photo of food/nutrition label.
# Outputs: reply string. Stub until nutrition schema and extraction are built.
def handle_food_log(msg: InboundMessage) -> str:
    return "food intent captured — logging not built yet"
