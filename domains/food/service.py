"""
Food logging domain — handles log_food intent.

Three-way macro source routing (A3) — two LLM calls per photo:
  Call 1: _classify_photo — fast one-word classifier (nutrition_label / macro_screenshot / food_image)
  Call 2: path-specific extraction prompt (_LABEL_PROMPT / _SCREENSHOT_PROMPT / _FOOD_IMAGE_PROMPT)

  Path 1 — nutrition_label: macros read from packaged food label. Backstops + zero-from-label applied.
  Path 2 — macro_screenshot: macros from printed non-label source. Source provenance + gap-fill.
  Path 3 — food_image: macros looked up via USDA/OFF (A5) by food type; LLM fallback if no match.

All public handlers return list[tuple[str, dict | None]] — one (reply, state) pair per food item.
Each item gets its own Telegram message so B can quote exactly the item she wants to correct.

Functions:
  handle_food_log(msg)                      — dispatches photo vs text; returns list of (reply, state)
  _format_item_reply(meal_type, item)       — formats one item as an HTML Telegram message
  _handle_text_items(msg, items, extracted) — processes text items; returns list of (reply, state)
  _handle_photo(msg)                        — downloads image, classifies, dispatches
  _classify_photo(image_bytes, update_id)   — Call 1: photo type classifier
  _resolve_meal_type(extracted, update_id)  — validates and defaults meal_type
  _stamp_photo_provenance(items, ...)       — stamps macro_input/method/meta on extracted items
  _handle_label_photo(msg, image_bytes)         — Path 1: label extraction + backstops + insert
  _handle_macro_screenshot_photo(msg, image_bytes) — Path 2: screenshot extraction + gap-fill + insert
  _handle_food_image_photo(msg, image_bytes)    — Path 3: food image extraction + insert
  _gap_fill_macros(item, update_id)         — fills null macro fields anchored to known values
  _check_label_backstops(item)              — validates nutrition_label item: partial read + row-shift
  _apply_zero_from_label(item)              — zeros missing secondary fields when all core fields present
  _get_timezone(as_of)                      — looks up B's timezone as-of a given timestamp
  _local_time_str(as_of)                    — formats B's local time for LLM prompts
  _parse_json(raw)                          — strips code fences and parses JSON
  _to_float(val)                            — safely coerces a macro value to float
  _insert_items(items, ...)                 — inserts food_log rows, returns food_log_ids
"""

import html
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
from domains.food.nutrition_sources.router import enrich_item

logger = logging.getLogger(__name__)

_FALLBACK_TZ = ZoneInfo("Asia/Singapore")  # used when b.latest_location has no rows

_VALID_MEAL_TYPES = {
    "breakfast", "brunch", "lunch", "snack",
    "dinner", "supper", "pre_workout", "post_workout",
}

