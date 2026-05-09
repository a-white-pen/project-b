"""
Food correction handler — applies B's quoted correction to previously logged food_log rows.

Functions:
  handle_food_correction(msg, state) — parse correction, update food_log rows, return (reply, state)
"""

import json
import logging
import re

import psycopg2.extras

from system.db import get_connection
from system.llm import MODEL_FLASH, generate_text
from system.messages import InboundMessage

logger = logging.getLogger(__name__)

_VALID_MEAL_TYPES = {
    "breakfast", "brunch", "lunch", "snack",
    "dinner", "supper", "pre_workout", "post_workout",
}

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

    if not food_log_ids:
        return ("Nothing to correct — couldn't find the original log entries.", None)

    correction_text = msg.text or msg.caption
    if not correction_text:
        return ("What would you like to change? Send me a text description of the correction.", None)

    # Fetch current state of the logged items from the DB
    try:
        current_items = _fetch_items(food_log_ids)
    except Exception as e:
        logger.error("correction fetch failed update_id=%s: %s", msg.update_id, e)
        return ("Couldn't load the original log — please try again.", None)

    if not current_items:
        return ("Nothing to correct — the original items may have been deleted already.", None)

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
        logger.error("correction parse failed update_id=%s: %s", msg.update_id, e)
        return ("Couldn't understand the correction — can you rephrase?", None)

    new_meal_type = parsed.get("meal_type", original_meal_type)
    if new_meal_type not in _VALID_MEAL_TYPES:
        new_meal_type = original_meal_type

    correction_items = parsed.get("items", [])
    if not correction_items and new_meal_type == original_meal_type:
        return ("Got your message — nothing seemed to need changing. What did you want to correct?", None)

    # Apply changes to DB
    try:
        surviving_ids = _apply_corrections(
            correction_items=correction_items,
            new_meal_type=new_meal_type,
            original_meal_type=original_meal_type,
            all_ids=food_log_ids,
        )
    except Exception as e:
        logger.error("correction apply failed update_id=%s: %s", msg.update_id, e)
        return ("Correction parsed but failed to save — please try again.", None)

    # Build updated item list for the reply.
    # _apply_corrections already committed — guard this re-fetch so a DB hiccup here
    # does not silence the reply entirely after a successful write.
    try:
        updated_items = _fetch_items(surviving_ids) if surviving_ids else []
    except Exception as e:
        logger.warning("post-correction re-fetch failed update_id=%s: %s", msg.update_id, e)
        updated_items = []
    reply = _format_correction_reply(new_meal_type, updated_items, correction_items)

    new_state = {
        "domain": "food",
        "context": {"food_log_ids": surviving_ids, "meal_type": new_meal_type},
        "parent_telegram_reply_message_id": state["telegram_reply_message_id"],
    }
    return (reply, new_state)


# Fetches food_log rows for the given IDs. Returns list of row dicts.
def _fetch_items(food_log_ids: list[int]) -> list[dict]:
    if not food_log_ids:
        return []
    sql = """
        SELECT food_log_id, food_item, meal_type,
               kcal, protein_g, carbs_g, fat_g, fibre_g, sugar_g, sodium_mg
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
                    }
                    for r in rows
                ]
    finally:
        conn.close()


# Formats current food_log rows as readable text for the correction LLM prompt.
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
                        updates: dict[str, object] = {}
                        for col in ("food_item", "kcal", "protein_g", "carbs_g",
                                    "fat_g", "fibre_g", "sugar_g", "sodium_mg"):
                            if col in item and item[col] is not None:
                                updates[col] = item[col]
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
    finally:
        conn.close()

    return [i for i in all_ids if i not in deleted_ids]


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
