"""
Routes normalized inbound messages to domain handlers based on LLM-classified intent.
Callback query updates are routed deterministically from callback_data, not via the LLM.
Slash commands are reserved for administrative or read/status actions (e.g. /refresh_menus,
/aligner_status, /attention_status) and bypass the LLM — never for data logging.
Free-form messages (text, photo, voice) are classified by the LLM.

Functions:
  route(msg) — classifies intent (or resolves callback/command/button) and returns a list of
      replies. Each item is (reply_text, pending_state) for most domains; the aligner domain
      appends an optional third element (a reply_markup dict) to dock its persistent keyboard.
      telegram/webhook.py unpacks the optional third element and passes it to send_reply.

Internal:
  _route_callback(msg)                 — deterministic callback_query dispatch
  _extract_command(msg) / _strip_command(msg) — slash-command parsing
  _extract_button(msg)                 — aligner reply-keyboard button-label match
  _classify_intent(msg)                — text/voice intent via LLM
  _classify_photo(msg)                 — lone-photo intent via vision (carries fetched bytes)
  _classify_album(msg)                 — media-group intent across ALL photos in one vision call
  _transcribe_voice(msg)               — voice → text via Gemini
  _parse_intent(raw)                   — LLM output → Intent enum
  _try_correction(msg)                 — quoted-reply correction routing
  _dispatch(intent, msg)               — Intent → domain handler
"""

import dataclasses
import logging
import os
from enum import Enum

from domains.menus.service import handle_refresh_menus
from domains.aligner.correction import handle_aligner_correction
from domains.aligner.service import (
    BUTTON_IN_TEXT,
    BUTTON_OUT_TEXT,
    handle_aligner_in,
    handle_aligner_out,
    handle_aligner_status,
)
from domains.attention.correction import handle_attention_correction
from domains.attention.service import (
    handle_attention_log,
    handle_attention_status,
    try_handle_wake_as_nap_end,
)
from domains.expense.correction import handle_expense_correction
from domains.expense.service import handle_expense_log
from domains.food.correction import handle_food_correction
from domains.food.service import handle_food_log
from domains.general.service import handle_general_ask
from domains.health_agent.plan_command import (
    dispatch_plan_subcommand,
    handle_meal_eaten,
    handle_plan,
    handle_plan_correction,
    handle_week_view,
)
from domains.location.service import handle_location
from domains.query.service import handle_query_data
from domains.sleep.correction import handle_sleep_wake_correction
from domains.sleep.service import handle_sleep_log, handle_wake_log
from domains.weight.correction import handle_weight_correction
from domains.weight.service import handle_weight_log
from system.conversation_state import load_state
from system.llm import (
    MODEL_FLASH,
    generate_text,
    generate_with_image,
    generate_with_images,
    transcribe_audio,
)
from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from telegram.files import get_file_bytes
from telegram.replies import pin_kind_for

logger = logging.getLogger(__name__)


class Intent(str, Enum):
    LOG_FOOD = "log_food"               # logging food, meals, nutrition
    LOG_WEIGHT = "log_weight"           # logging body weight or measurements
    LOG_SLEEP = "log_sleep"             # going to sleep
    LOG_WAKE = "log_wake"               # just woke up
    LOG_EXPENSE = "log_expense"         # logging money spent
    LOG_ATTENTION = "log_attention"     # logging what B is working on / paying attention to
    ATTENTION_STATUS = "attention_status"  # /attention_status — current focus + today's per-category breakdown
    LOG_ALIGNER_OUT = "log_aligner_out" # 🍽️ OUT button tap — aligners came out of the mouth
    LOG_ALIGNER_IN = "log_aligner_in"   # 🦷 IN button tap — aligners back in the mouth
    ALIGNER_STATUS = "aligner_status"   # /aligner_status — read current state + bootstrap keyboard
    QUERY_DATA = "query_data"           # question about B's own stored data
    ASK_GENERAL = "ask_general"         # general question — use as an LLM, unrelated to personal data
    CORRECT = "correct"                 # correction to a previously logged item (quoted bot reply)
    REFRESH_MENUS = "refresh_menus"     # trigger full menu scrape across all sources
    VIEW_WEEK = "view_week"             # /week — read-only weekly plan view (+ Plan Week button)
    PLAN = "plan"                       # /plan — hub: run / strength / meal
    UNKNOWN = "unknown"                 # cannot determine intent


