"""
Food logging domain — handles log_food intent.

Functions:
  handle_food_log(msg) — extracts food items from B's message, inserts into nutrition.food_log,
                         returns a formatted summary of what was logged
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text, generate_with_image
from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from telegram.files import get_file_bytes

logger = logging.getLogger(__name__)

_FALLBACK_TZ = ZoneInfo("Asia/Singapore")  # used when b.latest_location has no rows

_VALID_MEAL_TYPES = {
    "breakfast", "brunch", "lunch", "snack",
    "dinner", "supper", "pre_workout", "post_workout",
}

_EXTRACT_PROMPT = """\
You are extracting food log entries for a personal nutrition tracker.

About the user: Singaporean Chinese, based between Singapore and Bangkok. \
Eats Singaporean, Thai, and Southern Chinese food on top of regular western fare. \
Common meals include hawker dishes (chicken rice, char kway teow, laksa, wonton noodles), \
Thai dishes (pad kra pao, moo ping, khao soi, boat noodles), and Southern Chinese food \
(dim sum, congee, braised meats). Use this context when estimating portion sizes and macros.

Current local time: {local_time}
Message from user: {text}

Extract each distinct food item mentioned. Return a JSON object with this exact structure:
{{
  "meal_type": "<one of: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout>",
  "macro_input": "<one of: description, nutrition_label, restaurant_reported, image, manual>",
  "macro_method": "<one of: llm, nutrition_label, restaurant_reported, usda, open_foods, edamam, manual>",
  "items": [
    {{
      "food_item": "<description of the item>",
      "kcal": <number or null>,
      "protein_g": <number or null>,
      "carbs_g": <number or null>,
      "fat_g": <number or null>,
      "fibre_g": <number or null>,
      "sugar_g": <number or null>,
      "sodium_mg": <number or null>,
      "food_meta": {{
        "qty": {{"amount": <number>, "unit": "<string>"}},
        "prep": "<string>",
        "brand": "<string>",
        "notes": "<string>"
      }}
    }}
  ]
}}

Rules:
- meal_type: infer from the local time if not stated in the message.
- macro_input: use "description" for text descriptions. Use "nutrition_label" only if user mentions a label.
- macro_method: use "llm" since you are estimating from description.
- Estimate macros as accurately as you can from nutritional knowledge. Only use null if truly unknowable.
- food_meta keys are optional — omit any key that is not meaningful for this item.
- A named dish is ONE item even if it contains multiple components. "Chicken rice" = 1 item (not chicken + rice separately). "Laksa" = 1 item. Only split into multiple items when B explicitly lists separate things (e.g. "2 eggs, yoghurt, blueberries").
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

