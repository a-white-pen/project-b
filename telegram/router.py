"""
Routes normalized inbound messages to domain handlers based on LLM-classified intent.
Callback query updates are routed deterministically from callback_data, not via the LLM.
Slash commands are routed deterministically and the command token is stripped before dispatch.

Functions:
  route(msg) — classifies intent (or resolves callback/command) and returns a reply string
"""

import dataclasses
import logging
import os
from enum import Enum

from domains.attention.service import handle_attention_log
from domains.expense.service import handle_expense_log
from domains.food.correction import handle_food_correction
from domains.food.service import handle_food_log
from domains.general.service import handle_general_ask
from domains.location.service import handle_location
from domains.query.service import handle_query_data
from domains.sleep.service import handle_sleep_log, handle_wake_log
from domains.weight.service import handle_weight_log
from system.conversation_state import load_state
from system.llm import MODEL_LITE, generate_text, transcribe_audio
from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from telegram.files import get_file_bytes

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    LOG_FOOD = "log_food"           # logging food, meals, nutrition
    LOG_WEIGHT = "log_weight"       # logging body weight or measurements
    LOG_SLEEP = "log_sleep"         # going to sleep
    LOG_WAKE = "log_wake"           # just woke up
    LOG_EXPENSE = "log_expense"     # logging money spent
    LOG_ATTENTION = "log_attention" # logging what B is working on / paying attention to
    QUERY_DATA = "query_data"       # question about B's own stored data
    ASK_GENERAL = "ask_general"     # general question — use as an LLM, unrelated to personal data
    CORRECT = "correct"             # correction to a previously logged item (quoted bot reply)
    UNKNOWN = "unknown"             # cannot determine intent


# Maps slash commands to intents — bypasses LLM entirely.
# /eat@BotName form (used in groups) is handled by stripping the @suffix.
# Note: @suffix is not validated against the bot's own username — acceptable for a
# single-user private bot, but worth tightening if the bot is ever added to groups.
_COMMAND_MAP: dict[str, Intent] = {
    "/eat": Intent.LOG_FOOD,
    "/weight": Intent.LOG_WEIGHT,
    "/sleep": Intent.LOG_SLEEP,
    "/wake": Intent.LOG_WAKE,
    "/spend": Intent.LOG_EXPENSE,
    "/focus": Intent.LOG_ATTENTION,
    "/data": Intent.QUERY_DATA,
    "/ask": Intent.ASK_GENERAL,
}

_CLASSIFY_PROMPT = """\
You are an intent classifier for a personal data tracking system. \
The user is one person tracking nutrition, body metrics, training, expenses, and attention.

Classify the message into exactly one of these intents:
- log_food: logging food, meals, or nutrition (text description or photo of food/nutrition label)
- log_weight: logging body weight or body measurements — a bare number like "57.1" or "57.1 kg" always means weight in this context
- log_sleep: user is explicitly logging that they are going to sleep — strong signals: "night night", "going to sleep", "heading to bed", "bed bed", "sleeping now", sleep/moon emoji alone (🌙😴). A standalone "goodnight" with no conversational context may qualify. Do NOT classify as log_sleep if the message is clearly a conversational farewell or closing message in an ongoing exchange.
- log_wake: user is explicitly logging that they just woke up — strong signals: "just woke up", "woke up", "wakey wakey", "rise and shine", sunrise emoji alone (🌅). A standalone "good morning" or "morning" with no conversational context may qualify. Do NOT classify as log_wake if the message is clearly a conversational greeting opening a chat.
- log_expense: logging money spent or a receipt (text or photo)
- log_attention: logging what the user is currently working on, reading, watching, or spending time on
- query_data: asking a question about their own stored data ("what did I eat today?", "am I hitting protein?")
- ask_general: a general question or request to use the assistant as an LLM — not about personal data
- unknown: cannot determine intent

Message type: {message_type}
Text: {text}
Caption: {caption}

Respond with only the intent name. Nothing else."""


# Routes an inbound message to the right domain handler.
# Priority: callback_query → slash command → voice transcription → quoted correction → LLM classifier.
# Voice is transcribed before the correction check so that a quoted voice note works as a
# correction — handle_food_correction reads msg.text, which would be None for an untranscribed voice.
# Inputs: InboundMessage from normalizer.
# Outputs: (reply_text, pending_state) where pending_state is non-None for loggable actions.
#   pending_state keys: domain, context, [parent_telegram_reply_message_id].
def route(msg: InboundMessage) -> tuple[str, dict | None]:
    log_event(
        logger,
        logging.INFO,
        "route_started",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
        has_caption=bool(msg.caption),
        quoted_message_id=msg.quoted_message_id,
    )
    if msg.message_type == MessageType.CALLBACK_QUERY:
        return _route_callback(msg)
    # Location is always deterministic — no LLM needed.
    if msg.message_type == MessageType.LOCATION:
        if msg.location:
            log_event(logger, logging.INFO, "route_location_received", update_id=msg.update_id)
        return handle_location(msg)
    command_intent = _extract_command(msg)
    if command_intent is not None:
        log_event(
            logger,
            logging.INFO,
            "route_command_matched",
            update_id=msg.update_id,
            intent=command_intent.value,
        )
        return _dispatch(command_intent, _strip_command(msg))
    # Transcribe voice before correction check — correction handlers read msg.text.
    # A quoted voice note must be transcribed first so the correction can read the text.
    if msg.message_type == MessageType.VOICE:
        msg = _transcribe_voice(msg)
    # Correction path: B quoted a previous bot reply → check conversation_state before LLM.
    if msg.quoted_message_id is not None:
        result = _try_correction(msg)
        if result is not None:
            log_event(
                logger,
                logging.INFO,
                "route_correction_matched",
                update_id=msg.update_id,
                intent=Intent.CORRECT.value,
                quoted_message_id=msg.quoted_message_id,
            )
            return result
    # PHOTO: classified by LLM from message_type + caption.
    # Bare photos with no caption are unreliable — future fix is vision-based intent classification.
    intent = _classify_intent(msg)
    log_event(logger, logging.INFO, "route_intent_resolved", update_id=msg.update_id, intent=intent.value)
    return _dispatch(intent, msg)


