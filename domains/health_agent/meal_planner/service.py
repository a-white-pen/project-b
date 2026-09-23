"""
Orchestrates day-of meal planning for the plan button and scheduled job.

It reads logged food and reserved workout food, calculates the remaining target, filters the menu,
asks the model to choose items, validates the result, and renders the card.

Only open meal slots are planned. Days without a shop use own-food guidance, and Singapore mode uses
home-staple suggestions.

Functions:
  _fuel_item_label — formats one workout-food item
  _fuel_items — lists workout food for the day's activities
  _attach_name_en — adds English names to Thai dishes
  _wants_shop_change — detects a request to change shops
  _refit_week_shops — reassigns later shops after today's shop changes
  _try_swap_shop — finds an affordable alternative shop
  _gap_fill_shop_dishes — estimates missing values for selected dishes
  _gap_fill_staples — estimates missing values for selected staples
  plan_meals — builds the day's meal-planning result
  _interim — sends a temporary progress message
  _persist_planned — saves a planned meal result
  handle_meal — handles the meal-planning button
  _dish_tokens — normalizes dish names for matching
  _match_known — matches returned names to known menu items
  _learn_unavailable — records unavailable dishes from text or photos
  handle_meal_correction — rebuilds a meal plan from a quoted correction
  _send_meal — sends and registers a meal card
  run_meals — runs the scheduled daily meal flow
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from domains.food.service import _gap_fill_macros
from domains.health_agent.cards import register_card
from domains.health_agent.meal_planner import persistence, render, solver
from domains.health_agent.meal_planner import prompt as meal_prompt
from domains.health_agent.week_planner import meal_assign
from domains.health_agent.week_planner import persistence as week_persistence
from domains.health_agent.goals import (build_nutrition_target, fixed_intake_config, load_goals,
                                        mode_config, nutrition_config)
from system.llm import (MODEL_FLASH, MODEL_FLASH_LITE, generate_json,
                        generate_json_reasoning, generate_with_image, generate_with_images,
                        parse_json_response)
from system.logging import log_event, log_failure
from system.messages import MessageType
from system.text import is_thai as _is_thai
from system.timezone import get_local_today, get_timezone
from telegram.files import get_file_bytes
from telegram.replies import get_latest_chat_id, send_logged, send_reply

logger = logging.getLogger(__name__)

_MACROS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")
# Card states that should replace the current pinned meal card.
_PINNED_STATUSES = ("planned", "own_food", "all_eaten", "at_limit", "staple_topup")


# Display label for one fixed-intake fuel item, e.g. "banana ×2", "flaxseed 10g", "full cream milk".
def _fuel_item_label(it: dict) -> str:
    name = str(it.get("item", "")).replace("_", " ")
    if (it.get("qty") or 0) > 1:
        return f"{name} ×{it['qty']}"
    if it.get("qty_g"):
        return f"{name} {it['qty_g']}g"
    if it.get("qty_ml"):
        return f"{name} {it['qty_ml']}ml"
    return name


# Returns configured workout-food labels for the day's activities.
def _fuel_items(activity_type, fixed: dict) -> list[str]:
    at = activity_type or []
    items = []
    if "cardio" in at:
        items += (fixed.get("run_fuel") or [])
    if "strength" in at:
        items += (fixed.get("strength_fuel") or [])
    return [_fuel_item_label(it) for it in items]


_TRANSLATE_PROMPT = (
    "Translate each Thai dish name below to a short, natural English dish name. Return STRICT JSON only, "
    'mapping each EXACT input string to its English name: {"<thai>": "<english>"}. Names:\n'
)


# Adds English names to Thai main dishes for the meal card. Leaves the Thai name unchanged on failure.
def _attach_name_en(slots: dict) -> None:
    thai = sorted({i["item_name"] for items in (slots or {}).values() for i in (items or [])
                   if i.get("role") != "staple" and _is_thai(i.get("item_name"))})
    if not thai:
        return
    try:
        mapping = parse_json_response(generate_json(_TRANSLATE_PROMPT + "\n".join(thai), model=MODEL_FLASH_LITE))
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_translate_failed", e, names=len(thai))
        return
    for items in slots.values():
        for i in (items or []):
            if i.get("role") == "staple":
                continue
            en = mapping.get(i.get("item_name"))
            if isinstance(en, str) and en.strip():
                i["name_en"] = en.strip()


# Phrases that clearly request a different shop. Broad words such as "unavailable" are excluded to
# avoid treating a dish correction as a shop closure.
_SHOP_CHANGE_RE = re.compile(
    r"\b(closed|sold\s*out|not\s+open|can'?t\s+order|cannot\s+order|"
    r"(another|different|other|new)\s+(shop|place|store|restaurant|vendor)|"
    r"somewhere\s+else)\b",
    re.IGNORECASE,
)


# Returns whether correction text asks to change shops.
def _wants_shop_change(text: str) -> bool:
    return bool(_SHOP_CHANGE_RE.search(text or ""))


# Reassigns future shops after today's shop changes. Returns the number of updated days.
def _refit_week_shops(today, today_shop: str) -> int:
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    rows = week_persistence.read_week(monday, sunday, today)
    days = [{"date": r["date"], "is_vegetarian_day": bool(r.get("is_vegetarian_day")),
             "meal_plan_provider": (today_shop if r["date"] == today else r.get("meal_provider"))}
            for r in rows if monday <= r["date"] <= sunday]
    days.sort(key=lambda d: d["date"])
    if not days:
        return 0
    locked = {d["date"] for d in days if d["date"] <= today}
    before = {d["date"]: d["meal_plan_provider"] for d in days}
    mc = load_goals().get("meal_constraints", {})
    cap_thb = float(mc.get("budget_sgd_per_meal", 6.5)) * float(mc.get("fx_thb_per_sgd_planning", 25))
    pool = week_persistence.read_shop_pool(cap_thb)
    meal_assign.assign_shops(days, pool, mc, locked_dates=locked)
    # Only future days with a replacement shop are updated.
    changed = 0
    for d in days:
        new = d["meal_plan_provider"]
        if d["date"] > today and new and new != before.get(d["date"]):
            if persistence.update_meal_provider(d["date"], new):
                changed += 1
    if changed:
        log_event(logger, logging.INFO, "week_shops_refit", plan_date=str(today), changed=changed)
    return changed


# Finds an affordable alternative shop with usable dishes. Returns the old shop, new shop, and menu.
def _try_swap_shop(current_shop, remaining, unavailable: dict | None = None) -> tuple:
    unavailable = unavailable or {}
    mc = load_goals().get("meal_constraints", {})
    cap_thb = float(mc.get("budget_sgd_per_meal", 6.5)) * float(mc.get("fx_thb_per_sgd_planning", 25))
    try:
        pool = week_persistence.read_shop_pool(cap_thb)
    except Exception as e:
        log_failure(logger, logging.WARNING, "shop_swap_pool_failed", e)
        return None, current_shop, []
    for alt in pool:
        name = alt["name"]
        if name == current_shop or not alt.get("affordable") or alt.get("is_grain"):
            continue
        alt_palette = solver.filter_menu(persistence.read_menu(name), remaining,
                                         exclude_names=unavailable.get(name))
        if alt_palette:
            log_event(logger, logging.INFO, "meal_shop_swapped", from_shop=current_shop, to_shop=name)
            return current_shop, name, alt_palette
    return None, current_shop, []


# Estimates missing nutrition values for selected shop dishes before daily totals are calculated.
def _gap_fill_shop_dishes(proposed_slots: dict, palette: list) -> None:
    by_name = {d.get("item_name"): d for d in (palette or [])}
    picked = {it.get("item_name") for s in (proposed_slots or {}).values() for it in (s or [])}
    for name in picked:
        d = by_name.get(name)
        if d is None:
            continue
        d["food_item"] = name
        try:
            _gap_fill_macros(d, None)
        except Exception as e:
            log_failure(logger, logging.WARNING, "meal_shop_gap_fill_failed", e, item=name)
        finally:
            d.pop("food_item", None)


# Estimates missing sugar and sodium for selected home staples.
def _gap_fill_staples(slots: dict) -> None:
    for items in (slots or {}).values():
        for it in (items or []):
            if it.get("role") != "staple":
                continue
            it["food_item"] = it.get("item_name")
            try:
                _gap_fill_macros(it, None)
            except Exception as e:
                log_failure(logger, logging.WARNING, "meal_staple_gap_fill_failed", e,
                            item=it.get("item_name"))
            finally:
                it.pop("food_item", None)


# Plans open lunch and dinner slots. Returns a status plus the data needed to render and save the result.
def plan_meals(plan_date, tz_name: str, notify=None, correction: str | None = None,
               model: str = MODEL_FLASH, avoid_current_shop: bool = False,
               keep_available: bool = False) -> dict:
    # A shop closure forces a swap. A dish-only correction keeps unaffected meal slots.
    inp = persistence.read_day_inputs(plan_date, tz_name)
    cfg = nutrition_config()
    staples = load_goals().get("meal_constraints", {}).get("home_staples", {})

    fixed = fixed_intake_config()
    logged_markers = solver.logged_fuel_markers(inp.get("food_items"), fixed)
    reserved = solver.reserved_fuel(inp["activity_type"], logged_markers, fixed)

    taken = solver.taken_slots(inp["meal_rows"], inp["logged_meal_types"])
    uneaten = [s for s in ("lunch", "dinner") if s not in taken]

    # Keep open slots whose current dishes remain available.
    keep_slots: dict = {}
    if keep_available and inp["shop"] and not avoid_current_shop:
        keep_slots = solver.slots_to_keep(
            inp["shop"], inp.get("unavailable_items"), persistence.read_planned_slots(plan_date), uneaten)

    # Include logged food, reserved workout food, and kept slots in the current daily totals.
    consumed = {m: (inp["consumed"].get(m) or 0) + (reserved.get(m) or 0) for m in _MACROS}
    for items in keep_slots.values():
        for it in items:
            for m in _MACROS:
                consumed[m] += it.get(m) or 0

    # Uses the stored daily target or the standard target when no daily plan exists.
    target = inp["macro_target"] or build_nutrition_target(cfg)
    remaining = solver.compute_remaining(target, consumed)

    # In Singapore, suggest home staples for the remaining protein within the calorie limit.
    if not mode_config().get("b_extended_plans_meals", True):
        topup = solver.suggest_staples(remaining, staples)
        projected = {m: round((consumed.get(m) or 0) + (topup["added"].get(m) or 0)) for m in _MACROS}
        log_event(logger, logging.INFO, "meal_staple_topup", plan_date=str(plan_date),
                  staples=[s["item_name"] for s in topup["staples"]])
        return {"status": "staple_topup", "remaining": remaining, "topup": topup["staples"],
                "topup_added": topup["added"], "projected": projected, "report": [],
                "eaten": inp["consumed"], "eaten_by_meal": inp["eaten_by_meal"], "reserved": reserved,
                "target_macros": target,
                "fuel_items": _fuel_items(inp["activity_type"], fixed),
                "workout_label": "Strength food" if "strength" in (inp["activity_type"] or []) else "Run food"}

    # Read weekly and two-week protein tallies for the rotation settings.
    monday = plan_date - timedelta(days=plan_date.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    rotation = load_goals().get("meal_constraints", {}).get("protein_rotation", {})
    week_tally = persistence.read_protein_tally(monday, sunday, tz_name)
    fortnight_tally = persistence.read_protein_tally(monday - timedelta(days=7), sunday, tz_name)
    owed = solver.owed_proteins_split(week_tally, fortnight_tally, rotation)

    slots_to_plan = [s for s in uneaten if s not in keep_slots]
    base = {"shop": inp["shop"], "remaining": remaining, "slots_to_plan": slots_to_plan,
            "staples": staples, "owed": owed, "consumed_kcal": inp["consumed"].get("kcal"),
            "target_kcal": (target.get("kcal") or {}).get("target"),
            "report": [],
            # Keep the original ranges for the meal card.
            "eaten": inp["consumed"], "eaten_by_meal": inp["eaten_by_meal"], "reserved": reserved,
            "target_macros": target,
            "fuel_items": _fuel_items(inp["activity_type"], fixed),
            "workout_label": "Strength food" if "strength" in (inp["activity_type"] or []) else "Run food"}

    if not slots_to_plan:
        if keep_slots:
            projected = {m: round(consumed.get(m) or 0) for m in _MACROS}
            log_event(logger, logging.INFO, "meal_plan_kept_all", plan_date=str(plan_date))
            return {**base, "status": "planned", "slots": dict(keep_slots), "projected": projected,
                    "note": None}
        log_event(logger, logging.INFO, "meal_plan_all_eaten", plan_date=str(plan_date))
        return {**base, "status": "all_eaten"}
    if not inp["shop"]:
        log_event(logger, logging.INFO, "meal_plan_own_food", plan_date=str(plan_date))
        return {**base, "status": "own_food"}

    # Remove dishes recorded as unavailable today.
    unavailable = inp.get("unavailable_items") or {}
    # A reported shop closure forces the alternative-shop path.
    palette = [] if avoid_current_shop else solver.filter_menu(
        persistence.read_menu(inp["shop"]), remaining, exclude_names=unavailable.get(inp["shop"]))
    # Try another shop when the assigned one has no usable choices.
    if not palette and remaining["kcal"]["high"] > 0:
        swapped_from, new_shop, palette = _try_swap_shop(inp["shop"], remaining, unavailable)
        if swapped_from:
            base["shop"] = new_shop
            base["shop_swapped"] = True
            why = "closed" if avoid_current_shop else "couldn't fit today"
            base["report"] = base["report"] + [f"moved you off {swapped_from} ({why}) → {new_shop}"]
    # Mark an unresolved shop closure so the card can explain why no meal was planned.
    if avoid_current_shop and not base.get("shop_swapped"):
        base["shop_unavailable"] = True
        base["report"] = base["report"] + [f"{inp['shop']} closed — no other shop fits today"]
    if remaining["kcal"]["high"] <= 0 or not palette:
        log_event(logger, logging.INFO, "meal_plan_at_limit", plan_date=str(plan_date),
                  kcal_left=remaining["kcal"]["high"], palette=len(palette))
        return {**base, "status": "at_limit"}

    # After a shop swap, compose from the replacement menu without the closure instruction.
    compose_correction = None if avoid_current_shop else correction
    state = {"remaining": remaining, "slots_to_plan": slots_to_plan, "palette": palette,
             "staples": staples, "is_vegetarian_day": inp["is_vegetarian_day"], "protein_owed": owed,
             # Include both tally windows so two-week rotation settings use the correct period.
             "protein_rotation": rotation, "protein_tally": week_tally,
             "protein_tally_2wk": {p: fortnight_tally.get(p, 0)
                                   for p, s in rotation.items() if "2wk" in str(s)},
             "correction": compose_correction}
    if notify:
        notify()
    try:
        parsed = parse_json_response(generate_json_reasoning(meal_prompt.build_meal_prompt(state), model=model))
        # Estimate missing values before calculating the daily totals.
        _gap_fill_shop_dishes(parsed.get("slots", {}), palette)
        result = solver.finalize_meals(parsed.get("slots", {}), palette, staples, consumed, target)
        # Sugar and sodium do not affect the daily target calculation.
        _gap_fill_staples(result["slots"])
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_compose_failed", e, plan_date=str(plan_date))
        return {**base, "status": "compose_failed"}

    if not any(result["slots"].values()):
        log_event(logger, logging.INFO, "meal_compose_empty", plan_date=str(plan_date))
        return {**base, "status": "compose_failed"}
    _attach_name_en(result["slots"])
    result["slots"].update(keep_slots)
    log_event(logger, logging.INFO, "meal_planned", plan_date=str(plan_date), shop=inp["shop"],
              slots=list(result["slots"].keys()), kept=list(keep_slots.keys()), bent=len(result["report"]))
    return {**base, "status": "planned", "slots": result["slots"], "projected": result["projected"],
            "note": parsed.get("note"), "report": result["report"]}


# Sends a short progress message while meal composition runs.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if chat_id:
        try:
            send_reply(chat_id, text)
        except Exception as e:
            log_failure(logger, logging.WARNING, "meal_interim_failed", e)


# Saves planned meal slots and any replacement shop. Write failures are logged without losing the card.
def _persist_planned(today, result: dict, source: str) -> None:
    try:
        persistence.save_meal_plan(today, result["slots"], meta={"source": source, "as_of": str(today)})
        if result.get("shop_swapped"):
            persistence.update_meal_provider(today, result["shop"])
            # Rebalance future shops without undoing today's plan if it fails.
            try:
                result["week_refit"] = _refit_week_shops(today, result["shop"])
            except Exception as e:
                log_failure(logger, logging.WARNING, "week_refit_failed", e, plan_date=str(today))
    except Exception as e:
        log_failure(logger, logging.ERROR, "meal_save_failed", e, plan_date=str(today))


# Handles the meal-plan button, saves a planned result, sends the card, and returns no reply bubbles.
def handle_meal(msg) -> list[tuple]:
    today, _tz = get_local_today()
    result = plan_meals(today, _tz, notify=lambda: _interim(msg, "🍽️ Putting your meal together — ~20s…"))
    if result.get("status") == "planned":
        _persist_planned(today, result, "plan_meals")
    text, markup = render.render_meal_card(result)
    _send_meal(msg, today, text, markup,
               pin=result.get("status") in _PINNED_STATUSES,
               correctable=result.get("status") == "planned")
    return []


# Prompt for matching unavailable dishes to the shop's known menu.
_UNAVAIL_TEXT_PROMPT = (
    "B is correcting today's meal plan for the shop \"{shop}\". Its known menu items are:\n{names}\n\n"
    "B said: \"{text}\"\n\n"
    "Which of those menu items is B saying are NOT available today (sold out / not on the menu / "
    "\"cannot find\" / \"they don't have\")? Return STRICT JSON: {{\"unavailable\": [<exact item_name "
    "strings copied from the list>]}}. If B is not talking about availability at all, return "
    '{{"unavailable": []}}. Never invent a name.'
)
_UNAVAIL_PHOTO_PROMPT = (
    "These photo(s) show shop \"{shop}\"'s actual menu / order board today (there may be SEVERAL photos "
    "covering different sections of ONE menu — read them together). The shop's known menu items are:"
    "\n{names}\n\n"
    "Match every dish visible across the photo(s) to the CLOSEST item in that list, IGNORING category "
    "prefixes the board adds ('New Salad -', 'Grain Bowl -', 'Pasta -', 'Rice -') and minor wording "
    "differences (e.g. board 'Grain Bowl - Morrocan Rice & Cajun' = list item 'Morrocan Rice & Cajun "
    "Chicken'). Return STRICT JSON: {{\"menu_complete\": <true only if the photo(s) TOGETHER show the "
    "shop's FULL menu/board, false for a partial/unclear shot>, \"available\": [<the matching item_name "
    "strings FROM THE LIST that are on the board>], \"unavailable\": [<item_name strings from the list "
    "clearly sold out / crossed out>]}}. Use ONLY names copied from the list; never invent. If a board "
    "dish has no match in the list, omit it."
)

# Menu-board category prefixes removed before dish matching.
_MENU_CATEGORY_PREFIX = re.compile(
    r"^(new\s+)?(salad|pasta|grain\s+bowl|grain|bowl|rice(berry)?|noodles|set|combo)\s*[-–:]\s*",
    re.IGNORECASE)
_DISH_STOP = frozenset({"the", "with", "and", "a", "of", "in", "on", "set", "combo", "new"})


# Returns the significant words used to match a menu-board dish to a stored dish.
def _dish_tokens(s) -> set:
    stripped = _MENU_CATEGORY_PREFIX.sub("", str(s or "").strip().lower())
    toks = re.findall(r"[a-z0-9]+", stripped)
    return {t for t in toks if t not in _DISH_STOP and len(t) > 1}


# Matches returned dish names to real menu names. Uses an exact match first, then a conservative word match.
def _match_known(returned, names: list[str]) -> set:
    by_lower = {n.strip().lower(): n for n in names}
    name_tok = [(n, _dish_tokens(n)) for n in names]
    out: set = set()
    for r in returned or []:
        rl = str(r).strip().lower()
        if rl in by_lower:
            out.add(by_lower[rl])
            continue
        rt = _dish_tokens(r)
        if len(rt) < 2:
            continue
        for n, nt in name_tok:
            if len(nt) < 2:
                continue
            small, big = (rt, nt) if len(rt) <= len(nt) else (nt, rt)
            if small <= big:
                out.add(n)
                break
    return out


# Finds unavailable dishes from correction text or menu photos and records them for the day.
def _learn_unavailable(msg, today, tz_name: str, text: str, is_photo: bool) -> int:
    try:
        shop = persistence.read_day_inputs(today, tz_name).get("shop")
        if not shop:
            return 0
        names = [d["item_name"] for d in persistence.read_menu(shop) if d.get("item_name")]
        if not names:
            return 0
        listed = "\n".join(f"- {n}" for n in names)
        unavailable: set = set()
        # Read all menu-board photos together so separate sections form one menu.
        file_ids = list(getattr(msg, "media_group_file_ids", None) or [])
        if not file_ids and getattr(msg, "file_id", None):
            file_ids = [msg.file_id]
        if is_photo and file_ids:
            try:
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                images = [get_file_bytes(fid, token) for fid in file_ids[:4]]
                prompt = _UNAVAIL_PHOTO_PROMPT.format(shop=shop, names=listed)
                raw = (generate_with_images(images, prompt, model=MODEL_FLASH) if len(images) > 1
                       else generate_with_image(images[0], prompt, model=MODEL_FLASH))
                parsed = parse_json_response(raw)
                unavailable |= _match_known(parsed.get("unavailable"), names)
                available = _match_known(parsed.get("available"), names)
                # Trust a complete board only after enough dishes match the stored menu.
                if parsed.get("menu_complete") and len(available) >= 3:
                    unavailable |= set(names) - available - unavailable
                log_event(logger, logging.INFO, "meal_menu_photo_read", plan_date=str(today), shop=shop,
                          photos=len(images), available=len(available), menu_complete=bool(parsed.get("menu_complete")))
            except Exception as e:
                log_failure(logger, logging.WARNING, "meal_unavailable_photo_failed", e, plan_date=str(today))
        if text:
            try:
                parsed = parse_json_response(generate_json(
                    _UNAVAIL_TEXT_PROMPT.format(shop=shop, names=listed, text=text), model=MODEL_FLASH))
                unavailable |= _match_known(parsed.get("unavailable"), names)
            except Exception as e:
                log_failure(logger, logging.WARNING, "meal_unavailable_text_failed", e, plan_date=str(today))
        if unavailable:
            return persistence.add_unavailable_items(today, shop, sorted(unavailable))
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_learn_unavailable_failed", e, plan_date=str(today))
    return 0


# Handles a quoted meal correction, keeps completed slots, saves the replacement plan, and resends it.
def handle_meal_correction(msg, state: dict) -> list[tuple]:
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    is_photo = getattr(msg, "message_type", None) == MessageType.PHOTO and bool(
        getattr(msg, "file_id", None) or getattr(msg, "media_group_file_ids", None))
    if not text and not is_photo:
        return [("✏️ Tell me what to change — a note, or a photo of the shop's menu today.", None)]
    today, _tz = get_local_today()
    # Record unavailable dishes before rebuilding the plan.
    _learn_unavailable(msg, today, _tz, text, is_photo)
    # A reported closure forces a shop swap even when stored menu data still exists.
    avoid = _wants_shop_change(text)
    # Keep unaffected slots for a dish-only correction; replace all slots after a shop change.
    result = plan_meals(today, _tz, notify=lambda: _interim(msg, "🍽️ Re-working your meal…"),
                        correction=(text or None), model=MODEL_FLASH, avoid_current_shop=avoid,
                        keep_available=not avoid)
    if result.get("status") == "planned":
        _persist_planned(today, result, "meal_correction")
    card, markup = render.render_meal_card(result)
    _send_meal(msg, today, card, markup,
               pin=result.get("status") in _PINNED_STATUSES,
               correctable=result.get("status") == "planned")
    return []


# Sends the meal card and records its pin and correction context when applicable.
def _send_meal(msg, today, text: str, markup, pin: bool, correctable: bool) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if not chat_id:
        return
    message_id = send_logged(chat_id, text, reply_markup=markup)
    if message_id is None:
        return
    update_id = getattr(msg, "update_id", None)
    register_card(chat_id, message_id, pin_kind="meal" if pin else None,
                  update_id=update_id if correctable else None,
                  context={"kind": "meal", "plan_date": str(today)} if correctable else None,
                  plan_date=str(today))


# Sweeps yesterday's open meals, plans today, sends the scheduled card, and returns its text.
def run_meals(now_utc=None) -> str | None:
    now_utc = now_utc or datetime.now(timezone.utc)
    tz = get_timezone(now_utc)
    today = now_utc.astimezone(tz).date()
    yesterday = today - timedelta(days=1)
    try:
        persistence.sweep_meals(yesterday)
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_sweep_failed", e, plan_date=str(yesterday))

    result = plan_meals(today, str(tz))
    if result.get("status") == "planned":
        _persist_planned(today, result, "meal_cron")

    text, markup = render.render_meal_card(result)
    chat_id = get_latest_chat_id()
    if not chat_id:
        log_event(logger, logging.WARNING, "meals_cron_no_chat_id", plan_date=str(today))
        return None
    message_id = send_logged(chat_id, text, reply_markup=markup)
    if message_id is not None and result.get("status") in _PINNED_STATUSES:
        register_card(chat_id, message_id, pin_kind="meal", plan_date=str(today))
    log_event(logger, logging.INFO, "meals_cron_completed", plan_date=str(today),
              status=result.get("status"))
    return text