_PHOTO_EXTRACT_PROMPT = """\
You are extracting food log entries from a photo for a personal nutrition tracker.

About the user: Singaporean Chinese, based between Singapore and Bangkok. \
Eats Singaporean, Thai, and Southern Chinese food on top of regular western fare. \
Common meals include hawker dishes (chicken rice, char kway teow, laksa, wonton noodles), \
Thai dishes (pad kra pao, moo ping, khao soi, boat noodles), and Southern Chinese food \
(dim sum, congee, braised meats). Use this context when estimating portion sizes and macros.

Current local time: {local_time}
Caption from user (may be empty): {caption}

Examine the image and caption together.

Step 1 — determine photo type:
- nutrition_label: photo shows a nutrition facts panel, packaging label, or nutrition information table
- food_image: photo shows actual food, a plate, a dish, or a meal

Step 2 — extract based on type:

If nutrition_label:
- Read macro values directly from the label per serving
- Check the caption for quantity consumed (e.g. "150g", "2 servings", "half a bar")
- If the caption does NOT specify how much was consumed, set needs_quantity=true and return immediately with empty items
- If quantity is known, pro-rate the macros: (consumed / serving_size) × per_serving_macros
- Set macro_input="nutrition_label", macro_method="nutrition_label"

If food_image:
- Identify each distinct food item visible
- Use the caption for additional context (dish name, portion size, extras)
- Estimate portion sizes from visual cues, plate size, and typical serving sizes for this cuisine
- Estimate macros for each item
- Set macro_input="image", macro_method="llm", needs_quantity=false

Return a JSON object with this exact structure:
{{
  "photo_type": "<nutrition_label or food_image>",
  "needs_quantity": <true or false>,
  "meal_type": "<one of: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout>",
  "macro_input": "<nutrition_label or image>",
  "macro_method": "<nutrition_label or llm>",
  "items": [
    {{
      "food_item": "<description>",
      "kcal": <number or null>,
      "protein_g": <number or null>,
      "carbs_g": <number or null>,
      "fat_g": <number or null>,
      "fibre_g": <number or null>,
      "sugar_g": <number or null>,
      "sodium_mg": <number or null>,
      "food_meta": {{
        "qty": {{"amount": <number>, "unit": "<string>"}},
        "prep": "<string>",
        "brand": "<string>",
        "notes": "<string>"
      }}
    }}
  ]
}}

Rules:
- If needs_quantity=true, items should be empty
- meal_type: infer from local time if not stated in caption
- food_meta keys are optional — omit if not meaningful
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles a food logging request from B.
# Inputs: InboundMessage with text or photo.
# Outputs: (reply string, pending_state dict | None). pending_state is non-None when items were logged.
def handle_food_log(msg: InboundMessage) -> tuple[str, dict | None]:
    log_event(
        logger,
        logging.INFO,
        "food_log_started",
        update_id=msg.update_id,
        message_type=msg.message_type.value,
        has_text=bool(msg.text),
        has_caption=bool(msg.caption),
    )
    if msg.message_type == MessageType.PHOTO:
        return _handle_photo(msg)

    text = msg.text or msg.caption
    if not text:
        log_event(logger, logging.WARNING, "food_log_missing_text", update_id=msg.update_id)
        return ("I didn't catch what you ate — can you describe it in text?", None)

    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_text(
            _EXTRACT_PROMPT.format(local_time=local_time, text=text),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_extraction_failed", e, update_id=msg.update_id)
        return ("Couldn't parse what you ate — can you rephrase?", None)

    items = extracted.get("items", [])
    log_event(
        logger,
        logging.INFO,
        "food_extraction_completed",
        update_id=msg.update_id,
        item_count=len(items),
    )
    if not items:
        log_event(logger, logging.WARNING, "food_extraction_empty", update_id=msg.update_id)
        return ("Couldn't identify any food items — can you rephrase?", None)

    meal_type = extracted.get("meal_type", "snack")
    if meal_type not in _VALID_MEAL_TYPES:
        log_event(
            logger,
            logging.WARNING,
            "food_invalid_meal_type",
            update_id=msg.update_id,
            meal_type=meal_type,
        )
        meal_type = "snack"
    macro_input = extracted.get("macro_input", "description")
    macro_method = extracted.get("macro_method", "llm")
    macro_meta = {"model": MODEL_FLASH}

    try:
        food_log_ids = _insert_items(
            items=items,
            meal_type=meal_type,
            update_id=msg.update_id,
            source="telegram",
            macro_input=macro_input,
            macro_method=macro_method,
            macro_meta=macro_meta,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_insert_failed", e, update_id=msg.update_id)
        return ("Logged the intent but failed to save — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "food_inserted",
        update_id=msg.update_id,
        item_count=len(food_log_ids),
        meal_type=meal_type,
        macro_method=macro_method,
    )
    reply = _format_reply(meal_type, items, macro_method)
    state = {"domain": "food", "context": {"food_log_ids": food_log_ids, "meal_type": meal_type}}
    return (reply, state)


# Handles a food photo — nutrition label or food image.
# Downloads the image, sends to Gemini vision with caption for full context.
# Returns (reply, state). state is None if nothing was logged (needs_quantity, errors).
def _handle_photo(msg: InboundMessage) -> tuple[str, dict | None]:
    if not msg.file_id:
        log_event(logger, logging.WARNING, "food_photo_missing_file_id", update_id=msg.update_id)
        return ("Couldn't access the photo — please try again.", None)

    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        image_bytes = get_file_bytes(msg.file_id, token)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_download_failed", e, update_id=msg.update_id)
        return ("Couldn't download the photo — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "food_photo_downloaded",
        update_id=msg.update_id,
        image_byte_count=len(image_bytes),
    )

    caption = msg.caption or ""
    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_PHOTO_EXTRACT_PROMPT.format(local_time=local_time, caption=caption),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_extraction_failed", e, update_id=msg.update_id)
        return ("Couldn't read the photo — can you try again or describe it in text?", None)

    if extracted.get("needs_quantity"):
        log_event(logger, logging.INFO, "food_photo_needs_quantity", update_id=msg.update_id)
        return ("I can see the label but need to know how much you had. Resend the photo with a caption (e.g. '150g', '1 serving', 'half a bar').", None)

    items = extracted.get("items", [])
    log_event(
        logger,
        logging.INFO,
        "food_photo_extraction_completed",
        update_id=msg.update_id,
        photo_type=extracted.get("photo_type"),
        item_count=len(items),
    )
    if not items:
        log_event(logger, logging.WARNING, "food_photo_extraction_empty", update_id=msg.update_id)
        return ("Couldn't identify any food in the photo — can you describe it in text?", None)

    meal_type = extracted.get("meal_type", "snack")
    if meal_type not in _VALID_MEAL_TYPES:
        log_event(
            logger,
            logging.WARNING,
            "food_photo_invalid_meal_type",
            update_id=msg.update_id,
            meal_type=meal_type,
        )
        meal_type = "snack"

    macro_input = extracted.get("macro_input", "image")
    macro_method = extracted.get("macro_method", "llm")
    macro_meta: dict = {"model": MODEL_FLASH}
    if macro_input == "nutrition_label" and msg.file_id:
        macro_meta["file_id"] = msg.file_id

    try:
        food_log_ids = _insert_items(
            items=items,
            meal_type=meal_type,
            update_id=msg.update_id,
            source="telegram",
            macro_input=macro_input,
            macro_method=macro_method,
            macro_meta=macro_meta,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_insert_failed", e, update_id=msg.update_id)
        return ("Logged the intent but failed to save — please try again.", None)

    log_event(
        logger,
        logging.INFO,
        "food_photo_inserted",
        update_id=msg.update_id,
        item_count=len(food_log_ids),
        meal_type=meal_type,
        macro_method=macro_method,
    )
    reply = _format_reply(meal_type, items, macro_method)
    state = {"domain": "food", "context": {"food_log_ids": food_log_ids, "meal_type": meal_type}}
    return (reply, state)


# Returns B's timezone as-of a given event timestamp, falling back to Asia/Singapore.
# Queries b.location for the most recent row at or before `as_of` so timezone resolves
# to wherever B actually was when the message was sent — not where she is right now.
# This handles delayed messages, Telegram retries, and travel between meals correctly.
# Falls back to b.latest_location (most recent row regardless of time) if as_of is None,
# and to Asia/Singapore if the table is empty or unreachable.
def _get_timezone(as_of: datetime | None = None) -> ZoneInfo:
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                if as_of is not None:
                    cur.execute(
                        "SELECT timezone FROM b.location"
                        " WHERE created_at <= %s ORDER BY created_at DESC LIMIT 1",
                        (as_of,),
                    )
                else:
                    cur.execute("SELECT timezone FROM b.latest_location")
                row = cur.fetchone()
                if row:
                    log_event(logger, logging.INFO, "food_timezone_resolved", timezone=row[0], as_of=as_of.isoformat() if as_of else None)
                    return ZoneInfo(row[0])
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_timezone_lookup_failed", e)
    log_event(logger, logging.INFO, "food_timezone_fallback_used", timezone=str(_FALLBACK_TZ))
    return _FALLBACK_TZ


# Returns B's local time at the given event timestamp as a readable string for the LLM prompt.
# Resolves timezone as-of the event so meal_type inference is correct for delayed messages.
def _local_time_str(as_of: datetime | None = None) -> str:
    tz = _get_timezone(as_of)
    # Use the event's own timestamp if available; otherwise use current time in that zone.
    if as_of is not None:
        local_now = as_of.astimezone(tz)
    else:
        local_now = datetime.now(tz=tz)
    return local_now.strftime("%H:%M on %A")  # e.g. "08:30 on Monday"


# Strips markdown code fences if the LLM wraps its response, then parses JSON.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    return json.loads(cleaned)


# Safely coerces a macro value to float. Returns None if missing or not numeric.
def _to_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


# Inserts one row per food item into nutrition.food_log.
# Returns the list of food_log_ids assigned by the DB (used to write conversation_state.context).
def _insert_items(
    items: list[dict],
    meal_type: str,
    update_id: int | None,
    source: str,
    macro_input: str,
    macro_method: str,
    macro_meta: dict,
) -> list[int]:
    sql = """
        INSERT INTO nutrition.food_log (
            meal_type, telegram_update_id, food_item, food_meta,
            kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
            source, macro_input, macro_method, macro_meta
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        ) RETURNING food_log_id
    """
    ids: list[int] = []
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                for item in items:
                    food_meta = item.get("food_meta") or {}
                    cur.execute(sql, (
                        meal_type,
                        update_id,
                        item.get("food_item"),
                        psycopg2.extras.Json(food_meta) if food_meta else None,
                        _to_float(item.get("kcal")),
                        _to_float(item.get("protein_g")),
                        _to_float(item.get("carbs_g")),
                        _to_float(item.get("fat_g")),
                        _to_float(item.get("fibre_g")),
                        _to_float(item.get("sugar_g")),
                        _to_float(item.get("sodium_mg")),
                        source,
                        macro_input,
                        macro_method,
                        psycopg2.extras.Json(macro_meta),
                    ))
                    row = cur.fetchone()
                    if row:
                        ids.append(row[0])
    finally:
        conn.close()
    log_event(
        logger,
        logging.INFO,
        "food_db_insert_completed",
        update_id=update_id,
        item_count=len(ids),
        meal_type=meal_type,
        macro_input=macro_input,
        macro_method=macro_method,
    )
    return ids


# Formats the reply shown to B after logging.
def _format_reply(meal_type: str, items: list[dict], macro_method: str) -> str:
    lines = [f"Logged for {meal_type.replace('_', ' ')}:"]
    for item in items:
        line = f"• {item.get('food_item', '?')}"
        macros = []
        if _to_float(item.get("kcal")) is not None:
            macros.append(f"{_to_float(item['kcal']):.0f} kcal")
        if _to_float(item.get("protein_g")) is not None:
            macros.append(f"{_to_float(item['protein_g']):.0f}g protein")
        if _to_float(item.get("fat_g")) is not None:
            macros.append(f"{_to_float(item['fat_g']):.0f}g fat")
        if _to_float(item.get("carbs_g")) is not None:
            macros.append(f"{_to_float(item['carbs_g']):.0f}g carbs")
        if macros:
            line += " — " + ", ".join(macros)
        lines.append(line)

    lines.append("")
    if macro_method == "llm":
        lines.append("Macros estimated by LLM. Anything to change?")
    else:
        lines.append("Anything to change?")

    return "\n".join(lines)