# Maps slash commands to intents — bypasses LLM entirely.
# Slash commands are for administrative or read/status bot actions, not data logging.
# Free-form messages (text, photo, voice) go through the LLM classifier instead.
# /command@BotName form (used in groups) is handled by stripping the @suffix.
_COMMAND_MAP: dict[str, Intent] = {
    "/refresh_menus": Intent.REFRESH_MENUS,
    "/aligner_status": Intent.ALIGNER_STATUS,
    "/attention_status": Intent.ATTENTION_STATUS,
    "/week": Intent.VIEW_WEEK,
    "/plan": Intent.PLAN,
}

# Maps the exact aligner reply-keyboard button labels to intents — like slash commands,
# these bypass the LLM. The buttons are a recording action (tap to log in/out of mouth),
# not a typed command; their text arrives as a normal TEXT message which we match verbatim.
_BUTTON_MAP: dict[str, Intent] = {
    BUTTON_IN_TEXT: Intent.LOG_ALIGNER_IN,
    BUTTON_OUT_TEXT: Intent.LOG_ALIGNER_OUT,
}

_CLASSIFY_PROMPT = """\
You are an intent classifier for a personal data tracking system. \
The user is one person tracking nutrition, body metrics, training, expenses, and attention.

Classify the message into exactly one of these intents:
- log_food: logging specific food items, meals consumed, or nutrition/macros (text description or photo of food/nutrition label)
- log_weight: logging body weight or body measurements — a bare number like "57.1" or "57.1 kg" always means weight in this context
- log_sleep: user is explicitly logging that they are going to NIGHT-SLEEP — strong signals: "night night", "going to sleep", "heading to bed", "bed bed", "sleeping now", "orh orh", "orh orh kun", sleep/moon emoji alone (🌙😴). A standalone "goodnight" with no conversational context may qualify. Do NOT classify as log_sleep if the message is clearly a conversational farewell or closing message in an ongoing exchange. Naps are NOT log_sleep — see the nap rule below under Disambiguation.
- log_wake: user is explicitly logging that they just woke up — strong signals: "just woke up", "woke up", "wakey wakey", "rise and shine", sunrise emoji alone (🌅). A standalone "good morning" or "morning" with no conversational context may qualify. Do NOT classify as log_wake if the message is clearly a conversational greeting opening a chat.
- log_expense: any money movement — money spent, a receipt or payment screenshot (text or photo), a wallet top-up (e.g. "topped up YouTrip"), a credit-card bill payment, or a transfer to someone. PayNow/PayLah/PromptPay/Grab/Bolt payments all count.
- log_attention: logging an attention/activity session start or finish — what the user is doing, working on, reading, watching, eating, cooking, commuting, resting, grooming/showering, or spending time on. Examples: "working on Project B", "I go cook dinner now", "finish lunch", "prep breakfast", "coffee break", "order food", "go poop", "go mum mum" (eat), "go pong pong" (shower/bathe), "done with attention module", "watching Succession"
- query_data: asking a question about their own stored data ("what did I eat today?", "am I hitting protein?")
- ask_general: a general question or request to use the assistant as an LLM — not about personal data
- unknown: cannot determine intent

Message type: {message_type}
Text: {text}
Caption: {caption}

Disambiguation:
- "ate chicken rice", "had eggs", a dish name, a food photo, or nutrition numbers = log_food.
- "go cook dinner", "prep breakfast", "eat lunch", "finish lunch", "coffee break", "order food", "go mum mum", "finish mum mum", "cooking", or meal words used as time/activity boundaries without specific food items = log_attention.
- Naps are log_attention (downtime/rest), NOT log_sleep. Signals: "nap nap", "napping", "taking a nap", "having a nap", "power nap", "lie down for a bit". log_sleep is reserved for going to bed for night sleep.
- Baby-talk terms may arrive literally from voice transcription: "orh orh" or "orh orh kun" = log_sleep; "mum mum" = eating activity; "pong pong" = shower/bathe activity; "nap nap" = nap (log_attention, downtime/rest).

Respond with only the intent name. Nothing else."""


_PHOTO_CLASSIFY_PROMPT = """\
You are classifying a photo sent to a personal tracker by one person. Decide what the image is,
using BOTH the image content and the caption. The image is the stronger signal — a caption that
sounds like an activity does not override a receipt/payment image.

- log_expense: a financial document — a shop/restaurant receipt, an order receipt, a payment app \
screenshot (YouTrip, PayNow, PayLah, PromptPay, TrueMoney), a bank transaction notification, a \
money-changer slip, a paid bill, or any screen showing an amount that was paid.
- log_food: food itself — a meal, dish, drink, or a packaged-food nutrition label / ingredients panel.
- log_weight: a scale display or body-measurement readout showing a weight number.
- unknown: none of the above / cannot tell.

Caption from the user: {caption}

Disambiguation:
- A receipt, bill, or payment/transaction screenshot is log_expense — even if the caption describes \
an activity (e.g. "did laundry", "had lunch") or the spend is for food. What was PAID for is a spend.
- A nutrition label or a photo of food on a plate (no prices/payment shown) is log_food.
- If the image shows an amount paid, choose log_expense regardless of the caption wording.

Respond with only the intent name. Nothing else."""


