"""
Meal completion (BRIEF §6 "Tap ✓ Ate"): a `meal_ate:` callback posts the tapped item(s) into food_log
and marks the slot — ATOMICALLY (persistence.claim_and_post locks the row, so concurrent/double taps
can't double-log, and a crash can't orphan). The posted rows are restaurant-reported food_log entries
(quote-reply editable, domain='food').

Callback data:
  meal_ate:d:<slot>:<idx>      -> ONE main dish (by index among the slot's mains); slot -> 'ate' when all done
  meal_ate:s:<slot>:<staple>   -> that one home staple (logs independently; tracked in meta)
  meal_ate:m:<slot>            -> the whole slot's mains (legacy; no longer rendered, still handled)

Idempotency is DB-enforced inside claim_and_post (a dish already in meta.posted_mains, a staple already in
meta.posted_staples, or a slot already 'ate' -> outcome='already' -> "Already logged ✓" toast, no re-post).

Functions:
  parse_callback(data) -> (kind, slot, ref)   # pure; ref = dish index ('d') | staple name ('s') | None ('m')
  handle_meal_eaten(msg) -> list[tuple]       # the callback handler
"""

import logging

from domains.food.correction import _fetch_items
from domains.food.service import _build_item_results
from domains.health_agent.meal_planner import persistence
from system.logging import log_event, log_failure
from system.timezone import get_local_today
from telegram.replies import answer_callback_query, edit_message_reply_markup

logger = logging.getLogger(__name__)


# Parses a meal_ate callback. Pure. Returns (kind, slot, ref): kind 'd' (one dish, ref=int index),
# 's' (one staple, ref=name), or 'm' (whole slot, ref=None). Returns (None, None, None) if malformed.
def parse_callback(data: str) -> tuple:
    parts = (data or "").split(":")
    if len(parts) >= 3 and parts[0] == "meal_ate" and parts[1] in ("m", "s", "d"):
        kind, slot = parts[1], parts[2]
        if kind == "s":
            staple = ":".join(parts[3:]) if len(parts) > 3 else ""
            return (kind, slot, staple) if staple else (None, None, None)
        if kind == "d":                                  # the dish INDEX among the slot's mains
            return (kind, slot, int(parts[3])) if len(parts) > 3 and parts[3].isdigit() else (None, None, None)
        return kind, slot, None                          # 'm' (whole slot — legacy, still handled)
    return None, None, None


# Handles a `✓ Ate` tap: atomically post the item(s) to food_log + mark the slot, then reply with the
# SAME rich, quote-correctable food card(s) as normal food logging — one per posted item (reusing the food
# module's _fetch_items + _build_item_results, so format + editing are identical). Answers the callback with
# a toast in every branch. Input: the CALLBACK_QUERY InboundMessage. Output: [(card, food-state), …] or [].
def handle_meal_eaten(msg) -> list[tuple]:
    kind, slot, ref = parse_callback(msg.callback_data)
    if not kind:
        answer_callback_query(msg.callback_query_id)
        return []
    today, _tz = get_local_today()
    try:
        res = persistence.claim_and_post(today, slot, kind, ref, getattr(msg, "update_id", None))
    except Exception as e:
        log_failure(logger, logging.ERROR, "meal_eaten_failed", e, callback_data=msg.callback_data)
        answer_callback_query(msg.callback_query_id, text="Couldn't log that — try again")
        return []

    outcome = res["outcome"]
    if outcome == "already":
        answer_callback_query(msg.callback_query_id, text="Already logged ✓")
        return []
    if outcome in ("no_slot", "empty"):
        answer_callback_query(msg.callback_query_id, text="Nothing to log here")
        return []

    answer_callback_query(msg.callback_query_id, text="Logged ✓")
    # Buttons disappear on tap (BRIEF §6): re-render the card's keyboard from the post-tap state so the
    # just-tapped button is gone (and the keyboard clears once everything is logged). Best-effort.
    chat_id, card_id = getattr(msg, "chat_id", None), getattr(msg, "message_id", None)
    if chat_id and card_id:
        try:
            edit_message_reply_markup(chat_id, card_id,
                                      {"inline_keyboard": persistence.read_open_buttons(today)})
        except Exception as e:
            log_failure(logger, logging.WARNING, "meal_card_buttons_refresh_failed", e)
    log_event(logger, logging.INFO, "meal_eaten_logged", slot=slot, kind=kind, food_log_ids=res["ids"])
    # Confirm with the SAME rich, quote-correctable food card(s) as normal food logging — one per posted
    # item — by reusing the food module's loader + card builder. So a meal ✓ Ate reads + edits IDENTICALLY
    # to any food log (macros + "restaurant reported" chips + "Quote to correct."; domain='food' state with
    # food_log_ids = the just-tapped item + meal_food_log_ids = the WHOLE slot's logged items, so a
    # "this was actually dinner" correction moves the whole meal, not one dish).
    slot_ids = res.get("slot_ids") or res["ids"]
    items = _fetch_items(res["ids"])
    if not items:                       # rows are committed but the read-back came back empty (transient)
        names = ", ".join(i.get("name_en") or i.get("item_name") for i in res["items"]) or "your meal"
        return [(f"✓ Logged {names}",   # don't drop the confirmation — fall back to a terse, still-editable line
                 {"domain": "food", "context": {"food_log_ids": res["ids"],
                  "meal_food_log_ids": slot_ids, "meal_type": slot}})]
    return _build_item_results(items, res["ids"], slot, meal_food_log_ids=slot_ids)
