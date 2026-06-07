"""
Expense correction — applies B's quoted-reply correction to a spend row.

Triggered when B quotes a spend bot reply (router checks conversation_state). A correction is just
another contribution to the transaction's thread: the new text and/or photo is appended and the
whole spend is REBUILT from all images + text by service.apply_thread_update (the same path album
stragglers use). The rebuild LLM applies B's text as an explicit override and otherwise picks the
best value per field, so editing the merchant never downgrades the rate or wipes the items.

Supports: editing any field, flipping a spend to ignored / reopening it, attaching the actual
payment screenshot, and hard delete ("delete" / "remove").

Functions:
  handle_expense_correction(msg, state) — main entry; returns (reply_text, pending_state | None)

Internal:
  _thread_state(spend_entry_id, state) — pending_state that keeps the correction chain alive
  _is_delete_request(text)             — detects explicit delete/remove text
"""

import logging
import re

from system.logging import log_event, log_failure
from system.messages import InboundMessage

from domains.expense.replies import format_deleted_reply
from domains.expense.repository import delete_spend, get_spend
from domains.expense.service import apply_thread_update, contributions_from_msg

logger = logging.getLogger(__name__)

# Exact whole-message phrases that mean "delete this spend". Kept strict so a field edit like
# "remove waikit from the notes" or a negation like "do not delete" never hard-deletes the row.
_DELETE_PHRASES = frozenset({
    "delete", "delete this", "delete it", "delete this spend", "delete the spend", "delete spend",
    "remove", "remove this", "remove it", "remove this spend", "remove the spend", "remove spend",
    "scrap", "scrap this", "discard", "discard this",
})
_DELETE_NEGATIONS = ("not", "n t", "never", "dont", "cant", "wont")


# Handles a quoted correction to a previously logged spend.
# Inputs: quoted InboundMessage plus conversation_state carrying {spend_entry_id}.
# Outputs: (reply_text, pending_state). None state after a delete; otherwise the row is rebuilt
#   from its full thread and the reply chains onto the previous reply for the spend.
def handle_expense_correction(msg: InboundMessage, state: dict) -> tuple[str, dict | None]:
    context = state.get("context") or {}
    spend_entry_id = context.get("spend_entry_id")
    correction_text = msg.text or msg.caption
    is_photo = msg.message_type.value == "photo" and bool(msg.file_id)

    if not spend_entry_id:
        return ("Nothing to correct — couldn't find that spend.", None)
    # Clarification keeps the thread alive: quoting THIS reply must route back to the correction
    # handler and update the same row, never fall through to a fresh insert.
    if not correction_text and not is_photo:
        return ("What should I change on that spend?", _thread_state(spend_entry_id, state))

    try:
        current = get_spend(spend_entry_id)
    except Exception as e:
        log_failure(logger, logging.ERROR, "expense_correction_fetch_failed", e,
                    update_id=msg.update_id, spend_entry_id=spend_entry_id)
        return ("Couldn't load that spend — try again.", _thread_state(spend_entry_id, state))

    if current is None:
        return ("Nothing to correct — that spend is already gone.", None)

    # Hard delete on explicit request (cascades to allocations). Photo-only corrections have no text.
    if correction_text and _is_delete_request(correction_text):
        try:
            delete_spend(spend_entry_id)
        except Exception as e:
            log_failure(logger, logging.ERROR, "expense_correction_delete_failed", e,
                        update_id=msg.update_id, spend_entry_id=spend_entry_id)
            return ("Couldn't delete that spend — try again.", None)
        log_event(logger, logging.INFO, "expense_correction_deleted",
                  update_id=msg.update_id, spend_entry_id=spend_entry_id)
        return (format_deleted_reply(), None)

    # Append this correction (text and/or photo) to the thread and rebuild the row from everything.
    log_event(logger, logging.INFO, "expense_correction_started",
              update_id=msg.update_id, spend_entry_id=spend_entry_id, is_photo=is_photo)
    return apply_thread_update(current, contributions_from_msg(msg), msg)


# Builds the correction-thread pending_state for a clarification/error reply: keeps the chain
# pointed at the same spend so quoting this reply routes back to the correction handler.
# Inputs: spend_entry_id, the conversation_state for the quoted message. Output: pending_state dict.
def _thread_state(spend_entry_id: int, state: dict) -> dict:
    return {
        "domain": "expense",
        "context": {"spend_entry_id": spend_entry_id},
        "parent_telegram_reply_message_id": state.get("telegram_reply_message_id"),
    }


# Detects an explicit delete request. Inputs: correction text.
# Output: True ONLY when the whole message is an exact delete phrase and contains no negation —
# so "remove tip", "remove X from the notes", "do not delete", and "don't delete" all return False.
def _is_delete_request(text: str) -> bool:
    cleaned = re.sub(r"[^\w\s]", " ", text.strip().lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return False
    if any(neg in cleaned.split() or neg in cleaned for neg in _DELETE_NEGATIONS):
        return False
    return cleaned in _DELETE_PHRASES
