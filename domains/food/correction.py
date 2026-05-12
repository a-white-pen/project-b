"""
Food correction handler — applies B's quoted correction to previously logged food_log rows.

Functions:
  handle_food_correction(msg, state)           — parse correction, update food_log rows, return (reply, state)
  _handle_photo_correction(msg, current_items) — re-reads macros from a correction photo via vision model, applies changes
"""

import json
import logging
import os
import re

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text, generate_with_image
from system.logging import log_event, log_failure
from system.messages import InboundMessage, MessageType
from telegram.files import get_file_bytes

logger = logging.getLogger(__name__)

_VALID_MEAL_TYPES = {
    "breakfast", "brunch", "lunch", "snack",
    "dinner", "supper", "pre_workout", "post_workout",
}

_REESTIMATE_PROMPT = """\
You are re-estimating a food log entry after a user correction.

About the user: Singaporean Chinese, based between Singapore and Bangkok. \
Eats Singaporean, Thai, and Southern Chinese food on top of regular western fare. \
Common meals include hawker dishes (chicken rice, char kway teow, laksa, wonton noodles), \
Thai dishes (pad kra pao, moo ping, khao soi, boat noodles), and Southern Chinese food \
(dim sum, congee, braised meats). Use this context when estimating portion sizes and macros.

Previously logged: {previously_logged}
User correction: {correction_text}

The user is correcting what was logged. Use both the original entry and their correction to \
produce an accurate estimate. The user may be clarifying the item name, the quantity, or \
providing extra context to fix a macro estimation error.

Return a JSON object:
{{
  "food_item": "<corrected description>",
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

Rules:
- food_meta keys are optional — omit if not meaningful for this item
- If the user explicitly stated macro values in their correction, use those exactly
- Otherwise estimate macros from all available context
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

_CORRECTION_PROMPT = """\
You are correcting food log entries for a personal nutrition tracker.

Previously logged items:
{logged_items}

Correction from user: {correction_text}

Determine what the user wants to change. They may want to:
- Change the meal type (e.g. "that was dinner not lunch")
- Update macros for one or more items (e.g. "actually the chicken rice was 600 kcal")
- Delete one or more items (e.g. "remove the yoghurt", "delete that")
- Update the food description or quantity

Return a JSON object with this exact structure:
{{
  "meal_type": "<meal type — use original if unchanged>",
  "items": [
    {{
      "food_log_id": <int — the id of the existing row to update or delete>,
      "action": "<update or delete>",
      "food_item": "<description — ONLY include if the user explicitly changed the name>",
      "kcal": <number — ONLY include if the user explicitly stated a new value>,
      "protein_g": <number — ONLY include if explicitly changed>,
      "carbs_g": <number — ONLY include if explicitly changed>,
      "fat_g": <number — ONLY include if explicitly changed>,
      "fibre_g": <number — ONLY include if explicitly changed>,
      "sugar_g": <number — ONLY include if explicitly changed>,
      "sodium_mg": <number — ONLY include if explicitly changed>
    }}
  ]
}}

Rules:
- Only include items that need to change (action=update) or be removed (action=delete)
- For items not mentioned by the user, omit them from the list entirely
- For fields not explicitly changed, OMIT the key entirely — do NOT include null
- meal_type: only change if the user explicitly mentions it
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

