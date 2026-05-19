"""
Food correction handler — applies B's quoted correction to previously logged food_log rows.

Functions:
  handle_food_correction(msg, state)              — route correction: awaiting_clearer_photo, awaiting_quantity, or normal edit
  _format_history_section(history)               — formats prior correction texts into a prompt section string
  _handle_awaiting_clearer_photo(msg, context)   — re-runs extraction with a new photo when the original label was unreadable
  _handle_awaiting_quantity(msg, context)         — re-downloads saved label photo and runs extraction with B's quantity reply
  _handle_photo_correction(msg, current_items)    — re-reads macros from a correction photo via vision model, applies changes
  _reestimate_item(original, correction_text, ...) — re-estimates macros when food name or quantity changes
  _fetch_items(food_log_ids)                      — fetches food_log rows by ID list
  _format_items_for_llm(items)                    — formats items as readable text for correction LLM prompts
  _apply_corrections(correction_items, ...)       — applies parsed corrections to DB; returns surviving food_log_ids
  _compute_reply_scope(correction_items, ...)    — determines which ids to show in reply (meal move vs item edit vs delete-only)
"""

import dataclasses
import logging
import os

import psycopg2.extras

from domains.food.service import _build_item_results, _parse_json, handle_food_log
from domains.food.nutrition_sources import usda, off
from domains.food.nutrition_sources.router import enrich_item
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

# Maximum number of past correction texts to carry forward in conversation_state.
# Caps prompt growth for users who make many sequential corrections to the same meal.
_MAX_HISTORY = 5

_REESTIMATE_PROMPT = """\
You are re-estimating a food log entry after a user correction.

About the user: Singaporean Chinese, based between Singapore and Bangkok. \
Eats Singaporean, Thai, and Southern Chinese food on top of regular western fare. \
Common meals include hawker dishes (chicken rice, char kway teow, laksa, wonton noodles), \
Thai dishes (pad kra pao, moo ping, khao soi, boat noodles), and Southern Chinese food \
(dim sum, congee, braised meats). Use this context when estimating portion sizes and macros.

Previously logged: {previously_logged}
{history_section}User correction: {correction_text}

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

{history_section}Correction from user: {correction_text}

Determine what the user wants to change. They may want to:
- Change the meal type (e.g. "that was dinner not lunch")
- Update macros for one or more items (e.g. "actually the chicken rice was 600 kcal")
- Delete one or more items (e.g. "remove the yoghurt", "delete that")
- Update the food description or quantity
- Pick a different candidate from the list shown (e.g. "go with b", "try c", "use d", \
"no it should be b") — extract the letter as candidate_letter
- Escape to LLM estimate (e.g. "use LLM", "skip USDA", "skip Open Food Facts") — \
set skip_structured_source to true

Return a JSON object with this exact structure:
{{
  "meal_type": "<meal type — use original if unchanged>",
  "candidate_letter": "<letter a-z or null — set when user picks a candidate from the list>",
  "skip_structured_source": <true or false — set true when user wants LLM estimate instead>,
  "items": [
    {{
      "food_log_id": <int — the id of the existing row to update or delete>,
      "action": "<update or delete>",
      "food_item": "<description — ONLY include if the user explicitly changed the name>",
      "food_meta": {{"qty": {{"amount": <number>, "unit": "<string>"}}}} ,
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
- food_meta.qty: include ONLY if the user explicitly stated a new weight or quantity \
(e.g. "actually 70g", "2 servings"). When only quantity changes, do NOT include macro \
fields — the system will re-estimate them from the new quantity
- meal_type must be one of: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout
- meal_type: only change if the user explicitly mentions it
- "pre workout", "pre-workout", "post workout", "post-workout" always refer to meal_type changes — \
they are meal timing labels, NOT food item names; never rename food_item based on these words
- candidate_letter: set when the user references a letter from the candidate list shown \
(e.g. "go with b" → "b", "try c" → "c"). If no candidate pick, set to null.
- skip_structured_source: set to true only when user explicitly wants LLM instead of USDA/OFF
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""

_PHOTO_CORRECTION_PROMPT = """\
You are correcting previously logged food entries using a new photo provided by the user.

Previously logged items:
{logged_items}

Caption from user (may be empty): {caption}
{history_section}
The user has sent a photo to correct the logged entries. The photo may be:
- A nutrition label: read macros directly from the label. Pro-rate by quantity if the caption or \
previously logged food_meta specifies how much was consumed. If no quantity is known, use 1 serving.
- A food image: use the image and caption together to estimate corrected macros.

For each previously logged item that can be corrected from this photo, return an update.
If the photo clearly shows the correct values for an item, update it.
If the photo is unrelated to a logged item, omit that item.

