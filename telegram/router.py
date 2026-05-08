"""
Routes normalized inbound messages to domain handlers based on LLM-classified intent.
Callback query updates are routed deterministically from callback_data, not via the LLM.

Functions:
  route(msg) — classifies intent (or resolves callback) and returns a reply string
"""

import logging
from enum import Enum

from domains.attention.service import handle_attention_log
from domains.expense.service import handle_expense_log
from domains.food.service import handle_food_log
from domains.general.service import handle_general_ask
from domains.query.service import handle_query_data
from domains.weight.service import handle_weight_log
from system.llm import MODEL_LITE, generate_text
from system.messages import InboundMessage, MessageType

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    LOG_FOOD = "log_food"           # logging food, meals, nutrition
    LOG_WEIGHT = "log_weight"       # logging body weight or measurements
    LOG_EXPENSE = "log_expense"     # logging money spent
    LOG_ATTENTION = "log_attention" # logging what B is working on / paying attention to
    QUERY_DATA = "query_data"       # question about B's own stored data
    ASK_GENERAL = "ask_general"     # general question — use as an LLM, unrelated to personal data
    UNKNOWN = "unknown"             # cannot determine intent


_CLASSIFY_PROMPT = """\
You are an intent classifier for a personal data tracking system. \
The user is one person tracking nutrition, body metrics, training, expenses, and attention.

Classify the message into exactly one of these intents:
- log_food: logging food, meals, or nutrition (text description or photo of food/nutrition label)
- log_weight: logging body weight or body measurements
- log_expense: logging money spent or a receipt (text or photo)
- log_attention: logging what the user is currently working on, reading, watching, or spending time on
- query_data: asking a question about their own stored data ("what did I eat today?", "am I hitting protein?")
- ask_general: a general question or request to use the assistant as an LLM — not about personal data
- unknown: cannot determine intent

Message type: {message_type}
Text: {text}
Caption: {caption}

Respond with only the intent name. Nothing else."""


# Routes an inbound message to the right domain handler and returns a reply string.
# Callback queries are routed deterministically. All other types go through the LLM classifier.
# Inputs: InboundMessage from normalizer.
# Outputs: reply string to send back to B.
def route(msg: InboundMessage) -> str:
    if msg.message_type == MessageType.CALLBACK_QUERY:
        return _route_callback(msg)
    intent = _classify_intent(msg)
    logger.info("update_id=%s intent=%s", msg.update_id, intent.value)
    return _dispatch(intent, msg)


# Routes a callback_query update deterministically from callback_data.
# Inputs: InboundMessage with message_type=CALLBACK_QUERY.
# Outputs: reply string. No LLM call — callback_data is explicit, not ambiguous.
def _route_callback(msg: InboundMessage) -> str:
    logger.info("update_id=%s callback_data=%s", msg.update_id, msg.callback_data)
    # No inline buttons implemented yet — all callback_data falls through to stub.
    # When buttons are added, dispatch here by callback_data value.
    return "button press captured — not implemented yet"


# Sends the message context to the LLM and parses the intent.
# Inputs: InboundMessage. Outputs: Intent enum value; falls back to UNKNOWN on error.
def _classify_intent(msg: InboundMessage) -> Intent:
    prompt = _CLASSIFY_PROMPT.format(
        message_type=msg.message_type.value,
        text=msg.text or "—",
        caption=msg.caption or "—",
    )
    try:
        raw = generate_text(prompt, model=MODEL_LITE)
        return _parse_intent(raw)
    except Exception as e:
        logger.warning("intent classification failed: %s", e)
        return Intent.UNKNOWN


# Extracts the intent name from an LLM response string.
# Looks for any known intent value inside the response, returns UNKNOWN if none found.
def _parse_intent(raw: str) -> Intent:
    cleaned = raw.lower().strip()
    for intent in Intent:
        if intent.value in cleaned:
            return intent
    return Intent.UNKNOWN


# Dispatches to the right domain handler and returns a reply string.
# Inputs: Intent, InboundMessage. Outputs: reply string from the domain handler.
def _dispatch(intent: Intent, msg: InboundMessage) -> str:
    if intent == Intent.LOG_FOOD:
        return handle_food_log(msg)
    if intent == Intent.LOG_WEIGHT:
        return handle_weight_log(msg)
    if intent == Intent.LOG_EXPENSE:
        return handle_expense_log(msg)
    if intent == Intent.LOG_ATTENTION:
        return handle_attention_log(msg)
    if intent == Intent.QUERY_DATA:
        return handle_query_data(msg)
    if intent == Intent.ASK_GENERAL:
        return handle_general_ask(msg)
    return "not sure what to do with that yet"
