"""
Food logging domain — handles log_food intent.

Functions:
  handle_food_log(msg) — extracts food items from B's message, inserts into nutrition.food_log,
                         returns a formatted summary of what was logged
"""

import json
import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

# B's default timezone. TODO: retrieve from b.location when that table exists.
_DEFAULT_TZ = ZoneInfo("Asia/Bangkok")

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
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles a food logging request from B.
# Inputs: InboundMessage with text describing food consumed.
# Outputs: reply string summarising what was logged.
def handle_food_log(msg: InboundMessage) -> str:
    text = msg.text or msg.caption
    if not text:
        return "I didn't catch what you ate — can you describe it in text?"

    local_time = _local_time_str()

    try:
        raw = generate_text(
            _EXTRACT_PROMPT.format(local_time=local_time, text=text),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        logger.error("food extraction failed update_id=%s: %s", msg.update_id, e)
        return "Couldn't parse what you ate — can you rephrase?"

    items = extracted.get("items", [])
    if not items:
        return "Couldn't identify any food items — can you rephrase?"

    meal_type = extracted.get("meal_type", "snack")
    if meal_type not in _VALID_MEAL_TYPES:
        logger.warning("update_id=%s unrecognised meal_type=%r, defaulting to snack", msg.update_id, meal_type)
        meal_type = "snack"
    macro_input = extracted.get("macro_input", "description")
    macro_method = extracted.get("macro_method", "llm")
    macro_meta = {"model": MODEL_FLASH}

    try:
        _insert_items(
            items=items,
            meal_type=meal_type,
            update_id=msg.update_id,
            source="telegram",
            macro_input=macro_input,
            macro_method=macro_method,
            macro_meta=macro_meta,
        )
    except Exception as e:
        logger.error("food insert failed update_id=%s: %s", msg.update_id, e)
        return "Logged the intent but failed to save — please try again."

    return _format_reply(meal_type, items, macro_method)


# Returns B's current local time as a readable string for the LLM prompt.
def _local_time_str() -> str:
    local_now = datetime.now(tz=_DEFAULT_TZ)
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
def _insert_items(
    items: list[dict],
    meal_type: str,
    update_id: int | None,
    source: str,
    macro_input: str,
    macro_method: str,
    macro_meta: dict,
) -> None:
    sql = """
        INSERT INTO nutrition.food_log (
            meal_type, telegram_update_id, food_item, food_meta,
            kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
            source, macro_input, macro_method, macro_meta
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
    """
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
    finally:
        conn.close()


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