_MEAL_LABELS: dict[str, str] = {
    "breakfast": "Breakfast",
    "brunch": "Brunch",
    "lunch": "Lunch",
    "snack": "Snack",
    "dinner": "Dinner",
    "supper": "Supper",
    "pre_workout": "Pre-workout",
    "post_workout": "Post-workout",
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
      "stated_fields": ["<field_name>", ...],
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
- stated_fields: list only the macro field names (from: kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg) where B explicitly gave a numeric value in the message. Empty list if B gave no explicit values. Never list estimated or inferred fields.
- food_meta keys are optional — omit any key that is not meaningful for this item.
- A named dish is ONE item even if it contains multiple components. "Chicken rice" = 1 item (not chicken + rice separately). "Laksa" = 1 item. Only split into multiple items when B explicitly lists separate things (e.g. "2 eggs, yoghurt, blueberries").
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

# Call 1 — single-word photo classifier. Anchored to first token.
# Distinguishes the standardised packaged-food label format from other macro sources and food photos.
_CLASSIFY_PROMPT = """\
Classify this photo. Return exactly one word — nothing else.

nutrition_label — the standardised nutrition facts panel printed ON packaged food \
(government-mandated table format with rows for energy, protein, fat, carbohydrates). \
Must include a sodium row — sodium declaration is required by law on all government-mandated panels. \
A simplified macro display showing only 4 values (kcal/carbs/protein/fat) without sodium is \
macro_screenshot, even if printed on the packaging.
macro_screenshot — any other image with printed nutrition numbers: \
restaurant menu, meal plan, app screenshot, printed flyer, or simplified meal-service macro card
food_image — food, a meal, or anything without explicit nutrition numbers\
"""

# Call 2a — label extraction. Only sent when classifier returns nutrition_label.
_LABEL_PROMPT = """\
Extract nutrition values from this packaged food label.

Caption: {caption}
Time: {local_time}

A — Legibility: if the label is blurry, angled, or key values are illegible, return:
{{"status": "unreadable_label"}}

B — Quantity: if no clear quantity is in the caption, return:
{{"status": "needs_quantity"}}
Clear quantities: "150g", "2 servings", "1 cup", "200ml". When in doubt, ask — do not guess.

C — Read and scale.
Supported languages: English, Thai, Simplified Chinese, Traditional Chinese.
Thai row order (top→bottom): energy → fat → sat.fat → cholesterol → sodium → carbs → fibre → sugars → protein. PROTEIN IS NEAR THE BOTTOM.
Simplified Chinese: 能量 (kJ — divide by 4.184 for kcal), 蛋白质, 脂肪, 碳水化合物, 钠
Traditional Chinese: 蛋白質, 脂肪, 碳水化合物, 鈉

Read: kcal, protein_g, carbs_g, fat_g, and if shown: fibre_g, sugar_g, sodium_mg.
Scale by quantity: (qty consumed ÷ serving size) × per-serving values.
If caption mentions food not visible in the label photo, include as separate items \
with macro_input="description", macro_method="llm".

Return JSON:
{{
  "meal_type": "<breakfast|brunch|lunch|snack|dinner|supper|pre_workout|post_workout>",
  "items": [{{
    "food_item": "<name in English>",
    "kcal": <number or null>, "protein_g": <number or null>, "carbs_g": <number or null>,
    "fat_g": <number or null>, "fibre_g": <number or null>, "sugar_g": <number or null>,
    "sodium_mg": <number or null>,
    "macro_input": "nutrition_label", "macro_method": "nutrition_label",
    "food_meta": {{"qty": {{"amount": <number>, "unit": "<string>"}}, "brand": "<string>"}}
  }}]
}}
All text in English. Valid JSON only. No markdown.\
"""

# Call 2b — macro screenshot extraction. Only sent when classifier returns macro_screenshot.
_SCREENSHOT_PROMPT = """\
Read the printed nutrition numbers from this image.

Caption: {caption}
Time: {local_time}

Read only values explicitly printed. Return null for anything not shown — do not estimate.
If multiple food items are shown, extract each separately.
If caption mentions food not visible in the image, include as separate items \
with macro_input="description", macro_method="llm".

Return JSON:
{{
  "meal_type": "<breakfast|brunch|lunch|snack|dinner|supper|pre_workout|post_workout>",
  "items": [{{
    "food_item": "<name in English>",
    "kcal": <number or null>, "protein_g": <number or null>, "carbs_g": <number or null>,
    "fat_g": <number or null>, "fibre_g": <number or null>, "sugar_g": <number or null>,
    "sodium_mg": <number or null>,
    "macro_input": "macro_screenshot", "macro_method": "restaurant_reported",
    "food_meta": {{"qty": {{"amount": <number>, "unit": "<string>"}}, "brand": "<string>"}}
  }}]
}}
All text in English. Valid JSON only. No markdown.\
"""

# Call 2c — food image extraction. Only sent when classifier returns food_image.
_FOOD_IMAGE_PROMPT = """\
Identify food and estimate macros from this photo.

Caption: {caption}
Time: {local_time}
User: Singaporean Chinese, based between Singapore and Bangkok. \
Common meals: chicken rice, char kway teow, laksa, pad kra pao, moo ping, khao soi, \
dim sum, congee, braised meats.

Identify each distinct food item visible. Use caption for dish name, portion, and extras. \
Estimate portion sizes and macros.
Do not log sauces, condiments, dressings, or dipping sauces as separate items unless the \
caption explicitly names them.
If caption mentions food not visible in the image, include as separate items \
with macro_input="description", macro_method="llm".

Return JSON:
{{
  "meal_type": "<breakfast|brunch|lunch|snack|dinner|supper|pre_workout|post_workout>",
  "items": [{{
    "food_item": "<name in English>",
    "kcal": <number or null>, "protein_g": <number or null>, "carbs_g": <number or null>,
    "fat_g": <number or null>, "fibre_g": <number or null>, "sugar_g": <number or null>,
    "sodium_mg": <number or null>,
    "macro_input": "image", "macro_method": "llm",
    "food_meta": {{"qty": {{"amount": <number>, "unit": "<string>"}}, "prep": "<string>", "notes": "<string>"}}
  }}]
}}
All text in English. Valid JSON only. No markdown.\
"""

_GAP_FILL_PROMPT = """\
You are filling in missing nutrition fields for a food log entry.

Food item: {food_item}

Known values — these are exact and authoritative. Do not change them:
{known_lines}

Estimate ONLY these missing fields: {missing_list}

Rules:
- If kcal is known, your estimates for protein, carbs, and fat must be consistent with it: \
4 kcal/g protein, 4 kcal/g carbs, 9 kcal/g fat.
- Use nutritional knowledge anchored to the known values — not free estimation.
- Return only the missing fields as a JSON object. Do not include the known fields.
- All values as numbers. No null values. No explanation. No markdown.\
"""


# Builds a formatted candidate list string from macro_meta for appending to the item reply.
# Reads candidate_letter_map and source_candidates to construct the lettered list.
# Returns an empty string if no candidate_letter_map is present.
#
# Format:
#   a. Chicken breast, raw, boneless ✓
#   b. Chicken breast, cooked, roasted
#   ...
#   d. Try Open Food Facts
#   e. Use LLM estimate
def _build_candidate_list(macro_meta: dict) -> str:
    letter_map: dict = macro_meta.get("candidate_letter_map") or {}
    source_candidates: list = macro_meta.get("source_candidates") or []
    if not letter_map:
        return ""

    _SOURCE_DISPLAY = {
        "usda": "USDA",
        "open_food_facts": "Open Food Facts",
    }

    lines: list[str] = []
    for letter, action in sorted(letter_map.items()):
        act = action.get("action")
        if act == "candidate":
            idx = action.get("index", 0)
            if idx < len(source_candidates):
                cand = source_candidates[idx]
                label = html.escape(str(cand.get("label", "?")))
                brand = cand.get("brand", "")
                brand_str = f" ({html.escape(brand)})" if brand else ""
                checkmark = " ✓" if idx == 0 else ""
                lines.append(f"{letter}. {label}{brand_str}{checkmark}")
        elif act == "cross_source":
            src = action.get("source", "")
            display = _SOURCE_DISPLAY.get(src, src)
            lines.append(f"{letter}. Try {html.escape(display)}")
        elif act == "llm":
            lines.append(f"{letter}. Use LLM estimate")

    return "\n".join(lines)


# Formats one logged food item as an HTML Telegram message.
# One message per item — keeps correction quoting unambiguous (B quotes exactly the item to fix).
# All user/LLM strings are passed through html.escape() — required for Telegram HTML parse_mode.
#
# Source attribution per field is read from macro_meta.field_sources when present;
# falls back to row-level macro_method for older rows or paths that don't set field_sources.
#
# field_sources status → display label:
#   zero_from_label                          → (assumed 0)
#   from_source + source=nutrition_label     → (nutrition label)
#   from_source + source=macro_screenshot    → (meal service label)
#   stated_by_user                           → (you stated)
#   gap_filled / llm_estimated               → (LLM estimate)
#   fallback macro_method=nutrition_label    → (nutrition label)
#   fallback macro_method=restaurant_reported → (meal service label)
#   fallback macro_method=manual             → (you stated)
#   fallback anything else                   → (LLM estimate)
#
# Null secondary macros (fibre/sugar/sodium) show "—" rather than being omitted.
# Null sodium specifically means genuinely unknown; non-null sodium=0 means confirmed zero.
#
# Inputs: meal_type string, item dict with food_item, macro fields, food_meta, macro_meta.
# Outputs: HTML-formatted string ready for Telegram parse_mode="HTML".
def _format_item_reply(meal_type: str, item: dict) -> str:
    meal_label = _MEAL_LABELS.get(meal_type, meal_type.replace("_", " ").title())
    food_name = html.escape(str(item.get("food_item") or "?"))
    lines: list[str] = [f"<b>{meal_label} · {food_name}</b>"]

    # Optional subheader: brand and/or quantity from food_meta.
    food_meta = item.get("food_meta") or {}
    brand = food_meta.get("brand")
    qty = food_meta.get("qty") or {}
    subparts: list[str] = []
    if brand:
        subparts.append(html.escape(str(brand)))
    if qty.get("amount") is not None:
        unit = qty.get("unit") or ""
        qty_str = str(qty["amount"])
        if unit:
            qty_str += " " + unit
        subparts.append(html.escape(qty_str))
    if subparts:
        lines.append(f"<i>{' · '.join(subparts)}</i>")

    lines.append("")

    # Per-field source attribution.
    macro_meta = item.get("macro_meta") or {}
    field_sources: dict = macro_meta.get("field_sources") or {}
    macro_method: str = item.get("macro_method") or "llm"

    def _source_label(field: str) -> str:
        fs = field_sources.get(field)
        if fs:
            status = fs.get("status", "")
            source = fs.get("source", "")
            if status == "zero_from_label":
                return "(assumed 0)"
            if status == "from_source":
                if source == "nutrition_label":
                    return "(nutrition label)"
                if source == "macro_screenshot":
                    return "(meal service label)"
                if source == "usda":
                    scaling_g = fs.get("scaling_g")
                    candidate = fs.get("candidate_name", "")
                    suffix = f" — {candidate}" if candidate else ""
                    return f"(USDA · {scaling_g:.0f}g{suffix})" if scaling_g is not None else f"(USDA{suffix})"
                if source == "open_food_facts":
                    scaling_g = fs.get("scaling_g")
                    candidate = fs.get("candidate_name", "")
                    suffix = f" — {candidate}" if candidate else ""
                    return f"(Open Food Facts · {scaling_g:.0f}g{suffix})" if scaling_g is not None else f"(Open Food Facts{suffix})"
                return "(LLM estimate)"  # food_image and unrecognised sources
            if status == "stated_by_user":
                return "(you stated)"
            if status in ("gap_filled", "llm_estimated"):
                return "(LLM estimate)"
        if macro_method == "nutrition_label":
            return "(nutrition label)"
        if macro_method == "restaurant_reported":
            return "(meal service label)"
        if macro_method == "manual":
            return "(you stated)"
        return "(LLM estimate)"

    def _fmt(val) -> str | None:
        f = _to_float(val)
        return None if f is None else f"{f:.0f}"

    # Core macros.
    kcal = _fmt(item.get("kcal"))
    protein = _fmt(item.get("protein_g"))
    carbs = _fmt(item.get("carbs_g"))
    fat = _fmt(item.get("fat_g"))
    if kcal is not None:
        lines.append(f"{kcal} kcal  <i>{_source_label('kcal')}</i>")
    if protein is not None:
        lines.append(f"{protein}g protein  <i>{_source_label('protein_g')}</i>")
    if carbs is not None:
        lines.append(f"{carbs}g carbs  <i>{_source_label('carbs_g')}</i>")
    if fat is not None:
        lines.append(f"{fat}g fat  <i>{_source_label('fat_g')}</i>")

    lines.append("")

    # Secondary macros — always shown; null renders as "—".
    fibre = _fmt(item.get("fibre_g"))
    sugar = _fmt(item.get("sugar_g"))
    sodium_val = _to_float(item.get("sodium_mg"))
    lines.append(
        f"fibre: {fibre}g  <i>{_source_label('fibre_g')}</i>" if fibre is not None else "fibre: —"
    )
    lines.append(
        f"sugar: {sugar}g  <i>{_source_label('sugar_g')}</i>" if sugar is not None else "sugar: —"
    )
    lines.append(
        f"sodium: {_fmt(sodium_val)}mg  <i>{_source_label('sodium_mg')}</i>"
        if sodium_val is not None else "sodium: —"
    )

    # Candidate list (shown when a structured source match was made).
    candidate_list = _build_candidate_list(macro_meta)
    if candidate_list:
        lines.append("")
        lines.append(candidate_list)

    lines.append("")
    lines.append("<i>Quote to correct.</i>")
    return "\n".join(lines)


# Builds the list of (reply, state) pairs from a completed insert or correction.
# Centralises the repeated pattern shared across all four logging paths and two correction paths.
#
# Parameters:
#   items              — food items (dicts) in order; must align 1-to-1 with food_log_ids.
#   food_log_ids       — DB-assigned IDs for each item.
#   meal_type          — meal type string stored in per-item state.
#   meal_food_log_ids  — all IDs belonging to the meal batch (defaults to food_log_ids when None).
#                        Stored as meal_food_log_ids in context so meal-type corrections can
#                        move the whole batch, not just the single quoted item.
#   correction_history — prior correction texts to carry forward in state (corrections only).
#   parent_reply_id    — telegram_reply_message_id of the message being corrected (corrections only).
#   deleted_count      — number of items just deleted; footnote appended to last surviving item.
def _build_item_results(
    items: list[dict],
    food_log_ids: list[int],
    meal_type: str,
    meal_food_log_ids: list[int] | None = None,
    *,
    correction_history: list[str] | None = None,
    parent_reply_id: int | None = None,
    deleted_count: int = 0,
) -> list[tuple[str, dict | None]]:
    all_meal_ids = meal_food_log_ids if meal_food_log_ids is not None else list(food_log_ids)
    results: list[tuple[str, dict | None]] = []
    for i, (item, fid) in enumerate(zip(items, food_log_ids)):
        reply = _format_item_reply(meal_type, item)
        if deleted_count and i == len(items) - 1:
            reply += f"\n\n<i>({deleted_count} item{'s' if deleted_count > 1 else ''} removed.)</i>"
        context: dict = {
            "food_log_ids": [fid],
            "meal_food_log_ids": all_meal_ids,
            "meal_type": meal_type,
        }
        if correction_history is not None:
            context["correction_history"] = correction_history
        state: dict = {"domain": "food", "context": context}
        if parent_reply_id is not None:
            state["parent_telegram_reply_message_id"] = parent_reply_id
        results.append((reply, state))
    return results


# Dispatches a food logging request to the text or photo path.
# Inputs: InboundMessage with text or photo.
# Outputs: list of (reply, state) — one per food item found.
def handle_food_log(msg: InboundMessage) -> list[tuple[str, dict | None]]:
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
        return [("I didn't catch what you ate — can you describe it in text?", None)]

    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_text(
            _EXTRACT_PROMPT.format(local_time=local_time, text=text),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_extraction_failed", e, update_id=msg.update_id)
        return [("Couldn't parse what you ate — can you rephrase?", None)]

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
        return [("Couldn't identify any food items — can you rephrase?", None)]

    return _handle_text_items(msg, items, extracted)


# Processes extracted text items for both Path 2 (manual/stated macros) and Path 3 (LLM description).
# Items may be mixed — stated_fields is handled per-item so both paths can coexist in one message.
# stated_fields is removed from each item before DB insert (it is not a DB column).
# Inputs: msg, list of extracted items, full extracted dict (for meal_type and top-level provenance).
# Outputs: list of (reply, state) — one per food item.
def _handle_text_items(
    msg: InboundMessage,
    items: list[dict],
    extracted: dict,
) -> list[tuple[str, dict | None]]:
    _MACRO_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")

    # Path 2: stamp stated_fields as authoritative for downstream evidence-first logic (A7).
    # Items with no stated_fields are Path 3 — LLM estimation, no change needed here.
    for item in items:
        stated = item.pop("stated_fields", None) or []
        if stated:
            item["macro_input"] = "manual"
            item["macro_method"] = "manual"
            item.setdefault("macro_meta", {"model": MODEL_FLASH})
            field_sources = item["macro_meta"].setdefault("field_sources", {})
            for field in _MACRO_FIELDS:
                if item.get(field) is not None:
                    if field in stated:
                        field_sources[field] = {"status": "stated_by_user"}
                    else:
                        # Field was estimated by the extraction LLM (not stated by B).
                        # Record provenance so analytics can distinguish stated vs estimated fields.
                        field_sources[field] = {"status": "llm_estimated", "model": MODEL_FLASH}

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

    # Attempt structured source lookup for items without user-stated macros.
    # Items where B stated macro values (macro_input="manual") are left unchanged.
    items = [enrich_item(item, msg.update_id) for item in items]

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
        return [("Logged the intent but failed to save — please try again.", None)]

    log_event(
        logger,
        logging.INFO,
        "food_inserted",
        update_id=msg.update_id,
        item_count=len(food_log_ids),
        meal_type=meal_type,
    )
    return _build_item_results(items, food_log_ids, meal_type)


# Downloads the photo, classifies it, then dispatches to the appropriate path.
# Two LLM calls: _classify_photo (fast, one word), then path-specific extraction.
# Inputs: InboundMessage with file_id set.
# Outputs: list of (reply, state) — one per food item found.
def _handle_photo(msg: InboundMessage) -> list[tuple[str, dict | None]]:
    if not msg.file_id:
        log_event(logger, logging.WARNING, "food_photo_missing_file_id", update_id=msg.update_id)
        return [("Couldn't access the photo — please try again.", None)]

    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        image_bytes = get_file_bytes(msg.file_id, token)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_download_failed", e, update_id=msg.update_id)
        return [("Couldn't download the photo — please try again.", None)]

    log_event(logger, logging.INFO, "food_photo_downloaded",
              update_id=msg.update_id, image_byte_count=len(image_bytes))

    photo_type = _classify_photo(image_bytes, msg.update_id)

    if photo_type == "nutrition_label":
        return _handle_label_photo(msg, image_bytes)
    elif photo_type == "macro_screenshot":
        return _handle_macro_screenshot_photo(msg, image_bytes)
    else:
        return _handle_food_image_photo(msg, image_bytes)


# Classifies a photo as nutrition_label, macro_screenshot, or food_image.
# Fast vision call — model returns one word. Anchored to first token for robustness.
# Falls back to food_image on error or unrecognised response.
# Inputs: image bytes, update_id for logging.
# Outputs: "nutrition_label", "macro_screenshot", or "food_image".
def _classify_photo(image_bytes: bytes, update_id: int | None) -> str:
    _VALID = {"nutrition_label", "macro_screenshot", "food_image"}
    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_CLASSIFY_PROMPT,
            model=MODEL_FLASH,
        ).strip().lower()
        _ALIASES: dict[str, str] = {"restaurant_reported": "macro_screenshot"}
        first_word = raw.split()[0].strip(".,;:!?\"'") if raw.split() else ""
        first_word = _ALIASES.get(first_word, first_word)
        photo_type = first_word if first_word in _VALID else "food_image"
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_photo_classify_failed", e, update_id=update_id)
        photo_type = "food_image"
    log_event(logger, logging.INFO, "food_photo_classified",
              update_id=update_id, photo_type=photo_type)
    return photo_type


# Resolves and validates meal_type from extracted JSON, defaulting to "snack".
# Inputs: extracted dict (must have "meal_type" key), update_id for logging.
# Outputs: validated meal_type string (always a member of _VALID_MEAL_TYPES).
def _resolve_meal_type(extracted: dict, update_id: int | None) -> str:
    meal_type = extracted.get("meal_type", "snack")
    if meal_type not in _VALID_MEAL_TYPES:
        log_event(logger, logging.WARNING, "food_photo_invalid_meal_type",
                  update_id=update_id, meal_type=meal_type)
        meal_type = "snack"
    return meal_type


# Stamps macro_input, macro_method, and macro_meta on each item in a photo batch.
# Items that already have these keys are left unchanged — per-item values take priority.
# primary_input is the expected macro_input for image-sourced items in this path.
# caption-only items (macro_input="description") get a plain macro_meta without file_id.
# Inputs: items list (modified in-place), primary_input string, image_macro_meta dict.
# Outputs: None (modifies items in-place).
def _stamp_photo_provenance(items: list[dict], primary_input: str, image_macro_meta: dict) -> None:
    # Map each macro_input value to its correct macro_method default.
    # Used as fallback when the LLM omits macro_method from an item — prevents label and
    # screenshot items from being silently stored as macro_method="llm".
    _METHOD_FOR_INPUT: dict[str, str] = {
        "nutrition_label": "nutrition_label",
        "macro_screenshot": "restaurant_reported",
        "image": "llm",
        "description": "llm",
    }
    caption_macro_meta: dict = {"model": image_macro_meta.get("model", MODEL_FLASH)}
    for item in items:
        if "macro_input" not in item:
            item["macro_input"] = primary_input
        if "macro_method" not in item:
            item["macro_method"] = _METHOD_FOR_INPUT.get(item["macro_input"], "llm")
        if "macro_meta" not in item:
            base = caption_macro_meta if item["macro_input"] == "description" else image_macro_meta
            item["macro_meta"] = dict(base)


# Path 1 — nutrition label.
# Calls _LABEL_PROMPT, handles status responses, applies backstops and zero-from-label, inserts.
# Inputs: msg, raw image bytes.
# Outputs: list of (reply, state) — one per item found.
def _handle_label_photo(msg: InboundMessage, image_bytes: bytes) -> list[tuple[str, dict | None]]:
    caption = msg.caption or ""
    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_LABEL_PROMPT.format(caption=caption, local_time=local_time),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_label_extraction_failed", e, update_id=msg.update_id)
        return [("Couldn't read the label — can you try again or type the values?", None)]

    status = extracted.get("status")
    if status == "unreadable_label":
        log_event(logger, logging.INFO, "food_photo_label_unreadable", update_id=msg.update_id)
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
        return [("Can't read the label clearly — could you send a clearer photo, or type the values?", pending_state)]
    if status == "needs_quantity":
        log_event(logger, logging.INFO, "food_photo_needs_quantity", update_id=msg.update_id)
        # Save original_caption alongside file_ids so the handler can merge it with the quantity reply.
        # Without this, "protein bar plus banana — 150g" would lose the food context when re-running extraction.
        pending_state: dict = {
            "domain": "food",
            "context": {"awaiting_quantity": True, "file_ids": [msg.file_id]},
        }
        if msg.caption:
            pending_state["context"]["original_caption"] = msg.caption
        return [(
            "I can see the label — how much did you have? (e.g. '150g', '1 serving', 'half a bar')",
            pending_state,
        )]

    items = extracted.get("items", [])
    log_event(logger, logging.INFO, "food_label_extraction_completed",
              update_id=msg.update_id, item_count=len(items))
    if not items:
        log_event(logger, logging.WARNING, "food_label_extraction_empty", update_id=msg.update_id)
        return [("Couldn't read any values from the label — can you try again or type them?", None)]

    meal_type = _resolve_meal_type(extracted, msg.update_id)
    image_macro_meta: dict = {"model": MODEL_FLASH}
    if msg.file_id:
        image_macro_meta["file_id"] = msg.file_id
    _stamp_photo_provenance(items, "nutrition_label", image_macro_meta)

    for item in items:
        if item.get("macro_input") == "nutrition_label":
            ok, reason = _check_label_backstops(item)
            if not ok:
                log_event(logger, logging.WARNING, "food_photo_label_backstop_failed",
                          update_id=msg.update_id, reason=reason,
                          food_item_chars=len(item.get("food_item") or ""))
                return [(reason, None)]

    for item in items:
        if item.get("macro_input") == "nutrition_label":
            _apply_zero_from_label(item)

    try:
        food_log_ids = _insert_items(items=items, meal_type=meal_type, update_id=msg.update_id,
                                     source="telegram", macro_input="nutrition_label",
                                     macro_method="nutrition_label", macro_meta=image_macro_meta)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_insert_failed", e, update_id=msg.update_id)
        return [("Logged the intent but failed to save — please try again.", None)]

    log_event(logger, logging.INFO, "food_photo_inserted", update_id=msg.update_id,
              item_count=len(food_log_ids), meal_type=meal_type, macro_method="nutrition_label")
    return _build_item_results(items, food_log_ids, meal_type)


# Path 2 — macro screenshot (restaurant menu, meal plan, food app, etc.).
# Calls _SCREENSHOT_PROMPT, records source provenance, gap-fills null fields, inserts.
# Inputs: msg, raw image bytes.
# Outputs: list of (reply, state) — one per item found.
def _handle_macro_screenshot_photo(msg: InboundMessage, image_bytes: bytes) -> list[tuple[str, dict | None]]:
    caption = msg.caption or ""
    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_SCREENSHOT_PROMPT.format(caption=caption, local_time=local_time),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_screenshot_extraction_failed", e, update_id=msg.update_id)
        return [("Couldn't read the numbers — can you try again or type them?", None)]

    items = extracted.get("items", [])
    log_event(logger, logging.INFO, "food_screenshot_extraction_completed",
              update_id=msg.update_id, item_count=len(items))
    if not items:
        log_event(logger, logging.WARNING, "food_screenshot_extraction_empty", update_id=msg.update_id)
        return [("Couldn't read any items — can you describe them in text?", None)]

    meal_type = _resolve_meal_type(extracted, msg.update_id)
    image_macro_meta: dict = {"model": MODEL_FLASH}
    if msg.file_id:
        image_macro_meta["file_id"] = msg.file_id
    _stamp_photo_provenance(items, "macro_screenshot", image_macro_meta)

    _MACRO_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")
    for item in items:
        if item.get("macro_input") == "macro_screenshot":
            field_sources = item["macro_meta"].setdefault("field_sources", {})
            for field in _MACRO_FIELDS:
                if item.get(field) is not None:
                    field_sources[field] = {"status": "from_source", "source": "macro_screenshot"}
            _gap_fill_macros(item, msg.update_id)

    try:
        food_log_ids = _insert_items(items=items, meal_type=meal_type, update_id=msg.update_id,
                                     source="telegram", macro_input="macro_screenshot",
                                     macro_method="restaurant_reported", macro_meta=image_macro_meta)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_insert_failed", e, update_id=msg.update_id)
        return [("Logged the intent but failed to save — please try again.", None)]

    log_event(logger, logging.INFO, "food_photo_inserted", update_id=msg.update_id,
              item_count=len(food_log_ids), meal_type=meal_type, macro_method="restaurant_reported")
    return _build_item_results(items, food_log_ids, meal_type)


# Path 3 — food image (actual food photo, no printed macro numbers).
# Calls _FOOD_IMAGE_PROMPT for LLM macro estimation, inserts.
# USDA/OFF structured source routing will be added here in A4.
# Inputs: msg, raw image bytes.
# Outputs: list of (reply, state) — one per item found.
def _handle_food_image_photo(msg: InboundMessage, image_bytes: bytes) -> list[tuple[str, dict | None]]:
    caption = msg.caption or ""
    local_time = _local_time_str(msg.timestamp)

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_FOOD_IMAGE_PROMPT.format(caption=caption, local_time=local_time),
            model=MODEL_FLASH,
        )
        extracted = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_image_extraction_failed", e, update_id=msg.update_id)
        return [("Couldn't read the photo — can you try again or describe it in text?", None)]

    items = extracted.get("items", [])
    log_event(logger, logging.INFO, "food_image_extraction_completed",
              update_id=msg.update_id, item_count=len(items))
    if not items:
        log_event(logger, logging.WARNING, "food_image_extraction_empty", update_id=msg.update_id)
        return [("Couldn't identify any food in the photo — can you describe it in text?", None)]

    meal_type = _resolve_meal_type(extracted, msg.update_id)
    image_macro_meta: dict = {"model": MODEL_FLASH}

    # Attempt structured source lookup for food_image items.
    # _stamp_photo_provenance runs after so it only fills provenance for items that
    # didn't get a structured source match (macro_input stays "image" for those).
    items = [enrich_item(item, msg.update_id) for item in items]

    _stamp_photo_provenance(items, "image", image_macro_meta)

    try:
        food_log_ids = _insert_items(items=items, meal_type=meal_type, update_id=msg.update_id,
                                     source="telegram", macro_input="image",
                                     macro_method="llm", macro_meta=image_macro_meta)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_insert_failed", e, update_id=msg.update_id)
        return [("Logged the intent but failed to save — please try again.", None)]

    log_event(logger, logging.INFO, "food_photo_inserted", update_id=msg.update_id,
              item_count=len(food_log_ids), meal_type=meal_type, macro_method="llm")
    return _build_item_results(items, food_log_ids, meal_type)


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


