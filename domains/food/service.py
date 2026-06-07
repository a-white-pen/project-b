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
  get_timezone(as_of)                       — imported from system.timezone; resolves B's tz as-of a timestamp
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

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text, generate_with_image
from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from system.timezone import get_timezone
from telegram.files import get_file_bytes
from domains.food.nutrition_sources.router import enrich_item

logger = logging.getLogger(__name__)

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
  "macro_input": "<one of: description, nutrition_label, macro_screenshot, image, manual>",
  "macro_method": "<one of: llm, nutrition_label, restaurant_reported, usda, open_foods, manual>",
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
- qty: when the user states an explicit gram/weight alongside a container or count, always use the gram weight as qty. Examples: "1 box 80g" → {{"amount": 80, "unit": "g"}}; "1 bar 45g" → {{"amount": 45, "unit": "g"}}; "2 eggs 55g each" → {{"amount": 110, "unit": "g"}} (multiply count × per-item weight). Gram weight always beats container unit (box, bag, bar, packet, slice, piece, scoop, serving).
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
# Layout:
#   Header:    <b>Meal · food_name</b>
#   Subheader: candidate_name · scaling_g  (or brand · qty from food_meta when no structured source)
#   Grid:      field  value unit  <i>chip</i>  — one row per macro; field name first
#   Footer:    <i>Source · candidate_name</i>  — once per card, only for USDA / OFF matches
#   Extras:    expandable alternatives block, "Quote to correct."
#
# Short source chips (per field):
#   from_source + usda              → USDA
#   from_source + open_food_facts   → OFF
#   from_source + nutrition_label   → nutrition label
#   from_source + macro_screenshot  → restaurant reported
#   zero_from_label                 → -0
#   stated_by_user                  → B reported
#   gap_filled / llm_estimated      → llm est.
#   fallback macro_method=manual    → B reported
#   fallback anything else          → llm est.
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

    macro_meta = item.get("macro_meta") or {}
    field_sources: dict = macro_meta.get("field_sources") or {}
    macro_method: str = item.get("macro_method") or "llm"
    food_meta = item.get("food_meta") or {}

    # Subheader: candidate_name + scaling_g when a structured source was used;
    # falls back to brand + qty from food_meta (LLM / label / screenshot paths).
    structured_source = macro_meta.get("structured_source")
    candidate_name_meta: str = macro_meta.get("candidate_name") or ""

    # Scaling_g: scan field_sources first, then resolved_grams (count/natural units).
    scaling_g: float | None = None
    for _fs in field_sources.values():
        if _fs.get("scaling_g") is not None:
            scaling_g = float(_fs["scaling_g"])
            break
    if scaling_g is None and macro_meta.get("resolved_grams") is not None:
        scaling_g = float(macro_meta["resolved_grams"])

    _STRUCTURED_SOURCES = {"usda", "open_food_facts"}

    if structured_source in _STRUCTURED_SOURCES and candidate_name_meta:
        raw_cn = candidate_name_meta
        truncated_cn = (raw_cn[:30] + "…") if len(raw_cn) > 30 else raw_cn
        subheader = html.escape(truncated_cn)
        if scaling_g is not None:
            subheader += f" · {scaling_g:.0f}g"
        lines.append(f"<i>{subheader}</i>")
    else:
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

    # Short source chip per field — full attribution goes in the footer line, not repeated per field.
    def _chip(field: str) -> str:
        fs = field_sources.get(field)
        if fs:
            status = fs.get("status", "")
            source = fs.get("source", "")
            if status == "zero_from_label":
                return "-0"
            if status == "from_source":
                if source == "nutrition_label":
                    return "nutrition label"
                if source == "macro_screenshot":
                    return "restaurant reported"
                if source == "usda":
                    return "USDA"
                if source == "open_food_facts":
                    return "OFF"
                return "llm est."
            if status == "stated_by_user":
                return "B reported"
            if status in ("gap_filled", "llm_estimated"):
                return "llm est."
        # Fallback to row-level macro_method for older rows or paths without field_sources.
        if macro_method == "nutrition_label":
            return "nutrition label"
        if macro_method == "restaurant_reported":
            return "restaurant reported"
        if macro_method == "manual":
            return "B reported"
        return "llm est."

    def _fmt(val) -> str | None:
        f = _to_float(val)
        return None if f is None else f"{f:.0f}"

    # Core macros — field name first, then value + unit, then short source chip.
    kcal = _fmt(item.get("kcal"))
    protein = _fmt(item.get("protein_g"))
    carbs = _fmt(item.get("carbs_g"))
    fat = _fmt(item.get("fat_g"))
    if kcal is not None:
        lines.append(f"kcal  {kcal}  <i>{_chip('kcal')}</i>")
    if protein is not None:
        lines.append(f"protein  {protein} g  <i>{_chip('protein_g')}</i>")
    if carbs is not None:
        lines.append(f"carbs  {carbs} g  <i>{_chip('carbs_g')}</i>")
    if fat is not None:
        lines.append(f"fat  {fat} g  <i>{_chip('fat_g')}</i>")

    lines.append("")

    # Secondary macros — always shown; null renders as "—" so the field is visible.
    fibre = _fmt(item.get("fibre_g"))
    sugar = _fmt(item.get("sugar_g"))
    sodium_val = _to_float(item.get("sodium_mg"))
    lines.append(f"fibre  {fibre} g  <i>{_chip('fibre_g')}</i>" if fibre is not None else "fibre  —")
    lines.append(f"sugar  {sugar} g  <i>{_chip('sugar_g')}</i>" if sugar is not None else "sugar  —")
    lines.append(
        f"sodium  {_fmt(sodium_val)} mg  <i>{_chip('sodium_mg')}</i>"
        if sodium_val is not None else "sodium  —"
    )

    # Footer: structured source attribution once per card — only for USDA / OFF matches.
    # Mixed sources (some fields gap-filled by LLM) still show the primary source here;
    # per-field chips already surface "llm est." for the gap-filled ones.
    _SOURCE_DISPLAY = {"usda": "USDA", "open_food_facts": "Open Food Facts"}
    footer_source = structured_source if structured_source in _SOURCE_DISPLAY else None
    if not footer_source:
        # Scan field_sources in case macro_meta.structured_source wasn't set (e.g. post-correction).
        for _fs in field_sources.values():
            if _fs.get("status") == "from_source" and _fs.get("source") in _SOURCE_DISPLAY:
                footer_source = _fs["source"]
                if not candidate_name_meta:
                    candidate_name_meta = _fs.get("candidate_name") or ""
                break

    if footer_source:
        source_display = _SOURCE_DISPLAY[footer_source]
        footer_candidate = macro_meta.get("candidate_name") or candidate_name_meta
        if footer_candidate:
            raw_fc = footer_candidate
            truncated_fc = (raw_fc[:40] + "…") if len(raw_fc) > 40 else raw_fc
            footer_line = f"{source_display} · {html.escape(truncated_fc)}"
        else:
            footer_line = source_display
        lines.append("")
        lines.append(f"<i>{footer_line}</i>")

    # Candidate list (shown when a structured source match was made).
    # Wrapped in <blockquote expandable> so it collapses by default in Telegram.
    candidate_list = _build_candidate_list(macro_meta)
    if candidate_list:
        lines.append("")
        lines.append(f"<blockquote expandable>{candidate_list}</blockquote>")

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

    # Gap 4: when B stated kcal but not the full macro breakdown, null out LLM-estimated
    # P/C/F/fibre/sugar/sodium so gap-fill can re-estimate them anchored to the stated kcal.
    # Without this the reply would show LLM-estimated P/C/F alongside a stated kcal that
    # doesn't add up to the estimated breakdown.
    _DERIVED_FIELDS = ("protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")
    for item in items:
        field_sources = (item.get("macro_meta") or {}).get("field_sources") or {}
        stated_set = {f for f, fs in field_sources.items() if fs.get("status") == "stated_by_user"}
        if "kcal" in stated_set and not stated_set.issuperset({"protein_g", "carbs_g", "fat_g"}):
            for field in _DERIVED_FIELDS:
                if field not in stated_set:
                    item[field] = None
                    field_sources.pop(field, None)
            _gap_fill_macros(item, msg.update_id)

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
        # Reuse bytes the router already fetched for bare-photo intent classification.
        image_bytes = msg.file_bytes
        if image_bytes is None:
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

    # Caption-only items (label unreadable but caption names the food) get description/llm treatment.
    # Run enrich_item so they get the same USDA/OFF structured source lookup as text items.
    items = [enrich_item(item, msg.update_id) if item.get("macro_input") == "description" else item for item in items]

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

    # Caption-only items (screenshot unreadable but caption names the food) get description/llm treatment.
    # Run enrich_item so they get the same USDA/OFF structured source lookup as text items.
    items = [enrich_item(item, msg.update_id) if item.get("macro_input") == "description" else item for item in items]

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
# Calls _FOOD_IMAGE_PROMPT for LLM macro estimation, then runs enrich_item for structured source lookup.
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
    # enrich_item may set macro_method/macro_meta on matched items but never touches macro_input
    # (it stays "image" throughout). _stamp_photo_provenance fills any missing macro_input/method
    # keys on the remaining items — it is a no-op for fields already set.
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
# Returns B's local time at the given event timestamp as a readable string for the LLM prompt.
# Resolves timezone as-of the event so meal_type inference is correct for delayed messages.
def _local_time_str(as_of: datetime | None = None) -> str:
    tz = get_timezone(as_of)
    # Use the event's own timestamp if available; otherwise use current time in that zone.
    if as_of is not None:
        local_now = as_of.astimezone(tz)
    else:
        local_now = datetime.now(tz=tz)
    return local_now.strftime("%H:%M on %A")  # e.g. "08:30 on Monday"