_PHOTO_CORRECTION_PROMPT = """\
You are correcting previously logged food entries using a new photo provided by the user.

Previously logged items:
{logged_items}

Caption from user (may be empty): {caption}

The user has sent a photo to correct the logged entries. The photo may be:
- A nutrition label: read macros directly from the label. Pro-rate by quantity if the caption or \
previously logged food_meta specifies how much was consumed. If no quantity is known, use 1 serving.
- A food image: use the image and caption together to estimate corrected macros.

For each previously logged item that can be corrected from this photo, return an update.
If the photo clearly shows the correct values for an item, update it.
If the photo is unrelated to a logged item, omit that item.

Return a JSON object with this exact structure:
{{
  "photo_type": "<nutrition_label or food_image>",
  "meal_type": "<meal type — use original if unchanged>",
  "items": [
    {{
      "food_log_id": <int>,
      "action": "update",
      "food_item": "<corrected description — only if changed>",
      "kcal": <number — only if changed>,
      "protein_g": <number — only if changed>,
      "carbs_g": <number — only if changed>,
      "fat_g": <number — only if changed>,
      "fibre_g": <number — only if changed>,
      "sugar_g": <number — only if changed>,
      "sodium_mg": <number — only if changed>
    }}
  ]
}}

Rules:
- photo_type: "nutrition_label" if the image shows a nutrition facts panel or packaging label; "food_image" otherwise
- For fields not changed, OMIT the key entirely — do NOT include null
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Handles a correction to a previously logged food entry.
# B quotes a bot reply → router looks up conversation_state → calls this function.
# Inputs:
#   msg   — the inbound correction message (msg.text is the correction text)
#   state — conversation_state row dict from load_state() (includes context.food_log_ids)
# Outputs: (reply_text, pending_state) where pending_state has the new state for saving.
def handle_food_correction(msg: InboundMessage, state: dict) -> tuple[str, dict | None]:
    context = state.get("context") or {}
    food_log_ids = context.get("food_log_ids") or []
    original_meal_type = context.get("meal_type", "snack")
    log_event(
        logger,
        logging.INFO,
        "food_correction_started",
        update_id=msg.update_id,
        food_log_id_count=len(food_log_ids),
        has_text=bool(msg.text),
        has_caption=bool(msg.caption),
    )

    if not food_log_ids:
        log_event(logger, logging.WARNING, "food_correction_missing_food_log_ids", update_id=msg.update_id)
        return ("Nothing to correct — couldn't find the original log entries.", None)

    # Validate that the message carries actionable content before touching the DB.
    # Photo path needs only file_id (no text required); text path needs text or caption.
    # Both checks happen here so a blank quoted reply always gets the prompt, never a DB error.
    is_photo = msg.message_type == MessageType.PHOTO and msg.file_id
    correction_text = msg.text or msg.caption
    if not is_photo and not correction_text:
        log_event(logger, logging.WARNING, "food_correction_missing_text", update_id=msg.update_id)
        return ("What would you like to change? Send me a text description of the correction.", None)

    # Fetch current state of the logged items from the DB
    try:
        current_items = _fetch_items(food_log_ids)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_fetch_failed", e, update_id=msg.update_id)
        return ("Couldn't load the original log — please try again.", None)

    if not current_items:
        log_event(logger, logging.WARNING, "food_correction_original_items_missing", update_id=msg.update_id)
        return ("Nothing to correct — the original items may have been deleted already.", None)

    # Route to photo correction path if B attached a photo to the correction message.
    if is_photo:
        return _handle_photo_correction(msg, current_items, original_meal_type, state)

    # Format items for LLM context
    logged_items_text = _format_items_for_llm(current_items)

    # Ask LLM to parse the correction
    try:
        raw = generate_text(
            _CORRECTION_PROMPT.format(
                logged_items=logged_items_text,
                correction_text=correction_text,
            ),
            model=MODEL_FLASH,
        )
        parsed = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_parse_failed", e, update_id=msg.update_id)
        return ("Couldn't understand the correction — can you rephrase?", None)

    new_meal_type = parsed.get("meal_type", original_meal_type)
    if new_meal_type not in _VALID_MEAL_TYPES:
        new_meal_type = original_meal_type

    correction_items = parsed.get("items", [])
    log_event(
        logger,
        logging.INFO,
        "food_correction_parsed",
        update_id=msg.update_id,
        item_count=len(correction_items),
        meal_type=new_meal_type,
    )
    if not correction_items and new_meal_type == original_meal_type:
        log_event(logger, logging.WARNING, "food_correction_no_changes", update_id=msg.update_id)
        return ("Got your message — nothing seemed to need changing. What did you want to correct?", None)

    # Enrich correction items with re-estimated macros and accurate provenance.
    #
    # Two independent concerns handled here:
    #
    # 1. Re-estimation: when food_item changes, call the LLM with the original entry + correction
    #    text so macros and food_meta reflect the new item, not the old one.
    #
    # 2. Provenance: macro_method/macro_input must reflect what actually happened.
    #    - User explicitly stated values → "manual" (applies whether food_item changed or not)
    #    - Food item renamed, macros all from re-estimation → "llm"
    #    - No food_item change, no explicit macros → don't touch provenance columns at all
    #
    # user_stated is computed BEFORE re-estimation so it captures only what the correction LLM
    # returned (i.e. what B explicitly said), not values filled in by re-estimation.
    _MACRO_COLS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")

    for item in correction_items:
        if item.get("action") == "delete":
            continue

        # Snapshot explicit macro values before any re-estimation merge.
        user_stated = [col for col in _MACRO_COLS if col in item]

        if "food_item" in item:
            # Food item name changed — re-estimate macros and food_meta for the new item.
            fid = item.get("food_log_id")
            original = next((r for r in current_items if r["food_log_id"] == fid), None)
            if original is not None:
                reestimated = _reestimate_item(original, correction_text, msg.update_id)
                if reestimated is None:
                    log_event(
                        logger,
                        logging.WARNING,
                        "food_correction_reestimate_skipped",
                        update_id=msg.update_id,
                        food_log_id=fid,
                    )
                else:
                    # food_item: use re-estimation (has full context of original + correction text)
                    item["food_item"] = reestimated.get("food_item") or item["food_item"]
                    # macros: re-estimation as base; user-stated values (snapshotted above) take priority
                    for col in _MACRO_COLS:
                        if col not in item:
                            item[col] = reestimated.get(col)
                    # food_meta: only write if re-estimation returned something non-empty —
                    # never wipe existing metadata just because the model omitted those keys.
                    reestimated_food_meta = reestimated.get("food_meta") or {}
                    if reestimated_food_meta:
                        item["_food_meta"] = reestimated_food_meta
                    item["_macro_meta"] = {
                        "model": MODEL_FLASH,
                        "corrected_from": original["food_item"],
                        "correction_update_id": msg.update_id,
                        **({"user_stated_fields": user_stated} if user_stated else {}),
                    }

        # Set macro provenance. Applies whether or not food_item changed.
        # "manual" when B explicitly stated any value; "llm" when all macros came from re-estimation.
        # If neither (e.g. only meal_type changed), leave provenance columns untouched.
        if user_stated:
            item["_macro_input"] = "manual"
            item["_macro_method"] = "manual"
            if "_macro_meta" not in item:
                # No re-estimation (food_item didn't change) — build meta from scratch.
                item["_macro_meta"] = {
                    "user_stated_fields": user_stated,
                    "correction_update_id": msg.update_id,
                }
            else:
                # Re-estimation ran — user_stated already in macro_meta; update method fields.
                item["_macro_input"] = "manual"
                item["_macro_method"] = "manual"
        elif "_macro_meta" in item:
            # food_item changed, re-estimation ran, no explicit macros — all LLM.
            item["_macro_input"] = "description"
            item["_macro_method"] = "llm"

    # Apply changes to DB
    try:
        surviving_ids = _apply_corrections(
            correction_items=correction_items,
            new_meal_type=new_meal_type,
            original_meal_type=original_meal_type,
            all_ids=food_log_ids,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_apply_failed", e, update_id=msg.update_id)
        return ("Correction parsed but failed to save — please try again.", None)

    # Build updated item list for the reply.
    # _apply_corrections already committed — guard this re-fetch so a DB hiccup here
    # does not silence the reply entirely after a successful write.
    try:
        updated_items = _fetch_items(surviving_ids) if surviving_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_correction_refetch_failed", e, update_id=msg.update_id)
        updated_items = []
    log_event(
        logger,
        logging.INFO,
        "food_correction_completed",
        update_id=msg.update_id,
        surviving_item_count=len(surviving_ids),
        meal_type=new_meal_type,
    )
    reply = _format_correction_reply(new_meal_type, updated_items, correction_items)

    new_state = {
        "domain": "food",
        "context": {"food_log_ids": surviving_ids, "meal_type": new_meal_type},
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (reply, new_state)


# Handles a photo-based correction — re-reads macros from an image B attached to the correction.
# Called when the correction message has a photo (e.g. a clearer label photo or food image).
# Downloads the image, asks the vision model to correct the logged items, then applies changes.
# Inputs: msg with file_id set, current DB items, original meal type, full state dict.
# Outputs: (reply_text, pending_state) — same contract as handle_food_correction.
def _handle_photo_correction(
    msg: InboundMessage,
    current_items: list[dict],
    original_meal_type: str,
    state: dict,
) -> tuple[str, dict | None]:
    food_log_ids = [item["food_log_id"] for item in current_items]
    log_event(logger, logging.INFO, "food_photo_correction_started", update_id=msg.update_id)

    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        image_bytes = get_file_bytes(msg.file_id, token)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_download_failed", e, update_id=msg.update_id)
        return ("Couldn't download the photo — please try again.", None)

    logged_items_text = _format_items_for_llm(current_items)
    caption = msg.caption or msg.text or ""

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_PHOTO_CORRECTION_PROMPT.format(
                logged_items=logged_items_text,
                caption=caption,
            ),
            model=MODEL_FLASH,
        )
        parsed = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_parse_failed", e, update_id=msg.update_id)
        return ("Couldn't read the photo — can you try again or type the correction instead?", None)

    new_meal_type = parsed.get("meal_type", original_meal_type)
    if new_meal_type not in _VALID_MEAL_TYPES:
        new_meal_type = original_meal_type

    correction_items = parsed.get("items", [])
    log_event(
        logger,
        logging.INFO,
        "food_photo_correction_parsed",
        update_id=msg.update_id,
        item_count=len(correction_items),
    )
    if not correction_items and new_meal_type == original_meal_type:
        return ("Looked at the photo but couldn't see what needed changing — can you describe it in text?", None)

    # Mark all photo-corrected macros as coming from a label/image re-read.
    # photo_type is returned by the vision model and is the authoritative signal —
    # do NOT infer from caption presence (a label photo can arrive with or without a caption).
    # macro_meta shape follows the schema contract: nutrition_label rows must include file_id.
    photo_type = parsed.get("photo_type", "food_image")
    is_label = photo_type == "nutrition_label"
    correction_meta: dict = {
        "model": MODEL_FLASH,
        "correction_source": "photo",
        "photo_type": photo_type,
        "correction_update_id": msg.update_id,
    }
    if is_label and msg.file_id:
        correction_meta["file_id"] = msg.file_id
    for item in correction_items:
        if item.get("action") != "delete":
            item["_macro_input"] = "nutrition_label" if is_label else "image"
            item["_macro_method"] = "nutrition_label" if is_label else "llm"
            item["_macro_meta"] = correction_meta

    try:
        surviving_ids = _apply_corrections(
            correction_items=correction_items,
            new_meal_type=new_meal_type,
            original_meal_type=original_meal_type,
            all_ids=food_log_ids,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_apply_failed", e, update_id=msg.update_id)
        return ("Photo read but failed to save the correction — please try again.", None)

    try:
        updated_items = _fetch_items(surviving_ids) if surviving_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_photo_correction_refetch_failed", e, update_id=msg.update_id)
        updated_items = []

    log_event(logger, logging.INFO, "food_photo_correction_completed", update_id=msg.update_id, surviving_item_count=len(surviving_ids))
    reply = _format_correction_reply(new_meal_type, updated_items, correction_items)
    new_state = {
        "domain": "food",
        "context": {"food_log_ids": surviving_ids, "meal_type": new_meal_type},
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (reply, new_state)


# Re-estimates a single food item using the original logged entry plus the user's correction text.
# Called when food_item changes in a correction — ensures macros, food_meta, and metadata columns
# are all refreshed for the new item rather than inheriting stale values from the original.
# Returns a dict with food_item, macros, food_meta on success, or None on failure (caller falls back
# to rename-only behaviour so the correction is never blocked by a re-estimation error).
def _reestimate_item(original: dict, correction_text: str, update_id: int | None) -> dict | None:
    parts = [original.get("food_item", "?")]
    macros = []
    if original.get("kcal") is not None:
        macros.append(f"{original['kcal']:.0f} kcal")
    if original.get("protein_g") is not None:
        macros.append(f"{original['protein_g']:.0f}g protein")
    if original.get("fat_g") is not None:
        macros.append(f"{original['fat_g']:.0f}g fat")
    if original.get("carbs_g") is not None:
        macros.append(f"{original['carbs_g']:.0f}g carbs")
    if macros:
        parts.append(f"({', '.join(macros)})")
    previously_logged = " ".join(parts)

    try:
        raw = generate_text(
            _REESTIMATE_PROMPT.format(
                previously_logged=previously_logged,
                correction_text=correction_text,
            ),
            model=MODEL_FLASH,
        )
        result = _parse_json(raw)
        log_event(
            logger,
            logging.INFO,
            "food_correction_reestimate_completed",
            update_id=update_id,
            original_food_chars=len(original.get("food_item") or ""),
            new_food_chars=len(result.get("food_item") or ""),
        )
        return result
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_reestimate_failed", e, update_id=update_id)
        return None


# Fetches food_log rows for the given IDs. Returns list of row dicts.
def _fetch_items(food_log_ids: list[int]) -> list[dict]:
    if not food_log_ids:
        return []
    sql = """
        SELECT food_log_id, food_item, meal_type,
               kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg,
               food_meta
        FROM nutrition.food_log
        WHERE food_log_id = ANY(%s)
        ORDER BY food_log_id
    """
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, (food_log_ids,))
                rows = cur.fetchall()
                log_event(
                    logger,
                    logging.INFO,
                    "food_correction_items_loaded",
                    food_log_id_count=len(food_log_ids),
                    row_count=len(rows),
                )
                return [
                    {
                        "food_log_id": r[0],
                        "food_item": r[1],
                        "meal_type": r[2],
                        "kcal": r[3],
                        "protein_g": r[4],
                        "carbs_g": r[5],
                        "fat_g": r[6],
                        "fibre_g": r[7],
                        "sugar_g": r[8],
                        "sodium_mg": r[9],
                        "food_meta": r[10] or {},
                    }
                    for r in rows
                ]
    finally:
        conn.close()


# Formats current food_log rows as readable text for the correction LLM prompt.
# Includes logged quantity from food_meta so the model can pro-rate label macros correctly
# without needing B to repeat the quantity in the correction message.
def _format_items_for_llm(items: list[dict]) -> str:
    lines = []
    for item in items:
        parts = [f"[id={item['food_log_id']}] {item['food_item']}"]
        macros = []
        if item["kcal"] is not None:
            macros.append(f"{item['kcal']:.0f} kcal")
        if item["protein_g"] is not None:
            macros.append(f"{item['protein_g']:.0f}g protein")
        if item["fat_g"] is not None:
            macros.append(f"{item['fat_g']:.0f}g fat")
        if item["carbs_g"] is not None:
            macros.append(f"{item['carbs_g']:.0f}g carbs")
        if macros:
            parts.append("(" + ", ".join(macros) + ")")
        # Surface the originally logged quantity so the LLM can pro-rate label macros.
        qty = (item.get("food_meta") or {}).get("qty")
        if qty and qty.get("amount") is not None:
            unit = qty.get("unit", "")
            parts.append(f"[logged qty: {qty['amount']}{' ' + unit if unit else ''}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


# Applies parsed corrections: updates or deletes rows. Returns surviving food_log_ids.
def _apply_corrections(
    correction_items: list[dict],
    new_meal_type: str,
    original_meal_type: str,
    all_ids: list[int],
) -> list[int]:
    deleted_ids: set[int] = set()
    updated_ids: set[int] = set()

    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                # Pass 1: per-item data changes (delete or update item-level fields only).
                # meal_type is NOT included here — it is a meal-level attribute that applies
                # to all surviving rows, handled separately in Pass 2.
                for item in correction_items:
                    fid = item.get("food_log_id")
                    if fid is None or fid not in all_ids:
                        continue
                    action = item.get("action", "update")
                    if action == "delete":
                        cur.execute(
                            "DELETE FROM nutrition.food_log WHERE food_log_id = %s",
                            (fid,),
                        )
                        deleted_ids.add(fid)
                    else:
                        # Build UPDATE only for explicitly provided, non-null fields.
                        # A missing key or null value means "keep existing" — never write null
                        # to the DB based on an absent or default LLM response.
                        # _food_meta / _macro_* are injected by re-estimation (underscore prefix
                        # distinguishes them from user-explicit macro values).
                        updates: dict[str, object] = {}
                        for col in ("food_item", "kcal", "protein_g", "carbs_g",
                                    "fat_g", "fibre_g", "sugar_g", "sodium_mg"):
                            if col in item and item[col] is not None:
                                updates[col] = item[col]
                        if "_food_meta" in item:
                            fm = item["_food_meta"] or {}
                            updates["food_meta"] = psycopg2.extras.Json(fm) if fm else None
                        if "_macro_input" in item:
                            updates["macro_input"] = item["_macro_input"]
                        if "_macro_method" in item:
                            updates["macro_method"] = item["_macro_method"]
                        if "_macro_meta" in item:
                            updates["macro_meta"] = psycopg2.extras.Json(item["_macro_meta"])
                        if updates:
                            set_clause = ", ".join(f"{col} = %s" for col in updates)
                            values = list(updates.values()) + [fid]
                            cur.execute(
                                f"UPDATE nutrition.food_log SET {set_clause} WHERE food_log_id = %s",
                                values,
                            )
                        updated_ids.add(fid)

                # Pass 2: if meal_type changed, apply it to ALL surviving rows in one shot.
                # This must happen even when specific items were also edited — a correction
                # like "that was dinner, and the iced coffee was 0 sugar" must move every
                # item in the meal to dinner, not just the one that was data-edited.
                surviving = [i for i in all_ids if i not in deleted_ids]
                if new_meal_type != original_meal_type and surviving:
                    cur.execute(
                        "UPDATE nutrition.food_log SET meal_type = %s WHERE food_log_id = ANY(%s)",
                        (new_meal_type, surviving),
                    )
                log_event(
                    logger,
                    logging.INFO,
                    "food_correction_db_updates_completed",
                    deleted_count=len(deleted_ids),
                    updated_count=len(updated_ids),
                    surviving_count=len(surviving),
                    meal_type_changed=new_meal_type != original_meal_type,
                )
                return surviving
    finally:
        conn.close()


# Formats the correction confirmation reply.
def _format_correction_reply(meal_type: str, items: list[dict], changes: list[dict]) -> str:
    deleted_count = sum(1 for c in changes if c.get("action") == "delete")

    if not items:
        return f"Done — all items removed from the {meal_type.replace('_', ' ')} log."

    lines = [f"Updated {meal_type.replace('_', ' ')}:"]
    for item in items:
        line = f"• {item.get('food_item', '?')}"
        macros = []
        if item.get("kcal") is not None:
            macros.append(f"{float(item['kcal']):.0f} kcal")
        if item.get("protein_g") is not None:
            macros.append(f"{float(item['protein_g']):.0f}g protein")
        if item.get("fat_g") is not None:
            macros.append(f"{float(item['fat_g']):.0f}g fat")
        if item.get("carbs_g") is not None:
            macros.append(f"{float(item['carbs_g']):.0f}g carbs")
        if macros:
            line += " — " + ", ".join(macros)
        lines.append(line)

    if deleted_count:
        lines.append(f"\n({deleted_count} item{'s' if deleted_count > 1 else ''} removed.)")

    lines.append("\nAnything else to change?")
    return "\n".join(lines)


# Strips markdown code fences if the LLM wraps its response, then parses JSON.
def _parse_json(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", raw).strip()
    return json.loads(cleaned)