# Fills null macro fields for a macro_screenshot item using a constrained LLM call.
# Known non-null fields (read from the printed source) are passed to the LLM as fixed
# constraints; only null fields are estimated. Never overwrites non-null values.
# Text items with stated_fields do not call this — the extraction LLM already estimated
# all fields in the same pass, so a second call would be redundant.
# Records each gap-filled field in item["macro_meta"]["field_sources"] for provenance.
# Inputs: item dict (modified in-place), update_id for logging.
# Outputs: None (modifies item in-place).
def _gap_fill_macros(item: dict, update_id: int | None) -> None:
    _ALL_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")

    known = {f: _to_float(item.get(f)) for f in _ALL_FIELDS if item.get(f) is not None}
    missing = [f for f in _ALL_FIELDS if item.get(f) is None]

    if not missing:
        return  # nothing to fill
    if not known:
        # No anchors — cannot do constrained estimation. Leave nulls as-is.
        log_event(logger, logging.WARNING, "food_gap_fill_no_anchors", update_id=update_id)
        return

    known_lines = "\n".join(f"  {f}: {v}" for f, v in known.items())
    missing_list = ", ".join(missing)

    try:
        raw = generate_text(
            _GAP_FILL_PROMPT.format(
                food_item=item.get("food_item", "unknown food"),
                known_lines=known_lines,
                missing_list=missing_list,
            ),
            model=MODEL_FLASH,
        )
        filled = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_gap_fill_failed", e, update_id=update_id)
        return

    # Apply only the fields that were requested and returned as valid numbers.
    # Never overwrite non-null values — the known fields are authoritative.
    meta = item.setdefault("macro_meta", {"model": MODEL_FLASH})
    field_sources = meta.setdefault("field_sources", {})
    filled_count = 0
    for field in missing:
        val = _to_float(filled.get(field))
        if val is not None and item.get(field) is None:
            item[field] = val
            field_sources[field] = {"status": "gap_filled", "model": MODEL_FLASH}
            filled_count += 1

    log_event(
        logger,
        logging.INFO,
        "food_gap_fill_completed",
        update_id=update_id,
        known_count=len(known),
        requested_count=len(missing),
        filled_count=filled_count,
    )


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