# Strips markdown code fences and parses the FIRST valid JSON value from the LLM
# response, ignoring any trailing content. The LLM occasionally returns two
# top-level JSON values back-to-back (e.g. `{...}\n{...}` or `null\nnull`) — strict
# json.loads would reject the whole response with "Extra data: line 2 column 1".
# raw_decode tolerates this by consuming the first value and leaving the rest.
# Caller still sees clean dict output for the common single-value case.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    obj, _end = json.JSONDecoder().raw_decode(cleaned)
    if not isinstance(obj, dict):
        # Some LLM mishaps emit `null` or a bare list as the first value. Treat
        # those as parse failures so the caller's existing error path runs.
        raise ValueError(f"expected JSON object, got {type(obj).__name__}")
    return obj


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


# Fills null macro fields using a constrained LLM call anchored to known values.
# Used in three contexts: (1) macro_screenshot items where some fields were read from the label,
# (2) text items where B stated kcal but not full P/C/F, (3) after a candidate switch in correction
# to fill fields the new candidate lacks.
# Known non-null fields are passed to the LLM as fixed constraints; only null fields are estimated.
# Never overwrites non-null values.
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
    prompt = _GAP_FILL_PROMPT.format(
        food_item=item.get("food_item", "unknown food"),
        known_lines=known_lines,
        missing_list=missing_list,
    )

    # Try once; on parse/LLM failure retry once before giving up. The gap-fill
    # LLM occasionally emits malformed JSON for short responses (two top-level
    # values, bare null, etc.); a fresh call almost always succeeds. _parse_json
    # is already tolerant of trailing values, so this retry only fires on the
    # rarer cases that survive raw_decode.
    filled = None
    last_error: Exception | None = None
    for attempt in (1, 2):
        try:
            raw = generate_text(prompt, model=MODEL_FLASH)
            filled = _parse_json(raw)
            break
        except Exception as e:
            last_error = e
            log_event(
                logger,
                logging.INFO,
                "food_gap_fill_attempt_failed",
                update_id=update_id,
                attempt=attempt,
                error_type=type(e).__name__,
            )
    if filled is None:
        log_failure(logger, logging.WARNING, "food_gap_fill_failed", last_error, update_id=update_id)
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


