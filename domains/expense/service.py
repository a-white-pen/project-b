"""
Expense logging domain — handles log_expense intent.

Functions:
  handle_expense_log(msg) — stub: logs expense intent, returns acknowledgement
"""

from system.messages import InboundMessage


# Handles an expense logging request from B.
# Inputs: InboundMessage with text or photo of receipt.
# Outputs: reply string. Stub until expense schema and extraction are built.
def handle_expense_log(msg: InboundMessage) -> str:
    return "expense intent captured — logging not built yet"