# Routes a callback_query update deterministically from callback_data.
# Inputs: InboundMessage with message_type=CALLBACK_QUERY.
# Outputs: (reply, None). No LLM call — callback_data is explicit, not ambiguous.
def _route_callback(msg: InboundMessage) -> tuple[str, None]:
    log_event(
        logger,
        logging.INFO,
        "route_callback_received",
        update_id=msg.update_id,
        callback_data=msg.callback_data,
    )
    # No inline buttons implemented yet — all callback_data falls through to stub.
    # When buttons are added, dispatch here by callback_data value.
    return ("button press captured — not implemented yet", None)


# Extracts a slash command from the message text and maps it to an Intent.
# Handles /command and /command@BotName forms. Returns None if no known command found.
def _extract_command(msg: InboundMessage) -> Intent | None:
    if not msg.text or not msg.text.startswith("/"):
        return None
    cmd = msg.text.split()[0].split("@")[0].lower()
    return _COMMAND_MAP.get(cmd)


# Strips the leading slash command token from msg.text before handing to domain handlers.
# Domains should receive "chicken rice", not "/eat chicken rice".
# Inputs: InboundMessage where text starts with a slash command.
# Outputs: new InboundMessage with text set to the post-command content (or None if no content).
def _strip_command(msg: InboundMessage) -> InboundMessage:
    if not msg.text:
        return msg
    parts = msg.text.split(maxsplit=1)
    return dataclasses.replace(msg, text=parts[1] if len(parts) > 1 else None)


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
        log_failure(
            logger,
            logging.WARNING,
            "route_intent_classification_failed",
            e,
            update_id=msg.update_id,
            message_type=msg.message_type.value,
        )
        return Intent.UNKNOWN


# Downloads a voice message and transcribes it via Gemini.
# Returns a new InboundMessage with message_type=TEXT and text set to the transcription.
# Falls back to UNKNOWN text on error so routing can continue.
def _transcribe_voice(msg: InboundMessage) -> InboundMessage:
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        audio = get_file_bytes(msg.file_id, token)
        log_event(
            logger,
            logging.INFO,
            "route_voice_downloaded",
            update_id=msg.update_id,
            bytes_downloaded=len(audio),
        )
        text = transcribe_audio(audio)
        log_event(
            logger,
            logging.INFO,
            "route_voice_transcribed",
            update_id=msg.update_id,
            text_chars=len(text),
        )
        return dataclasses.replace(msg, message_type=MessageType.TEXT, text=text)
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "route_voice_transcription_failed",
            e,
            update_id=msg.update_id,
        )
        return dataclasses.replace(msg, message_type=MessageType.TEXT, text=None)


# Extracts the intent name from an LLM response string.
# Tries exact match first (Intent(cleaned)), then falls back to scanning for the first known
# intent value that appears as a whole word — avoids "correct" matching inside "incorrect".
def _parse_intent(raw: str) -> Intent:
    cleaned = raw.lower().strip()
    try:
        return Intent(cleaned)
    except ValueError:
        pass
    # Fallback: find the first intent value that appears as a whole word in the response.
    import re
    for intent in Intent:
        if re.search(rf"\b{re.escape(intent.value)}\b", cleaned):
            return intent
    return Intent.UNKNOWN


# Checks whether a quoted bot reply has loggable conversation_state. Returns correction result or None.
# Returning None means the quoted message has no state — fall through to normal LLM routing.
def _try_correction(msg: InboundMessage) -> tuple[str, dict | None] | None:
    try:
        state = load_state(msg.quoted_message_id)
    except Exception as e:
        log_failure(
            logger,
            logging.WARNING,
            "route_correction_state_lookup_failed",
            e,
            update_id=msg.update_id,
            quoted_message_id=msg.quoted_message_id,
        )
        return None
    if state is None:
        return None
    domain = state.get("domain")
    if domain == "food":
        return handle_food_correction(msg, state)
    # Other domains: not implemented yet — fall through to normal routing
    log_event(
        logger,
        logging.INFO,
        "route_correction_domain_not_implemented",
        update_id=msg.update_id,
        domain=domain,
    )
    return None


# Dispatches to the right domain handler.
# Inputs: Intent, InboundMessage. Outputs: (reply, pending_state) from the domain handler.
def _dispatch(intent: Intent, msg: InboundMessage) -> tuple[str, dict | None]:
    if intent == Intent.LOG_FOOD:
        return handle_food_log(msg)
    if intent == Intent.LOG_WEIGHT:
        return handle_weight_log(msg)
    if intent == Intent.LOG_SLEEP:
        return handle_sleep_log(msg)
    if intent == Intent.LOG_WAKE:
        return handle_wake_log(msg)
    if intent == Intent.LOG_EXPENSE:
        return handle_expense_log(msg)
    if intent == Intent.LOG_ATTENTION:
        return handle_attention_log(msg)
    if intent == Intent.QUERY_DATA:
        return handle_query_data(msg)
    if intent == Intent.ASK_GENERAL:
        return handle_general_ask(msg)
    return ("not sure what to do with that yet", None)
