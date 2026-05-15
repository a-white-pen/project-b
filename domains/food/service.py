"""
Food logging domain — handles log_food intent.

Functions:
  handle_food_log(msg)            — extracts food items from B's message, inserts into nutrition.food_log,
                                    returns a formatted summary of what was logged
  _handle_photo(msg)              — handles PHOTO messages: downloads image, calls vision model,
                                    applies label backstops and zero-from-label rule
  _check_label_backstops(item)    — validates a nutrition_label item: rejects partial reads and row-shift mismatches
  _apply_zero_from_label(item)    — zeros missing secondary fields when all core label fields are present
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

IMPORTANT: Return ALL text fields (food_item, prep, brand, notes) in English, \
regardless of the language in the photo or caption.

Step 1 — determine photo type:
- nutrition_label: photo shows a nutrition facts panel, packaging label, or nutrition information table
- food_image: photo shows actual food, a plate, a dish, or a meal

============================================================
If photo_type is nutrition_label, follow Steps 2A → 2B → 2C:
============================================================

Step 2A — legibility check (do this FIRST):
Can you clearly read the nutrition values on this label? If the label is blurry, angled,
partially cut off, or any key values are illegible, return immediately — do not attempt extraction:
{{"status": "unreadable_label"}}

Step 2B — quantity check:
Is there a clear, unambiguous quantity in the caption?
Clear quantities: "150g", "30g", "2 servings", "half a bar", "1 cup", "200ml", "3 pieces".
If the quantity is missing, unclear, or you are in ANY doubt — return immediately:
{{"status": "needs_quantity"}}
When in doubt, ask — do not guess.

Step 2C — read and scale the values:
Nutrition labels may be in English, Thai, Simplified Chinese, or Traditional Chinese.
Return all numeric values in standard units (kcal, grams, milligrams).

Thai labels list rows TOP-TO-BOTTOM in this order:
  พลังงาน (energy/kcal), ไขมันทั้งหมด (total fat), ไขมันอิ่มตัว (saturated fat),
  คอเลสเตอรอล (cholesterol), โซเดียม (sodium), คาร์โบไฮเดรต (carbohydrates),
  ใยอาหาร (dietary fibre), น้ำตาล (sugars), โปรตีน (protein).
  PROTEIN IS NEAR THE BOTTOM — do not confuse it with rows near the top.

Simplified Chinese labels (mainland China):
  能量 (energy), 蛋白质 (protein), 脂肪 (fat), 碳水化合物 (carbohydrates), 钠 (sodium).
  Often use kJ — convert to kcal: divide by 4.184.

Traditional Chinese labels (Hong Kong / Taiwan):
  蛋白質 (protein), 脂肪 (fat), 碳水化合物 (carbohydrates), 鈉 (sodium).
  Taiwan labels often show values per 100g. HK labels follow UK/EU format.

Read: energy (kcal), protein (g), total carbohydrates (g), total fat (g),
and if present: dietary fibre (g), sugars (g), sodium (mg).
Scale the values by the quantity: (quantity consumed / serving size) × per-serving values.
Set macro_input="nutrition_label", macro_method="nutrition_label" for this item.

========================================
If photo_type is food_image, follow Step 3:
========================================

Step 3 — food image extraction:
- Identify each distinct food item visible in the image
- Use the caption for additional context (dish name, portion size, extras)
- Estimate portion sizes from visual cues, plate size, and typical serving sizes for this cuisine
- Estimate macros for each item
- Set macro_input="image", macro_method="llm" for these items

============================================================
For all photo types — Step 4 — caption-only items:
============================================================

If the caption mentions food items NOT visible in the image, extract those too.
Set macro_input="description", macro_method="llm" for caption-only items.
Do not double-count items already extracted from the image.

Return a JSON object with this exact structure:
{{
  "photo_type": "<nutrition_label or food_image>",
  "meal_type": "<one of: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout>",
  "macro_input": "<nutrition_label or image — top-level fallback for items missing their own>",
  "macro_method": "<nutrition_label or llm — top-level fallback>",
  "items": [
    {{
      "food_item": "<description in English>",
      "kcal": <number or null>,
      "protein_g": <number or null>,
      "carbs_g": <number or null>,
      "fat_g": <number or null>,
      "fibre_g": <number or null>,
      "sugar_g": <number or null>,
      "sodium_mg": <number or null>,
      "macro_input": "<nutrition_label, image, or description — per item>",
      "macro_method": "<nutrition_label or llm — per item>",
      "food_meta": {{
        "qty": {{"amount": <number>, "unit": "<string>"}},
        "prep": "<string in English>",
        "brand": "<string in English>",
        "notes": "<string in English>"
      }}
    }}
  ]
}}

Rules:
- meal_type: infer from local time if not stated in caption
- Each item carries its own macro_input and macro_method — these override the top-level fields
- food_meta keys are optional — omit if not meaningful for this item
- All text fields must be in English
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

    # Handle early-return status responses from the label flow (Steps 2A and 2B).
    status = extracted.get("status")
    if status == "unreadable_label":
        log_event(logger, logging.INFO, "food_photo_label_unreadable", update_id=msg.update_id)
        # Save state only when there is a caption worth preserving — so B does not have to retype
        # the quantity or food name when resending a clearer photo.
        pending_state = None
        if msg.caption:
            pending_state = {
                "domain": "food",
                "context": {
                    "awaiting_clearer_photo": True,
                    "original_caption": msg.caption,
                    "file_ids": [msg.file_id],
                },
            }
        return ("Can't read the label clearly — could you send a clearer photo, or type the values?", pending_state)
    if status == "needs_quantity":
        log_event(logger, logging.INFO, "food_photo_needs_quantity", update_id=msg.update_id)
        # Save state so B can reply with just the quantity — correction handler re-downloads
        # the photo and re-runs extraction without B needing to resend.
        pending_state = {
            "domain": "food",
            "context": {"awaiting_quantity": True, "file_ids": [msg.file_id]},
        }
        return (
            "I can see the label — how much did you have? (e.g. '150g', '1 serving', 'half a bar')",
            pending_state,
        )

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

    # Top-level macro_input/macro_method are the fallback for items that don't carry their own.
    # Items extracted from the caption (not the image) carry macro_input="description" per-item.
    top_macro_input = extracted.get("macro_input", "image")
    top_macro_method = extracted.get("macro_method", "llm")

    # Stamp each item with its own provenance, falling back to the top-level value.
    # This ensures caption-only items are recorded as "description/llm", not "nutrition_label".
    # macro_meta is also per-item: image items include the Telegram file_id when applicable,
    # but caption-only items must NOT inherit that file_id (their macro_method is "llm", not
    # "nutrition_label", so the schema contract for macro_meta is different).
    image_macro_meta: dict = {"model": MODEL_FLASH}
    if top_macro_input == "nutrition_label" and msg.file_id:
        image_macro_meta["file_id"] = msg.file_id
    caption_macro_meta: dict = {"model": MODEL_FLASH}

    for item in items:
        if "macro_input" not in item:
            item["macro_input"] = top_macro_input
        if "macro_method" not in item:
            item["macro_method"] = top_macro_method
        # Each item gets its own copy of the macro_meta dict so backstops and zero-from-label
        # can write per-item field_sources without mutating the shared template.
        if "macro_meta" not in item:
            base = caption_macro_meta if item["macro_input"] == "description" else image_macro_meta
            item["macro_meta"] = dict(base)

    # Apply label backstops to every nutrition_label item before touching the DB.
    # If any item fails, return an error — do not log partial results.
    for item in items:
        if item.get("macro_input") == "nutrition_label":
            ok, reason = _check_label_backstops(item)
            if not ok:
                log_event(
                    logger, logging.WARNING, "food_photo_label_backstop_failed",
                    update_id=msg.update_id, reason=reason,
                    food_item_chars=len(item.get("food_item") or ""),
                )
                return (reason, None)

    # Apply zero-from-label rule: if all four core fields are present on a label item,
    # zero-fill any missing secondary fields (fibre_g, sugar_g, sodium_mg) and record
    # which fields were zeroed in macro_meta["field_sources"] for provenance.
    for item in items:
        if item.get("macro_input") == "nutrition_label":
            _apply_zero_from_label(item)

    # Batch-level macro_meta is the image meta — used as the fallback in _insert_items
    # for any item that somehow still lacks a per-item override (defensive only).
    macro_meta = image_macro_meta

    log_event(
        logger,
        logging.INFO,
        "food_photo_provenance",
        update_id=msg.update_id,
        image_items=sum(1 for i in items if i.get("macro_input") in ("nutrition_label", "image")),
        caption_items=sum(1 for i in items if i.get("macro_input") == "description"),
    )

    try:
        food_log_ids = _insert_items(
            items=items,
            meal_type=meal_type,
            update_id=msg.update_id,
            source="telegram",
            macro_input=top_macro_input,
            macro_method=top_macro_method,
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
        macro_method=top_macro_method,
    )
    reply = _format_reply(meal_type, items, top_macro_method)
    state = {"domain": "food", "context": {"food_log_ids": food_log_ids, "meal_type": meal_type}}
    return (reply, state)


# Returns B's timezone as-of a given event timestamp, falling back to Asia/Singapore.
# Queries b.location for the most recent row at or before `as_of` so timezone resolves
# to wherever B actually was when the message was sent — not where she is right now.
# This handles delayed messages, Telegram retries, and travel between meals correctly.
#
# Fallback chain (in order):
#   1. Most recent b.location row at or before as_of  (correct as-of lookup)
#   2. Most recent b.location row regardless of time  (handles no prior-to-event row)
#   3. Asia/Singapore hardcoded                       (no location ever shared)
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
                    row = cur.fetchone()
                    if row:
                        log_event(logger, logging.INFO, "food_timezone_resolved",
                                  source="as_of", timezone=row[0], as_of=as_of.isoformat())
                    else:
                        # No location at-or-before this event — use the most recent one anyway.
                        log_event(logger, logging.WARNING, "food_timezone_as_of_miss",
                                  as_of=as_of.isoformat(), as_of_tzinfo=str(as_of.tzinfo))
                        cur.execute("SELECT timezone FROM b.latest_location")
                        row = cur.fetchone()
                        if row:
                            log_event(logger, logging.INFO, "food_timezone_resolved",
                                      source="latest_location", timezone=row[0])
                else:
                    cur.execute("SELECT timezone FROM b.latest_location")
                    row = cur.fetchone()
                    if row:
                        log_event(logger, logging.INFO, "food_timezone_resolved",
                                  source="latest_location_no_as_of", timezone=row[0])
                if row:
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
                    # Per-item overrides take priority over batch-level defaults.
                    # _handle_photo stamps each item with its own macro_input, macro_method,
                    # and macro_meta so caption-only items are stored with the correct shape.
                    item_macro_input = item.get("macro_input") or macro_input
                    item_macro_method = item.get("macro_method") or macro_method
                    item_macro_meta = item.get("macro_meta") or macro_meta
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
                        item_macro_input,
                        item_macro_method,
                        psycopg2.extras.Json(item_macro_meta),
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


# Validates extracted nutrition label values before DB insert.
# Returns (True, "") if valid, or (False, user-facing error message) if validation fails.
#
# Two checks:
#   Partial read  — both protein_g and carbs_g are null → extraction was incomplete.
#   Row-shift     — |kcal - (4P + 4C + 9F)| / max(kcal, computed) > 0.60 → values assigned
#                   to wrong rows (common on angled Thai/Chinese labels).
#
# Always uses _to_float() before arithmetic — LLM values may arrive as strings.
def _check_label_backstops(item: dict) -> tuple[bool, str]:
    kcal    = _to_float(item.get("kcal"))
    protein = _to_float(item.get("protein_g"))
    carbs   = _to_float(item.get("carbs_g"))
    fat     = _to_float(item.get("fat_g"))

    if protein is None and carbs is None:
        return (
            False,
            "I could see the label but couldn't read all the values clearly — "
            "could you send a clearer photo, or type the values?",
        )

    if kcal is not None and protein is not None and carbs is not None and fat is not None:
        computed = 4.0 * protein + 4.0 * carbs + 9.0 * fat
        denom = max(kcal, computed)
        if denom > 0 and abs(kcal - computed) / denom > 0.60:
            return (
                False,
                "The values on that label don't add up — the photo may be angled or hard to read. "
                "Could you send a clearer photo, or type the values?",
            )

    return (True, "")


# Applies the zero-from-label rule to a nutrition_label item in-place.
#
# If all four core fields (kcal, protein_g, carbs_g, fat_g) are present, any missing
# secondary fields (fibre_g, sugar_g, sodium_mg) are set to 0.0. A label that declares
# all four core macros but omits fibre is genuinely saying fibre is 0 (or undeclared
# in that market) — do not leave it NULL and trigger an LLM fill later.
#
# Zero-filled fields are recorded in item["macro_meta"]["field_sources"] for provenance.
# Does nothing if any core field is missing (incomplete read — caller should have caught
# this via _check_label_backstops, but this is defensive).
#
# Only called for items with macro_input="nutrition_label". Never called for image,
# description, manual, or restaurant_reported items.
def _apply_zero_from_label(item: dict) -> None:
    core      = ("kcal", "protein_g", "carbs_g", "fat_g")
    secondary = ("fibre_g", "sugar_g", "sodium_mg")

    if any(_to_float(item.get(f)) is None for f in core):
        return

    zeroed = [f for f in secondary if _to_float(item.get(f)) is None]
    if not zeroed:
        return

    for field in zeroed:
        item[field] = 0.0

    meta = item.setdefault("macro_meta", {})
    sources = meta.setdefault("field_sources", {})
    for field in zeroed:
        sources[field] = {"source": "nutrition_label", "status": "zero_from_label"}


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