_ALBUM_CLASSIFY_PROMPT = """\
You are classifying a GROUP of photos that were sent together as ONE submission to a personal
tracker by one person. Decide what the WHOLE group represents, using all the images and the caption.

KEY RULE: if ANY single photo in the group is a financial document — a shop/restaurant receipt, an \
order receipt, a payment-app screenshot (YouTrip, PayNow, PayLah, PromptPay, TrueMoney), a bank \
transaction notification, a money-changer slip, a paid bill, or any screen showing an amount that \
was paid — classify the WHOLE group as log_expense. People commonly send a receipt together with a \
payment screenshot, and the other photos do not override that.

Only if NO photo is a financial document, pick from:
- log_food: the photos are food — meals, dishes, drinks, or a packaged-food nutrition label.
- log_weight: a scale display or body-measurement readout showing a weight number.
- unknown: none of the above / cannot tell.

Caption from the user: {caption}

Respond with only the intent name. Nothing else."""


# Routes an inbound message to the right domain handler.
# Priority: callback_query → location → slash command → aligner button → voice transcription → quoted correction → LLM classifier.
# Voice is transcribed before the correction check so that a quoted voice note works as a
# correction — handle_food_correction reads msg.text, which would be None for an untranscribed voice.
# Inputs: InboundMessage from normalizer.
# Outputs: list of (reply_text, pending_state[, reply_markup]) tuples. The optional third
#   element is a reply_markup dict, returned only by the aligner domain to keep its persistent
#   keyboard docked; webhook.py treats it as None when absent. Multi-entry lists when the
#   domain genuinely produces more than one bubble: food logging (one per food item), food
#   correction (per item), attention logging (one per session block — "finish X and start Y"
#   yields two), attention correction (one per affected session), aligner wear correction
#   (the updated event plus one per spawned tray). All other domains return a single entry.
#   pending_state keys: domain, context, [parent_telegram_reply_message_id].
def route(msg: InboundMessage) -> list[tuple[str, dict | None]]:
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
        return [handle_location(msg)]
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
    # Aligner keyboard taps — exact button-label match, deterministic like commands.
    button_intent = _extract_button(msg)
    if button_intent is not None:
        log_event(
            logger,
            logging.INFO,
            "route_button_matched",
            update_id=msg.update_id,
            intent=button_intent.value,
        )
        return _dispatch(button_intent, msg)
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
    # Photos are classified from the IMAGE (with the caption as extra context), not caption-only —
    # a caption that reads like an activity must not hide a receipt/payment image. The fetched bytes
    # are carried on the message so the domain handler does not re-download. Text/voice use the text
    # classifier.
    if msg.message_type == MessageType.PHOTO and msg.file_id:
        # An album (>1 photo) is ONE submission: classify across ALL its photos so an ambiguous
        # first photo (e.g. a plain food shot) cannot route a later clear payment screenshot away
        # from expense. A lone photo uses the single-image classifier.
        if msg.media_group_file_ids and len(msg.media_group_file_ids) > 1:
            msg, intent = _classify_album(msg)
        else:
            msg, intent = _classify_photo(msg)
    else:
        intent = _classify_intent(msg)
    log_event(logger, logging.INFO, "route_intent_resolved", update_id=msg.update_id, intent=intent.value)
    return _dispatch(intent, msg)