Return a JSON object with this exact structure:
{{
  "photo_type": "<nutrition_label or macro_screenshot or food_image>",
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
- photo_type: "nutrition_label" if government-mandated nutrition facts panel with a sodium row; \
"macro_screenshot" if printed nutrition numbers from a non-panel source (restaurant menu, meal \
service card, app screenshot, simplified macro display without sodium); "food_image" otherwise
- For fields not changed, OMIT the key entirely — do NOT include null
- Return valid JSON only. No explanation, no markdown, no code blocks.\
"""


# Applies a candidate pick action to a food_log row and returns formatted reply pairs.
# Called when the user picks a different candidate from the list (e.g. "go with b").
#
# action_entry is from candidate_letter_map:
#   {"action": "candidate", "index": i}   — apply stored per-100g values × (grams/100)
#   {"action": "cross_source", "source": key} — fresh API lookup on the other source
#   {"action": "llm"}                     — re-estimate with LLM (handled by skip_structured_source path)
#
# Returns list of (reply, state) on success, or None if the action could not be applied
# (caller falls through to normal correction handling).
def _apply_candidate_action(
    action_entry: dict,
    original_item: dict,
    macro_meta: dict,
    update_id: int | None,
) -> list[tuple[str, dict | None]] | None:
    _MACRO_COLS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")
    fid = original_item["food_log_id"]
    act = action_entry.get("action")

    food_meta = original_item.get("food_meta") or {}
    qty = food_meta.get("qty") or {}
    grams: float | None = None
    if qty.get("amount") is not None and qty.get("unit"):
        from domains.food.nutrition_sources.router import to_grams
        grams = to_grams({"qty": qty})

    if act == "candidate":
        idx = action_entry.get("index", 0)
        source_candidates: list = macro_meta.get("source_candidates") or []
        if idx >= len(source_candidates):
            return None
        cand = source_candidates[idx]
        per_100g = cand.get("nutrients_per_100g") or {}

        updates: dict = {}
        factor = (grams / 100.0) if grams else 1.0
        for col in _MACRO_COLS:
            raw = per_100g.get(col)
            if raw is not None:
                updates[col] = round(raw * factor, 1)

        candidate_name = cand.get("label", "?")
        source = macro_meta.get("structured_source", "usda")
        # Rebuild field_sources with new candidate_name.
        field_sources = {
            col: {
                "status": "from_source",
                "source": source,
                "scaling_g": grams,
                "candidate_name": candidate_name,
            }
            for col in _MACRO_COLS if col in updates
        }
        # Rebuild candidate_letter_map with new selection (index 0 moves to chosen candidate).
        old_candidates = list(source_candidates)
        chosen = old_candidates.pop(idx)
        new_order = [chosen] + old_candidates
        letters = "abcdefghijklmnopqrstuvwxyz"
        old_letter_map = macro_meta.get("candidate_letter_map") or {}
        untried_source = macro_meta.get("untried_source")
        new_letter_map: dict = {}
        for i, _c in enumerate(new_order):
            if i >= len(letters):
                break
            new_letter_map[letters[i]] = {"action": "candidate", "index": i}
        next_i = len(new_order)
        if untried_source and next_i < len(letters):
            new_letter_map[letters[next_i]] = {"action": "cross_source", "source": untried_source}
            next_i += 1
        if next_i < len(letters):
            new_letter_map[letters[next_i]] = {"action": "llm"}

        new_macro_meta = {
            **macro_meta,
            "candidate_name": candidate_name,
            "field_sources": field_sources,
            "source_candidates": new_order,
            "candidate_letter_map": new_letter_map,
            "correction_update_id": update_id,
        }

        updates["_macro_meta"] = new_macro_meta
        updates["food_log_id"] = fid
        updates["action"] = "update"

    elif act == "cross_source":
        source_key = action_entry.get("source", "")
        lookup_fn = usda.lookup if source_key == "usda" else off.lookup
        if grams is None:
            return None
        food_item = original_item.get("food_item", "")
        try:
            result, new_candidates = lookup_fn(food_item, grams, update_id)
        except Exception as e:
            log_failure(logger, logging.WARNING, "food_correction_cross_source_failed", e,
                        update_id=update_id, source=source_key)
            result, new_candidates = None, []

        if result is None:
            _SOURCE_DISPLAY = {"usda": "USDA", "open_food_facts": "Open Food Facts"}
            display = _SOURCE_DISPLAY.get(source_key, source_key)
            # Return a no-match message — caller should not fall through to normal correction.
            log_event(logger, logging.INFO, "food_correction_cross_source_no_match",
                      update_id=update_id, source=source_key)
            return [(f"No match found in {display} — reverting to LLM estimate.", None)]

        updates = {}
        for col in _MACRO_COLS:
            if col in result:
                updates[col] = result[col]

        candidate_name = result.get("_candidate_name", "?")
        field_sources = {
            col: {
                "status": "from_source",
                "source": source_key,
                "scaling_g": grams,
                "candidate_name": candidate_name,
            }
            for col in _MACRO_COLS if col in updates
        }
        # Build new letter map for the cross-source candidates.
        old_untried = macro_meta.get("untried_source")
        letters = "abcdefghijklmnopqrstuvwxyz"
        new_letter_map_cs: dict = {}
        for i, _c in enumerate(new_candidates):
            if i >= len(letters):
                break
            new_letter_map_cs[letters[i]] = {"action": "candidate", "index": i}
        next_i = len(new_candidates)
        # Cross-source was the untried source — now it's tried, no new untried.
        if next_i < len(letters):
            new_letter_map_cs[letters[next_i]] = {"action": "llm"}

        new_macro_meta = {
            **macro_meta,
            "candidate_name": candidate_name,
            "structured_source": source_key,
            "field_sources": field_sources,
            "source_candidates": new_candidates,
            "candidate_letter_map": new_letter_map_cs,
            "untried_source": None,
            "correction_update_id": update_id,
        }
        updates["_macro_meta"] = new_macro_meta
        updates["_macro_method"] = source_key
        updates["_macro_input"] = "description"
        updates["food_log_id"] = fid
        updates["action"] = "update"

    else:
        return None

    # Apply to DB.
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                import psycopg2.extras
                db_updates: dict = {}
                for col in _MACRO_COLS:
                    if col in updates:
                        db_updates[col] = updates[col]
                if "_macro_meta" in updates:
                    db_updates["macro_meta"] = psycopg2.extras.Json(updates["_macro_meta"])
                if "_macro_method" in updates:
                    db_updates["macro_method"] = updates["_macro_method"]
                if "_macro_input" in updates:
                    db_updates["macro_input"] = updates["_macro_input"]
                if db_updates:
                    set_clause = ", ".join(f"{col} = %s" for col in db_updates)
                    values = list(db_updates.values()) + [fid]
                    cur.execute(
                        f"UPDATE nutrition.food_log SET {set_clause} WHERE food_log_id = %s",
                        values,
                    )
                    log_event(logger, logging.INFO, "food_correction_candidate_applied",
                              update_id=update_id, food_log_id=fid, action=act)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_candidate_apply_failed", e,
                    update_id=update_id, food_log_id=fid)
        return None
    finally:
        conn.close()

    # Re-fetch and return.
    try:
        updated_items = _fetch_items([fid])
    except Exception:
        return None
    if not updated_items:
        return None

    meal_type = original_item.get("meal_type", "snack")
    return _build_item_results(updated_items, [fid], meal_type)


# Handles a correction to a previously logged food entry.
# B quotes a bot reply → router looks up conversation_state → calls this function.
#
# Two sub-cases:
#   awaiting_quantity — B replied with a quantity for a saved label photo (no food logged yet).
#                       Re-downloads the saved photo, re-runs handle_food_log with the quantity
#                       as caption. B never needs to resend the photo.
#   normal correction — B is editing a previously logged food_log row (food_log_ids in context).
#
# Inputs:
#   msg   — the inbound correction message (msg.text is the correction text or quantity)
#   state — conversation_state row dict from load_state()
# Outputs: list of (reply_text, pending_state) — one per surviving food item.
def handle_food_correction(msg: InboundMessage, state: dict) -> list[tuple[str, dict | None]]:
    context = state.get("context") or {}

    # awaiting_clearer_photo path: bot couldn't read a label and B is sending a clearer photo.
    if context.get("awaiting_clearer_photo"):
        return _handle_awaiting_clearer_photo(msg, context)

    # awaiting_quantity path: bot asked "how much did you have?" and B replied with the quantity.
    if context.get("awaiting_quantity"):
        return _handle_awaiting_quantity(msg, context)

    food_log_ids = context.get("food_log_ids") or []
    meal_food_log_ids = context.get("meal_food_log_ids") or food_log_ids
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
        return [("Nothing to correct — couldn't find the original log entries.", None)]

    # Validate that the message carries actionable content before touching the DB.
    # Photo path needs only file_id (no text required); text path needs text or caption.
    # Both checks happen here so a blank quoted reply always gets the prompt, never a DB error.
    is_photo = msg.message_type == MessageType.PHOTO and msg.file_id
    correction_text = msg.text or msg.caption
    if not is_photo and not correction_text:
        log_event(logger, logging.WARNING, "food_correction_missing_text", update_id=msg.update_id)
        return [("What would you like to change? Send me a text description of the correction.", None)]

    # Fetch current state of the logged items from the DB.
    # Use meal_food_log_ids (the full batch) so the LLM sees the whole meal for context —
    # e.g. "that was dinner" should inform the model that there are 3 items moving to dinner,
    # not just the one item B quoted. The quoted food_log_ids are highlighted in the prompt.
    ids_to_fetch = meal_food_log_ids or food_log_ids
    try:
        current_items = _fetch_items(ids_to_fetch)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_fetch_failed", e, update_id=msg.update_id)
        return [("Couldn't load the original log — please try again.", None)]

    if not current_items:
        log_event(logger, logging.WARNING, "food_correction_original_items_missing", update_id=msg.update_id)
        return [("Nothing to correct — the original items may have been deleted already.", None)]

    # Route to photo correction path if B attached a photo to the correction message.
    if is_photo:
        return _handle_photo_correction(msg, current_items, original_meal_type, state)

    # Format items for LLM context, marking the quoted item(s) as correction targets.
    logged_items_text = _format_items_for_llm(current_items, quoted_ids=set(food_log_ids))

    # Build correction history section — carries prior correction texts forward so the LLM
    # has full context when B makes sequential corrections to the same meal.
    correction_history: list[str] = context.get("correction_history") or []
    history_section = _format_history_section(correction_history)

    # Ask LLM to parse the correction
    try:
        raw = generate_text(
            _CORRECTION_PROMPT.format(
                logged_items=logged_items_text,
                history_section=history_section,
                correction_text=correction_text,
            ),
            model=MODEL_FLASH,
        )
        parsed = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_parse_failed", e, update_id=msg.update_id)
        return [("Couldn't understand the correction — can you rephrase?", None)]

    new_meal_type = parsed.get("meal_type", original_meal_type)
    if new_meal_type not in _VALID_MEAL_TYPES:
        new_meal_type = original_meal_type

    correction_items = parsed.get("items", [])
    candidate_letter = parsed.get("candidate_letter")
    skip_structured_source = parsed.get("skip_structured_source", False)
    log_event(
        logger,
        logging.INFO,
        "food_correction_parsed",
        update_id=msg.update_id,
        item_count=len(correction_items),
        meal_type=new_meal_type,
        candidate_letter=candidate_letter,
        skip_structured_source=skip_structured_source,
    )

    # Handle candidate pick — only when no food_item change is present.
    # food_item change beats candidate pick: re-run enrich_item fresh on the new name.
    if candidate_letter and not any("food_item" in ci for ci in correction_items if ci.get("action") != "delete"):
        # Find the quoted item to apply the candidate pick to.
        quoted_item = next((r for r in current_items if r["food_log_id"] in food_log_ids), None)
        if quoted_item is not None:
            macro_meta = quoted_item.get("macro_meta") or {}
            letter_map = macro_meta.get("candidate_letter_map") or {}
            action_entry = letter_map.get(candidate_letter)
            if action_entry:
                candidate_result = _apply_candidate_action(
                    action_entry, quoted_item, macro_meta, msg.update_id
                )
                if candidate_result is not None:
                    return candidate_result

    # Handle skip_structured_source (LLM escape) — re-estimate without structured source.
    if skip_structured_source and not any("food_item" in ci for ci in correction_items if ci.get("action") != "delete"):
        quoted_item = next((r for r in current_items if r["food_log_id"] in food_log_ids), None)
        if quoted_item is not None:
            reestimated = _reestimate_item(quoted_item, correction_text, msg.update_id, correction_history)
            if reestimated is not None:
                fid = quoted_item["food_log_id"]
                updates: dict = {
                    "macro_method": "llm",
                    "macro_input": "description",
                    "_macro_meta": {
                        "model": MODEL_FLASH,
                        "correction_update_id": msg.update_id,
                        "skip_structured_source": True,
                    },
                }
                for col in ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg"):
                    if reestimated.get(col) is not None:
                        updates[col] = reestimated[col]
                skip_item = {"food_log_id": fid, "action": "update", **updates}
                # Inject as the sole correction item and fall through to _apply_corrections.
                correction_items = [skip_item]

    if not correction_items and new_meal_type == original_meal_type:
        log_event(logger, logging.WARNING, "food_correction_no_changes", update_id=msg.update_id)
        # Preserve state so B can quote again and try a different correction.
        noop_state: dict = {
            "domain": "food",
            "context": {
                "food_log_ids": food_log_ids,
                "meal_food_log_ids": meal_food_log_ids,
                "meal_type": original_meal_type,
            },
            "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
        }
        if correction_history:
            noop_state["context"]["correction_history"] = correction_history
        return [("Got your message — nothing seemed to need changing. What did you want to correct?", noop_state)]

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

        # Quantity change: LLM returns food_meta.qty but no macros → re-estimate from new weight.
        # This is distinct from explicit macro correction ("kcal is 300") which stays as "manual".
        correction_food_meta = item.pop("food_meta", None) or {}
        has_qty_change = bool(correction_food_meta.get("qty"))

        if "food_item" in item or has_qty_change:
            # Re-estimate: food name changed, OR quantity changed (macros scale with weight).
            fid = item.get("food_log_id")
            original = next((r for r in current_items if r["food_log_id"] == fid), None)
            if original is not None:
                reestimated = _reestimate_item(original, correction_text, msg.update_id, correction_history)
                if reestimated is None:
                    log_event(
                        logger,
                        logging.WARNING,
                        "food_correction_reestimate_skipped",
                        update_id=msg.update_id,
                        food_log_id=fid,
                    )
                    # Re-estimation failed. Macros stay unchanged.
                    # If the user stated a new quantity, still persist food_meta.qty so the
                    # logged quantity is at least correct even though macros weren't scaled.
                    if has_qty_change:
                        original_food_meta = original.get("food_meta") or {}
                        merged_food_meta = {**original_food_meta, **correction_food_meta}
                        if merged_food_meta:
                            item["_food_meta"] = merged_food_meta
                else:
                    if "food_item" in item:
                        # food_item: use re-estimation (has full context of original + correction text)
                        item["food_item"] = reestimated.get("food_item") or item["food_item"]
                    # macros: re-estimation as base; user-stated values (snapshotted above) take priority
                    for col in _MACRO_COLS:
                        if col not in item:
                            item[col] = reestimated.get(col)
                    # food_meta: three-layer merge so existing brand/prep/notes are never wiped.
                    # Priority (low → high): original DB row → re-estimation → user correction.
                    original_food_meta = original.get("food_meta") or {}
                    reestimated_food_meta = reestimated.get("food_meta") or {}
                    merged_food_meta = {**original_food_meta, **reestimated_food_meta, **correction_food_meta}
                    if merged_food_meta:
                        item["_food_meta"] = merged_food_meta
                    item["_macro_meta"] = {
                        "model": MODEL_FLASH,
                        **({"corrected_from": original["food_item"]} if "food_item" in item else {}),
                        "correction_update_id": msg.update_id,
                        **({"user_stated_fields": user_stated} if user_stated else {}),
                    }
        elif correction_food_meta:
            # food_meta changed but no qty (e.g. brand/prep note) — no re-estimation needed,
            # just persist the updated metadata merged on top of the original so existing
            # brand/prep/notes/qty are not wiped by a partial LLM response.
            fid = item.get("food_log_id")
            original = next((r for r in current_items if r["food_log_id"] == fid), None)
            original_food_meta = (original.get("food_meta") or {}) if original else {}
            item["_food_meta"] = {**original_food_meta, **correction_food_meta}

        # Set macro provenance. Applies whether or not food_item / qty changed.
        # "manual" when B explicitly stated any macro value; "llm" when macros came from re-estimation.
        # Quantity-only corrections never set user_stated, so they always land in the llm branch.
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
            # food_item or qty changed, re-estimation ran, no explicit macros — all LLM.
            item["_macro_input"] = "description"
            item["_macro_method"] = "llm"

    # Apply changes to DB.
    # all_ids uses meal_food_log_ids (the full batch) so the LLM can act on any meal item —
    # e.g. "remove the iced coffee too" while quoting the chicken row is correctly applied.
    # The quoted food_log_ids are already marked as [quoted target] in the LLM prompt, so the
    # model knows which item triggered the correction without an allowlist filter here.
    try:
        surviving_ids, applied_deleted_ids, applied_updated_ids = _apply_corrections(
            correction_items=correction_items,
            new_meal_type=new_meal_type,
            original_meal_type=original_meal_type,
            all_ids=meal_food_log_ids,
            meal_ids=meal_food_log_ids,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_correction_apply_failed", e, update_id=msg.update_id)
        return [("Correction parsed but failed to save — please try again.", None)]

    new_history = (correction_history + [correction_text])[-_MAX_HISTORY:] if correction_text else correction_history
    deleted_count = len(applied_deleted_ids)
    deleted_from_batch = set(meal_food_log_ids) - set(surviving_ids)
    updated_meal_ids = [i for i in meal_food_log_ids if i not in deleted_from_batch]

    reply_ids, early_result = _compute_reply_scope(
        applied_deleted_ids, applied_updated_ids, surviving_ids, current_items, new_meal_type, original_meal_type,
    )
    if early_result is not None:
        log_event(logger, logging.INFO, "food_correction_completed",
                  update_id=msg.update_id, surviving_item_count=len(surviving_ids),
                  reply_item_count=0, meal_type=new_meal_type)
        return early_result

    # Build updated item list for the reply.
    # _apply_corrections already committed — guard this re-fetch so a DB hiccup here
    # does not silence the reply entirely after a successful write.
    try:
        updated_items = _fetch_items(reply_ids) if reply_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_correction_refetch_failed", e, update_id=msg.update_id)
        if surviving_ids:
            fallback_state: dict = {
                "domain": "food",
                "context": {
                    "food_log_ids": surviving_ids,
                    "meal_food_log_ids": updated_meal_ids,
                    "meal_type": new_meal_type,
                    "correction_history": new_history,
                },
                "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
            }
            return [("Correction saved — quote this message to correct further.", fallback_state)]
        updated_items = []
    log_event(
        logger,
        logging.INFO,
        "food_correction_completed",
        update_id=msg.update_id,
        surviving_item_count=len(surviving_ids),
        reply_item_count=len(reply_ids),
        meal_type=new_meal_type,
    )

    if not updated_items:
        return [(f"Done — all items removed from the {new_meal_type.replace('_', ' ')} log.", None)]

    return _build_item_results(
        updated_items,
        [item["food_log_id"] for item in updated_items],
        new_meal_type,
        updated_meal_ids,
        correction_history=new_history,
        parent_reply_id=state["telegram_reply_message_id"],
        deleted_count=deleted_count,
    )


# Formats the correction history list into a prompt section string.
# Returns an empty string when there is no history so prompts are not polluted with empty headers.
# Inputs: list of prior correction texts (already capped at _MAX_HISTORY by callers).
# Outputs: formatted string ready to embed in an LLM prompt as the {history_section} placeholder.
def _format_history_section(history: list[str]) -> str:
    if not history:
        return ""
    lines = ["Prior corrections in this thread (oldest first):"]
    lines += [f"- {h}" for h in history]
    lines.append("")  # blank line before "Correction from user:"
    return "\n".join(lines) + "\n"


# Handles the awaiting_quantity correction path.
#
# Called when the bot previously returned "how much did you have?" after seeing a nutrition label
# with no quantity in the caption. The state context contains awaiting_quantity=True and the
# file_ids of the original label photo(s).
#
# B's reply text is treated as the quantity. This function reconstructs a photo message using
# the saved file_id and B's reply as the caption, then calls handle_food_log — the full label
# extraction flow runs again with the quantity now available. B never needs to resend the photo.
#
# Inputs:
#   msg     — B's quantity reply (msg.text e.g. "150g", "2 servings")
#   context — context dict from conversation_state (must contain file_ids list)
# Outputs: (reply_text, pending_state) — same contract as handle_food_correction.
def _handle_awaiting_quantity(msg: InboundMessage, context: dict) -> list[tuple[str, dict | None]]:
    quantity_text = msg.text or msg.caption
    file_ids = context.get("file_ids") or []
    original_caption = context.get("original_caption") or ""

    log_event(
        logger,
        logging.INFO,
        "food_awaiting_quantity_received",
        update_id=msg.update_id,
        has_quantity=bool(quantity_text),
        has_original_caption=bool(original_caption),
        file_id_count=len(file_ids),
    )

    if not quantity_text:
        return [("What quantity did you have? (e.g. '150g', '1 serving', 'half a bar')", None)]
    if not file_ids:
        log_event(logger, logging.WARNING, "food_awaiting_quantity_no_file_ids", update_id=msg.update_id)
        return [("Can't find the original photo — please resend it with the quantity in the caption.", None)]

    # Merge original caption (food context) with quantity reply so neither is lost.
    # Example: original "protein bar plus banana" + quantity "150g" → "protein bar plus banana 150g".
    # Same join-with-space pattern as _handle_awaiting_clearer_photo.
    effective_caption = " ".join(filter(None, [original_caption, quantity_text])).strip()

    # Reconstruct a PHOTO message: file_id from saved state, merged caption.
    # handle_food_log routes this through _handle_photo, which now has both the food context and quantity.
    photo_msg = dataclasses.replace(
        msg,
        message_type=MessageType.PHOTO,
        file_id=file_ids[0],
        caption=effective_caption,
        text=None,
    )
    return handle_food_log(photo_msg)


# Handles the awaiting_clearer_photo correction path.
#
# Called when the bot returned "can't read the label clearly" and B quotes that reply
# to send a clearer photo. The original caption (quantity / food name) was saved in state
# so B does not need to retype it.
#
# If B's reply has a photo: reconstruct message with new photo + restored original caption,
# then re-run handle_food_log — full extraction runs with the caption intact.
# If B's reply has no photo: ask B to actually send the photo.
#
# Inputs:
#   msg     — B's reply (should be a PHOTO message with the clearer label)
#   context — context dict from conversation_state (may contain original_caption)
# Outputs: (reply_text, pending_state) — same contract as handle_food_correction.
def _handle_awaiting_clearer_photo(msg: InboundMessage, context: dict) -> list[tuple[str, dict | None]]:
    original_caption = context.get("original_caption") or ""
    log_event(
        logger,
        logging.INFO,
        "food_awaiting_clearer_photo_received",
        update_id=msg.update_id,
        has_photo=bool(msg.message_type == MessageType.PHOTO and msg.file_id),
        has_original_caption=bool(original_caption),
    )

    if msg.message_type != MessageType.PHOTO or not msg.file_id:
        return [(
            "Please send a clearer photo of the label — or type the nutrition values instead.",
            None,
        )]

    # Combine original caption (food info B sent first) with new caption (may correct or add info).
    # Gemini resolves conflicts — e.g. "1 serving actually 2 servings" → 2 servings.
    # Joining both means neither the original quantity nor a new correction gets silently dropped.
    effective_caption = " ".join(filter(None, [original_caption, msg.caption])).strip() or ""
    photo_msg = dataclasses.replace(
        msg,
        caption=effective_caption,
        text=None,
    )
    return handle_food_log(photo_msg)


# Maps photo_type → (macro_input, macro_method) for DB provenance columns.
# Tuple positions: index 0 = macro_input, index 1 = macro_method.
# "nutrition_label" — government-mandated panel: both input and method are "nutrition_label"
# "macro_screenshot" — restaurant menu, meal service card, simplified macro display: "restaurant_reported"
# "food_image" — food photo, no numbers: "image" input, "llm" method
_PHOTO_TYPE_PROVENANCE: dict[str, tuple[str, str]] = {
    "nutrition_label": ("nutrition_label", "nutrition_label"),
    "macro_screenshot": ("macro_screenshot", "restaurant_reported"),
    "food_image": ("image", "llm"),
}


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
) -> list[tuple[str, dict | None]]:
    food_log_ids = [item["food_log_id"] for item in current_items]
    log_event(logger, logging.INFO, "food_photo_correction_started", update_id=msg.update_id)
    context = state.get("context") or {}
    meal_food_log_ids: list[int] = context.get("meal_food_log_ids") or food_log_ids
    correction_history: list[str] = context.get("correction_history") or []
    history_section = _format_history_section(correction_history)

    try:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        image_bytes = get_file_bytes(msg.file_id, token)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_download_failed", e, update_id=msg.update_id)
        return [("Couldn't download the photo — please try again.", None)]

    logged_items_text = _format_items_for_llm(current_items)
    caption = msg.caption or msg.text or ""

    try:
        raw = generate_with_image(
            image_bytes=image_bytes,
            prompt=_PHOTO_CORRECTION_PROMPT.format(
                logged_items=logged_items_text,
                caption=caption,
                history_section=history_section,
            ),
            model=MODEL_FLASH,
        )
        parsed = _parse_json(raw)
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_parse_failed", e, update_id=msg.update_id)
        return [("Couldn't read the photo — can you try again or type the correction instead?", None)]

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
        return [("Looked at the photo but couldn't see what needed changing — can you describe it in text?", None)]

    # Mark all photo-corrected macros as coming from a label/image re-read.
    # photo_type is returned by the vision model and is the authoritative signal —
    # do NOT infer from caption presence (a label photo can arrive with or without a caption).
    # macro_meta shape follows the schema contract: nutrition_label rows must include file_id.
    photo_type = parsed.get("photo_type", "food_image")
    macro_input, macro_method = _PHOTO_TYPE_PROVENANCE.get(photo_type, ("image", "llm"))
    correction_meta: dict = {
        "model": MODEL_FLASH,
        "correction_source": "photo",
        "photo_type": photo_type,
        "correction_update_id": msg.update_id,
    }
    if photo_type in ("nutrition_label", "macro_screenshot") and msg.file_id:
        correction_meta["file_id"] = msg.file_id

    # P1a fix: only change row-level macro_input/macro_method if all four core macros are
    # present in the correction. A partial photo read (e.g. only kcal + protein returned)
    # should not re-stamp the row as nutrition_label/restaurant_reported — that would claim
    # the old LLM-estimated carbs/fat came from the photo source, which is false.
    # When partial, provenance is tracked at field level via field_sources only.
    _CORE_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g")
    _MACRO_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g", "sugar_g", "sodium_mg")
    for item in correction_items:
        if item.get("action") != "delete":
            all_core_present = all(
                field in item and item[field] is not None for field in _CORE_FIELDS
            )
            if all_core_present:
                item["_macro_input"] = macro_input
                item["_macro_method"] = macro_method
            # Always record field-level provenance for every field returned by the model.
            fields_from_photo = [f for f in _MACRO_FIELDS if f in item and item[f] is not None]
            item_meta = dict(correction_meta)
            if fields_from_photo:
                item_meta["field_sources"] = {
                    f: {"status": "from_source", "source": photo_type} for f in fields_from_photo
                }
            item["_macro_meta"] = item_meta

    try:
        surviving_ids, applied_deleted_ids, applied_updated_ids = _apply_corrections(
            correction_items=correction_items,
            new_meal_type=new_meal_type,
            original_meal_type=original_meal_type,
            all_ids=meal_food_log_ids,
            meal_ids=meal_food_log_ids,
        )
    except Exception as e:
        log_failure(logger, logging.ERROR, "food_photo_correction_apply_failed", e, update_id=msg.update_id)
        return [("Photo read but failed to save the correction — please try again.", None)]

    photo_note = f"[photo correction: {caption}]" if caption else "[photo correction]"
    new_history = (correction_history + [photo_note])[-_MAX_HISTORY:]
    deleted_count = len(applied_deleted_ids)
    deleted_from_batch = set(meal_food_log_ids) - set(surviving_ids)
    updated_meal_ids = [i for i in meal_food_log_ids if i not in deleted_from_batch]

    reply_ids, early_result = _compute_reply_scope(
        applied_deleted_ids, applied_updated_ids, surviving_ids, current_items, new_meal_type, original_meal_type,
    )
    if early_result is not None:
        log_event(logger, logging.INFO, "food_photo_correction_completed",
                  update_id=msg.update_id, surviving_item_count=len(surviving_ids),
                  reply_item_count=0)
        return early_result

    try:
        updated_items = _fetch_items(reply_ids) if reply_ids else []
    except Exception as e:
        log_failure(logger, logging.WARNING, "food_photo_correction_refetch_failed", e, update_id=msg.update_id)
        if surviving_ids:
            fallback_state: dict = {
                "domain": "food",
                "context": {
                    "food_log_ids": surviving_ids,
                    "meal_food_log_ids": updated_meal_ids,
                    "meal_type": new_meal_type,
                    "correction_history": new_history,
                },
                "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
            }
            return [("Correction saved — quote this message to correct further.", fallback_state)]
        updated_items = []

    log_event(
        logger,
        logging.INFO,
        "food_photo_correction_completed",
        update_id=msg.update_id,
        surviving_item_count=len(surviving_ids),
        reply_item_count=len(reply_ids),
        meal_type=new_meal_type,
    )

    if not updated_items:
        return [(f"Done — all items removed from the {new_meal_type.replace('_', ' ')} log.", None)]

    return _build_item_results(
        updated_items,
        [item["food_log_id"] for item in updated_items],
        new_meal_type,
        updated_meal_ids,
        correction_history=new_history,
        parent_reply_id=state["telegram_reply_message_id"],
        deleted_count=deleted_count,
    )


# Re-estimates a single food item using the original logged entry plus the user's correction text.
# Called when food_item name changes OR quantity changes — ensures macros, food_meta, and metadata
# columns are refreshed rather than inheriting stale values from the original.
# Inputs: original — the current food_log row dict; correction_text — what B said;
#         update_id — for logging; correction_history — prior corrections forwarded to the LLM for context.
# Returns a dict with food_item, macros, food_meta on success, or None on failure (caller falls back
# to rename-only behaviour so the correction is never blocked by a re-estimation error).
def _reestimate_item(original: dict, correction_text: str, update_id: int | None, correction_history: list[str] | None = None) -> dict | None:
    parts = [original.get("food_item", "?")]
    # Include original quantity so the LLM knows the baseline to scale from.
    # Without this, "actually 70g" when logged at 150g gets estimated from scratch
    # rather than scaled from the known baseline.
    orig_qty = (original.get("food_meta") or {}).get("qty") or {}
    if orig_qty.get("amount") is not None:
        unit = orig_qty.get("unit", "")
        qty_str = f"{orig_qty['amount']}{' ' + unit if unit else ''}"
        parts.append(f"[logged qty: {qty_str}]")
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

    history_section = _format_history_section(correction_history or [])
    try:
        raw = generate_text(
            _REESTIMATE_PROMPT.format(
                previously_logged=previously_logged,
                history_section=history_section,
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
               food_meta, macro_method, macro_meta
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
                        "macro_method": r[11] or "llm",
                        "macro_meta": r[12] or {},
                    }
                    for r in rows
                ]
    finally:
        conn.close()


# Determines which food_log_ids to include in the correction reply.
#
# Uses actual DB-applied sets (from _apply_corrections) rather than raw LLM items so that:
#   - hallucinated food_log_ids that were skipped by _apply_corrections never appear in reply text
#   - missing food_log_id keys in LLM output cannot cause a KeyError here
#
# Rules:
#   meal_type changed  → all surviving ids (B needs to see the full meal after a move)
#   update + delete    → only the updated ids (delete count badge appended on last card)
#   delete only        → None + early_result "Removed: <name>, ..." so B sees which item was removed
#   fallback           → all surviving ids
#
# Uses actual DB-applied sets (from _apply_corrections) rather than raw LLM items so that:
#   - hallucinated food_log_ids that were skipped by _apply_corrections never appear in reply text
#   - missing food_log_id keys in LLM output cannot cause a KeyError here
#
# Returns (reply_ids, early_result):
#   reply_ids    — list of ids to fetch and show, or None when early_result is set
#   early_result — ready-made result list for delete-only case, or None
def _compute_reply_scope(
    applied_deleted_ids: set[int],
    applied_updated_ids: set[int],
    surviving_ids: list[int],
    current_items: list[dict],
    new_meal_type: str,
    original_meal_type: str,
) -> tuple[list[int] | None, list[tuple[str, dict | None]] | None]:
    if new_meal_type != original_meal_type:
        return surviving_ids, None

    surviving_set = set(surviving_ids)
    reply_ids = [fid for fid in applied_updated_ids if fid in surviving_set]

    if not reply_ids and applied_deleted_ids:
        # Delete-only: return a plain confirmation naming the removed item(s).
        # B can still quote any remaining item card directly to keep correcting.
        deleted_names = [r["food_item"] for r in current_items if r["food_log_id"] in applied_deleted_ids]
        removed_str = ", ".join(deleted_names) if deleted_names else "item"
        return None, [(f"Removed: {removed_str}.", None)]

    return reply_ids if reply_ids else surviving_ids, None


# Formats current food_log rows as readable text for the correction LLM prompt.
# Includes logged quantity from food_meta so the model can pro-rate label macros correctly
# without needing B to repeat the quantity in the correction message.
def _format_items_for_llm(items: list[dict], quoted_ids: set[int] | None = None) -> str:
    # When quoted_ids is provided (per-item correction flow), items may include the full meal
    # for context. The quoted items are flagged so the LLM knows which items B is targeting.
    lines = []
    if quoted_ids:
        quoted_sorted = sorted(quoted_ids)
        lines.append(f"Quoted target food_log_ids: {', '.join(str(i) for i in quoted_sorted)}")
    for item in items:
        fid = item["food_log_id"]
        target_tag = " [quoted target]" if (quoted_ids and fid in quoted_ids) else ""
        parts = [f"[id={fid}] {item['food_item']}{target_tag}"]
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


# Applies parsed corrections: updates or deletes rows.
# Returns a 3-tuple: (surviving_ids, deleted_ids, updated_ids).
#   surviving_ids — food_log_ids from all_ids that were not deleted
#   deleted_ids   — set of food_log_ids that were DELETE'd this call
#   updated_ids   — set of food_log_ids where at least one column was actually written
def _apply_corrections(
    correction_items: list[dict],
    new_meal_type: str,
    original_meal_type: str,
    all_ids: list[int],
    meal_ids: list[int] | None = None,
) -> tuple[list[int], set[int], set[int]]:
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
                        # If updates is empty nothing was written; omit from updated_ids so the
                        # caller does not treat a no-op item as a successful update.

                # Pass 2: if meal_type changed, apply it to ALL surviving meal rows in one shot.
                # Uses meal_ids (the full batch) so "that was dinner" moves every item logged
                # together, not just the single quoted item. Falls back to all_ids when meal_ids
                # is not provided (e.g. old state without meal_food_log_ids).
                surviving = [i for i in all_ids if i not in deleted_ids]
                meal_scope = meal_ids if meal_ids is not None else all_ids
                surviving_meal = [i for i in meal_scope if i not in deleted_ids]
                if new_meal_type != original_meal_type and surviving_meal:
                    cur.execute(
                        "UPDATE nutrition.food_log SET meal_type = %s WHERE food_log_id = ANY(%s)",
                        (new_meal_type, surviving_meal),
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
                return surviving, deleted_ids, updated_ids
    finally:
        conn.close()