# Routes a callback_query update deterministically from callback_data (no LLM).
# Planner buttons: `plan:<sub>` -> the /plan sub-planners (run/strength/meal/week); `meal_ate:<...>`
# -> meal/staple completion. Handlers dismiss the spinner (answer_callback_query) themselves.
# Inputs: InboundMessage with message_type=CALLBACK_QUERY.
# Outputs: list of (reply, state) bubbles (a handler may self-send its message and return []).
def _route_callback(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    log_event(
        logger,
        logging.INFO,
        "route_callback_received",
        update_id=msg.update_id,
        callback_data=msg.callback_data,
    )
    data = msg.callback_data or ""
    if data.startswith("plan:"):
        return dispatch_plan_subcommand(data.split(":", 1)[1], msg)
    if data.startswith("meal_ate:"):
        return handle_meal_eaten(msg)
    # Unknown callback_data — acknowledge without action.
    return [("button press captured — not implemented yet", None)]


# Extracts a slash command from the message text and maps it to an Intent.
# Handles /command and /command@BotName forms. Returns None if no known command found.
def _extract_command(msg: InboundMessage) -> Intent | None:
    if not msg.text or not msg.text.startswith("/"):
        return None
    cmd = msg.text.split()[0].split("@")[0].lower()
    return _COMMAND_MAP.get(cmd)


# Matches an aligner reply-keyboard button tap by its exact label. Returns None for any
# other text. Whitespace is stripped so a stray trailing space from the client doesn't miss.
def _extract_button(msg: InboundMessage) -> Intent | None:
    if not msg.text:
        return None
    return _BUTTON_MAP.get(msg.text.strip())


# Strips the leading slash command token from msg.text before handing to domain handlers.
# Domains should receive the trailing content, not the raw command token.
# Inputs: InboundMessage where text starts with a slash command.
# Outputs: new InboundMessage with text set to the post-command content (or None if no content).
def _strip_command(msg: InboundMessage) -> InboundMessage:
    if not msg.text:
        return msg
    parts = msg.text.split(maxsplit=1)
    return dataclasses.replace(msg, text=parts[1] if len(parts) > 1 else None)


# Sends the message context to the LLM and parses the intent.
# Used for text and voice (already transcribed). All photos are handled by _classify_photo (lone
# photo) or _classify_album (media group), both image-based, before this is reached.
# Inputs: InboundMessage. Outputs: Intent enum value; falls back to UNKNOWN on error.
def _classify_intent(msg: InboundMessage) -> Intent:
    prompt = _CLASSIFY_PROMPT.format(
        message_type=msg.message_type.value,
        text=msg.text or "—",
        caption=msg.caption or "—",
    )
    try:
        raw = generate_text(prompt, model=MODEL_FLASH)
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


# Classifies any photo by looking at the IMAGE (with its caption as extra context), and carries
# the fetched bytes back on the message so the chosen domain handler does not download it again.
# Downloads once, asks the LLM what kind of image it is (expense vs food vs weight). The image is
# the stronger signal so a receipt/payment photo routes to expense even when the caption reads like
# an activity (e.g. a laundry-payment screenshot captioned "did laundry").
# Inputs: InboundMessage with message_type=PHOTO and a file_id.
# Output: (message_with_file_bytes, Intent). On download/LLM failure: (original_msg, UNKNOWN).
def _classify_photo(msg: InboundMessage) -> tuple[InboundMessage, Intent]:
    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        image_bytes = get_file_bytes(msg.file_id, token)
    except Exception as e:
        log_failure(logger, logging.WARNING, "route_photo_fetch_failed", e, update_id=msg.update_id)
        return msg, Intent.UNKNOWN
    msg = dataclasses.replace(msg, file_bytes=image_bytes)
    try:
        prompt = _PHOTO_CLASSIFY_PROMPT.format(caption=msg.caption or "—")
        raw = generate_with_image(image_bytes, prompt, model=MODEL_FLASH)
        intent = _parse_intent(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "route_photo_intent_classification_failed", e,
                    update_id=msg.update_id)
        return msg, Intent.UNKNOWN
    log_event(logger, logging.INFO, "route_photo_intent_classified",
              update_id=msg.update_id, intent=intent.value,
              has_caption=bool(msg.caption), image_bytes=len(image_bytes))
    return msg, intent


# Classifies a Telegram album (media group) as ONE submission across all its photos in a single
# multi-image vision call. The "any financial document -> log_expense" rule means an ambiguous or
# non-financial first photo cannot gate a later payment screenshot away from expense.
# Carries the triggering photo's bytes on the message (the expense domain re-fetches the full album
# itself for extraction). On download/LLM failure, falls back to single-photo classification.
# Inputs: InboundMessage with media_group_file_ids (>1). Output: (message, Intent).
def _classify_album(msg: InboundMessage) -> tuple[InboundMessage, Intent]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    file_ids = msg.media_group_file_ids or []
    try:
        images = [get_file_bytes(fid, token) for fid in file_ids]
    except Exception as e:
        log_failure(logger, logging.WARNING, "route_album_fetch_failed", e, update_id=msg.update_id)
        return _classify_photo(msg)
    if not images:
        return _classify_photo(msg)
    # Carry the triggering photo's bytes so a single-photo code path stays warm.
    idx = file_ids.index(msg.file_id) if msg.file_id in file_ids else 0
    msg = dataclasses.replace(msg, file_bytes=images[idx])
    try:
        prompt = _ALBUM_CLASSIFY_PROMPT.format(caption=msg.caption or "—")
        raw = generate_with_images(images, prompt, model=MODEL_FLASH)
        intent = _parse_intent(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "route_album_intent_classification_failed", e,
                    update_id=msg.update_id)
        return _classify_photo(msg)
    log_event(logger, logging.INFO, "route_album_intent_classified",
              update_id=msg.update_id, intent=intent.value,
              image_count=len(images), has_caption=bool(msg.caption))
    return msg, intent


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
# Food corrections return a list (one reply per item); all other domains return a single-item list.
def _try_correction(msg: InboundMessage) -> list[tuple[str, dict | None]] | None:
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
        # No saved correction-state — but PROACTIVE / summary / all-eaten cards NEVER save it (only the
        # interactive /plan card does). If the quoted message is a tracked PIN, route by its kind so a quote
        # (especially a menu photo/album to the pinned MEAL card, to re-plan) reaches the planner instead of
        # falling through to the food/expense photo classifier — the 2026-07-07 bug where a menu album got
        # logged as food + an ignored expense. Only meal/week route (exercise = ambiguous run vs strength).
        try:
            pin_kind = pin_kind_for(msg.quoted_message_id)
        except Exception as e:
            log_failure(logger, logging.WARNING, "route_pin_fallback_lookup_failed", e,
                        update_id=msg.update_id, quoted_message_id=msg.quoted_message_id)
            pin_kind = None
        if pin_kind in ("meal", "week"):
            log_event(logger, logging.INFO, "route_correction_pin_fallback",
                      update_id=msg.update_id, quoted_message_id=msg.quoted_message_id, kind=pin_kind)
            return handle_plan_correction(msg, {"domain": "plan", "context": {"kind": pin_kind}})
        return None
    domain = state.get("domain")
    if domain == "food":
        return handle_food_correction(msg, state)  # already returns list
    if domain == "attention":
        return handle_attention_correction(msg, state)  # already returns list — one entry per affected session
    if domain == "aligner":
        return handle_aligner_correction(msg, state)  # already returns list
    if domain == "sleep_wake":
        return [handle_sleep_wake_correction(msg, state)]
    if domain == "weight":
        return [handle_weight_correction(msg, state)]
    if domain == "expense":
        return [handle_expense_correction(msg, state)]
    if domain == "plan":
        return handle_plan_correction(msg, state)  # already returns list; splits by context.kind
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
# Food logging and attention logging return a list of (reply, state) — one per food item
# or one per attention session block (end/start). All other handlers return a single
# (reply, state) wrapped in a list.
def _dispatch(intent: Intent, msg: InboundMessage) -> list[tuple[str, dict | None]]:
    if intent == Intent.LOG_FOOD:
        return handle_food_log(msg)  # already returns list
    if intent == Intent.LOG_WEIGHT:
        return [handle_weight_log(msg)]
    if intent == Intent.LOG_SLEEP:
        return handle_sleep_log(msg)  # already returns list — may include attention end blocks
    if intent == Intent.LOG_WAKE:
        # "Wake up" mid-nap should close the open rest attention session, not log a
        # wake event. try_handle_wake_as_nap_end returns None when no rest session is
        # open so we fall through to normal sleep/wake routing; when it returns, it's
        # already a list[(reply, state)] so no wrapping needed.
        nap_end = try_handle_wake_as_nap_end(msg)
        if nap_end is not None:
            return nap_end
        return handle_wake_log(msg)  # already returns list
    if intent == Intent.LOG_EXPENSE:
        return [handle_expense_log(msg)]
    if intent == Intent.LOG_ATTENTION:
        return handle_attention_log(msg)  # already returns list — one entry per session block
    if intent == Intent.ATTENTION_STATUS:
        return handle_attention_status(msg)  # already returns list — read-only status reply
    if intent == Intent.LOG_ALIGNER_OUT:
        return handle_aligner_out(msg)  # already returns list (with reply_markup)
    if intent == Intent.LOG_ALIGNER_IN:
        return handle_aligner_in(msg)  # already returns list (with reply_markup)
    if intent == Intent.ALIGNER_STATUS:
        return handle_aligner_status(msg)  # already returns list (with reply_markup; doubles as keyboard bootstrap)
    if intent == Intent.QUERY_DATA:
        return [handle_query_data(msg)]
    if intent == Intent.ASK_GENERAL:
        return [handle_general_ask(msg)]
    if intent == Intent.REFRESH_MENUS:
        return [handle_refresh_menus(msg)]
    if intent == Intent.VIEW_WEEK:
        return handle_week_view(msg)  # already returns list
    if intent == Intent.PLAN:
        return handle_plan(msg)  # already returns list (hub picker)
    return [("not sure what to do with that yet", None)]
