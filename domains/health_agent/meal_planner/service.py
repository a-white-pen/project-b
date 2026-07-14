"""
Day-of meal planner orchestration (BRIEF §6 11am / `/plan` -> 🍽️ Meal). Ties the deterministic solver
to the LLM compose:

  read day inputs -> consumed (logged + reserved workout fuel) -> remaining budget -> which slots are
  still un-eaten -> filter the shop menu to the fitting palette -> Gemini Flash composes the un-eaten
  slots from palette + staples -> code finalizes (validate/caps/check).

Short-circuits (no shop card): all slots already eaten -> 'all_eaten'; no shop assigned (own-food/
weekend) -> 'own_food' (the /suggest_food guide, spec G); budget exhausted or nothing fits ->
'at_limit' (skip or a light staple — B 2026-06-25). Plans ONLY un-eaten slots, never a slot with a
record. NOT unit-tested here (DB + LLM); the solver it calls is pure + tested; reviewed adversarially.

Functions:
  plan_meals(plan_date, tz_name) -> dict   # the day's meal result (status + slots/remaining/report)
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from domains.food.service import _gap_fill_macros
from domains.health_agent import macros
from domains.health_agent.cards import register_card
from domains.health_agent.meal_planner import persistence, render, solver
from domains.health_agent.meal_planner import prompt as meal_prompt
from domains.health_agent.week_planner import meal_assign
from domains.health_agent.week_planner import persistence as week_persistence
from domains.health_agent.goals import fixed_intake_config, load_goals, nutrition_config
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
# Statuses whose card is pinned kind='meal' (each carries the "your day" table): a real order, the
# own-food guide, and the all-eaten / at-limit end-of-day summaries. compose_failed (an error) is NOT
# pinned. Only 'planned' is quote-correctable (nothing to re-plan once eaten / at limit).
_PINNED_STATUSES = ("planned", "own_food", "all_eaten", "at_limit")


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


# The day's configured workout-fuel items for the card's single "Workout fuel" line (B's choice 2026-06):
# run_fuel on cardio days, strength_fuel on strength days, from goals.yaml fixed_intake.
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


# Best-effort: translate Thai-script MAIN dish names -> English (name_en) for the Order Card's second copy
# box, using the CHEAPEST model (Flash-Lite). Mutates slots in place. Skips staples + already-English
# names; makes ONE call only when there's at least one Thai main. Any failure leaves name_en absent, so
# the card simply shows the Thai name alone (graceful). generate_json already retries on transient errors.
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


# True when a meal correction signals the assigned shop is unavailable and B wants a different one
# ("closed", "sold out", "shut", "order from another/different/other shop", "somewhere else", "can't
# order from …"). Keyword-based (fast, no extra LLM); a miss just means no forced swap (prior behaviour).
# Strong closure phrases (rarely appear in a dish tweak) + compound "…another/different/other/new SHOP".
# Deliberately NOT bare "shut"/"unavailable"/"closing" — those show up in carb-cut phrasings ("shut off the
# rice", "unavailable at home") and would wrongly force a whole-shop swap. A miss just means no swap.
_SHOP_CHANGE_RE = re.compile(
    r"\b(closed|sold\s*out|not\s+open|can'?t\s+order|cannot\s+order|"
    r"(another|different|other|new)\s+(shop|place|store|restaurant|vendor)|"
    r"somewhere\s+else)\b",
    re.IGNORECASE,
)


def _wants_shop_change(text: str) -> bool:
    return bool(_SHOP_CHANGE_RE.search(text or ""))


# After a day-of shop SWAP, re-balance THIS week's FUTURE order-days so Grain/Jones/variety still hold
# across the week (B 2026-07-01: "refit the whole week"). Past days + today are LOCKED (today = the swapped
# shop); only future days are re-assigned + persisted to daily_plan. Deterministic (no LLM). Best-effort —
# any failure leaves today's swap intact. Returns the count of future days whose shop changed (for the note).
def _refit_week_shops(today, today_shop: str) -> int:
    monday = today - timedelta(days=today.isoweekday() - 1)
    sunday = monday + timedelta(days=6)
    rows = week_persistence.read_week(monday, sunday, today)
    days = [{"date": r["date"], "is_vegetarian_day": bool(r.get("is_vegetarian_day")),
             "meal_plan_provider": (today_shop if r["date"] == today else r.get("meal_provider"))}
            for r in rows if monday <= r["date"] <= sunday]
    days.sort(key=lambda d: d["date"])                 # date order (adjacency/variety depends on it)
    if not days:
        return 0
    locked = {d["date"] for d in days if d["date"] <= today}      # past + today are fixed
    before = {d["date"]: d["meal_plan_provider"] for d in days}
    mc = load_goals().get("meal_constraints", {})
    cap_thb = float(mc.get("budget_sgd_per_meal", 6.5)) * float(mc.get("fx_thb_per_sgd_planning", 25))
    pool = week_persistence.read_shop_pool(cap_thb)
    meal_assign.assign_shops(days, pool, mc, locked_dates=locked)
    # Repoints FUTURE days only; a day that refits to None is left as-is (update_meal_provider can't clear).
    # Count only rows that ACTUALLY updated (a future day with no daily_plan row updates 0 rows) so the
    # "N days updated" note is truthful.
    changed = 0
    for d in days:
        new = d["meal_plan_provider"]
        if d["date"] > today and new and new != before.get(d["date"]):
            if persistence.update_meal_provider(d["date"], new):
                changed += 1
    if changed:
        log_event(logger, logging.INFO, "week_shops_refit", plan_date=str(today), changed=changed)
    return changed


# Finds an alternative shop when the assigned one can't fit today (BRIEF §6). Returns (swapped_from,
# new_shop, palette) on success, else (None, current_shop, []). Avoids the Grain shop to respect its
# weekly cap (a fuller swap could allow Grain when under-cap). unavailable = {shop: [sold-out dishes]}
# so an alt shop's own flagged-unavailable dishes are excluded from its palette too. Best-effort.
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


# Best-effort: estimate the PICKED shop dishes' missing macros (anchored to the menu's kcal) so sparse
# menus — e.g. Jones Salad, which publishes kcal only — don't log/show 0 for P/C/F/fibre/sugar/sodium.
# Mutates the palette dicts in place BEFORE finalize_meals, so the day projection + floor warnings use the
# estimates and finalize copies the filled macros + gap-fill provenance onto the plan items. Reuses the
# food module's _gap_fill_macros (same estimator as normal food logging). A per-dish failure is swallowed —
# that dish just keeps its menu values (0s) rather than failing the whole compose.
def _gap_fill_shop_dishes(proposed_slots: dict, palette: list) -> None:
    by_name = {d.get("item_name"): d for d in (palette or [])}
    picked = {it.get("item_name") for s in (proposed_slots or {}).values() for it in (s or [])}
    for name in picked:
        d = by_name.get(name)
        if d is None:                            # a staple or a hallucinated item — not a shop dish
            continue
        d["food_item"] = name                    # _gap_fill_macros anchors its prompt on this
        try:
            _gap_fill_macros(d, None)
        except Exception as e:
            log_failure(logger, logging.WARNING, "meal_shop_gap_fill_failed", e, item=name)
        finally:
            d.pop("food_item", None)             # temp anchor — finalize doesn't copy it; keep the dict clean


# Best-effort: fill the chosen staples' missing sugar/sodium (the home_staples config carries kcal/P/C/F/
# fibre but not those). Runs AFTER finalize (staples are materialised there) and only touches sugar/sodium,
# which aren't in the day projection — so no re-projection is needed. Per-staple failure is swallowed.
def _gap_fill_staples(slots: dict) -> None:
    for items in (slots or {}).values():
        for it in (items or []):
            if it.get("role") != "staple":
                continue
            it["food_item"] = it.get("item_name")   # _gap_fill_macros anchors on this
            try:
                _gap_fill_macros(it, None)
            except Exception as e:
                log_failure(logger, logging.WARNING, "meal_staple_gap_fill_failed", e,
                            item=it.get("item_name"))
            finally:
                it.pop("food_item", None)            # don't persist the temp anchor into meal_plan.items


# Plans the day's un-eaten meal slots. Output dict always has: status, shop, remaining, report. When
# status=='planned' it also has slots {slot:[items]}, projected {macros}, note. The card/persist layer
# (next module) renders + writes from this. status in:
#   planned     -> a shop card to render + meal_plan rows to write
#   all_eaten   -> both slots already recorded; nothing to do
#   own_food    -> no shop today -> /suggest_food guidance (spec G)
#   at_limit    -> no budget / nothing fits -> suggest skip or a light staple
#   compose_failed -> the compose LLM call failed (caller falls back to guidance)
def plan_meals(plan_date, tz_name: str, notify=None, correction: str | None = None,
               model: str = MODEL_FLASH, avoid_current_shop: bool = False,
               keep_available: bool = False) -> dict:
    # avoid_current_shop: B told us the assigned shop is closed/sold out -> skip it and swap to another
    # (set by handle_meal_correction when the reply signals a shop change). The menu-empty auto-swap can't
    # catch this, because the closed shop still HAS menu data — only B knows it's shut today.
    # keep_available: a correction that only flags SPECIFIC dishes as unavailable -> KEEP the un-eaten
    # slots whose dishes are all still available and re-pick ONLY the flagged/empty slot(s) (B 2026-07-01:
    # "if I only say specific items missing, keep what I didn't say is unavailable — it's implicitly available").
    inp = persistence.read_day_inputs(plan_date, tz_name)
    cfg = nutrition_config()
    staples = load_goals().get("meal_constraints", {}).get("home_staples", {})

    fixed = fixed_intake_config()
    logged_markers = solver.logged_fuel_markers(inp.get("food_items"), fixed)
    reserved = solver.reserved_fuel(inp["activity_type"], logged_markers, fixed)

    taken = solver.taken_slots(inp["meal_rows"], inp["logged_meal_types"])
    uneaten = [s for s in ("lunch", "dinner") if s not in taken]

    # Which un-eaten slots to KEEP as-is (correction preserve): a slot whose CURRENT planned dishes are all
    # still available (none in today's unavailable set). Only the flagged/empty slot(s) get re-picked.
    keep_slots: dict = {}
    if keep_available and inp["shop"] and not avoid_current_shop:
        keep_slots = solver.slots_to_keep(
            inp["shop"], inp.get("unavailable_items"), persistence.read_planned_slots(plan_date), uneaten)

    # consumed = logged so far + the day's reserved workout fuel (eaten ~1pm, not logged at 11am) + any
    # KEPT slot, so the re-pick balances the DAY around what we're keeping. Fuel B has ALREADY logged is
    # detected by marker and NOT re-reserved (so a post-workout re-run doesn't double-count it).
    consumed = {m: (inp["consumed"].get(m) or 0) + (reserved.get(m) or 0) for m in _MACROS}
    for items in keep_slots.values():
        for it in items:
            for m in _MACROS:
                consumed[m] += it.get(m) or 0

    # target: the scaffold's day target, or a rest-day fallback off the seed if the day was never planned.
    target = inp["macro_target"] or macros.build_macro_target(
        int(cfg["seed_maintenance"]) - int(cfg["DEFICIT"]), "rest", None, cfg)
    remaining = solver.compute_remaining(target, consumed)

    # Protein rotation state (BRIEF §5): weekly proteins are judged Mon-Sun; a ">=1 per 2wk" spec (duck)
    # over LAST Monday..this Sunday — the same split the weekly reflection uses, so duck eaten last week
    # doesn't re-surface as owed. BOTH tallies also go to the compose so it steers AWAY from a protein
    # whose minimum is already met (B 2026-07-02: "had fish yesterday — stop prioritising fish").
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
            "day_type": target.get("day_type"), "report": [],
            # Macro breakdown for the Order Card / suggest "📊 Your day" table (Message Workbook redesign):
            # eaten = logged so far; reserved = the day's workout fuel; target_macros = the RAW range
            # structure ({kcal:{low,target,high}, protein_g:{low,high}, fat_g:{min}, ...}) — the render
            # (_day_block) shows the range on the Target line + derives the floor for still-to-eat.
            "eaten": inp["consumed"], "eaten_by_meal": inp["eaten_by_meal"], "reserved": reserved,
            "target_macros": target,
            "fuel_items": _fuel_items(inp["activity_type"], fixed),
            "workout_label": "Strength food" if "strength" in (inp["activity_type"] or []) else "Run food"}

    if not slots_to_plan:
        if keep_slots:   # nothing B flagged is on today's plan -> keep the (unchanged) plan, re-pick nothing
            projected = {m: round(consumed.get(m) or 0) for m in _MACROS}   # consumed already folds keep
            log_event(logger, logging.INFO, "meal_plan_kept_all", plan_date=str(plan_date))
            return {**base, "status": "planned", "slots": dict(keep_slots), "projected": projected,
                    "note": None}
        log_event(logger, logging.INFO, "meal_plan_all_eaten", plan_date=str(plan_date))
        return {**base, "status": "all_eaten"}
    if not inp["shop"]:
        log_event(logger, logging.INFO, "meal_plan_own_food", plan_date=str(plan_date))
        return {**base, "status": "own_food"}

    # Dishes B flagged as sold-out/unavailable today, per shop (daily_plan.unavailable_items) — dropped
    # from the palette deterministically so a corrected-away dish is never re-offered on any re-run today.
    unavailable = inp.get("unavailable_items") or {}
    # avoid_current_shop -> B said the shop is closed: force an empty palette so the swap path fires even
    # though the shop still has menu data. Otherwise read the assigned shop's menu normally.
    palette = [] if avoid_current_shop else solver.filter_menu(
        persistence.read_menu(inp["shop"]), remaining, exclude_names=unavailable.get(inp["shop"]))
    # Assigned shop can't supply a fitting palette (sold out / closed / no fit) but there's still budget ->
    # swap to an alternative shop (BRIEF §6). shop_swapped repoints daily_plan so the reconciler matches.
    if not palette and remaining["kcal"]["high"] > 0:
        swapped_from, new_shop, palette = _try_swap_shop(inp["shop"], remaining, unavailable)
        if swapped_from:
            base["shop"] = new_shop
            base["shop_swapped"] = True   # persist new_shop to daily_plan so the expense reconciler matches
            why = "closed" if avoid_current_shop else "couldn't fit today"
            base["report"] = base["report"] + [f"moved you off {swapped_from} ({why}) → {new_shop}"]
    # Closed shop we couldn't re-home (no alt fits, OR no budget left to order at all): flag it so the
    # at_limit card says WHY (else B gets a misleading "you're at budget" with no hint the swap failed).
    if avoid_current_shop and not base.get("shop_swapped"):
        base["shop_unavailable"] = True
        base["report"] = base["report"] + [f"{inp['shop']} closed — no other shop fits today"]
    if remaining["kcal"]["high"] <= 0 or not palette:
        log_event(logger, logging.INFO, "meal_plan_at_limit", plan_date=str(plan_date),
                  kcal_left=remaining["kcal"]["high"], palette=len(palette))
        return {**base, "status": "at_limit"}

    # On a shop-closed swap, DON'T feed the "…is closed, order elsewhere" text to the compose LLM — the
    # swap already actioned it, and passing "closed" is exactly what made the model decline all dishes.
    # Compose fresh from the new shop instead. (A dish preference tucked into a closure message is lost;
    # rare — B can follow up.)
    compose_correction = None if avoid_current_shop else correction
    state = {"remaining": remaining, "slots_to_plan": slots_to_plan, "palette": palette,
             "staples": staples, "is_vegetarian_day": inp["is_vegetarian_day"], "protein_owed": owed,
             # The rotation spec + what B has ALREADY eaten (week; fortnight for the "per 2wk" proteins)
             # — lets the compose steer AWAY from a met protein, not just toward the owed ones.
             "protein_rotation": rotation, "protein_tally": week_tally,
             "protein_tally_2wk": {p: fortnight_tally.get(p, 0)
                                   for p, s in rotation.items() if "2wk" in str(s)},
             "correction": compose_correction}
    if notify:                               # only now (about to hit the LLM) — not on the fast paths
        notify()
    try:
        parsed = parse_json_response(generate_json_reasoning(meal_prompt.build_meal_prompt(state), model=model))
        # Gap-fill the PICKED shop dishes BEFORE finalizing, so the day projection + floor warnings use
        # estimated macros for sparse menus (e.g. Jones = kcal-only) instead of zeros. Anchored to kcal.
        _gap_fill_shop_dishes(parsed.get("slots", {}), palette)
        result = solver.finalize_meals(parsed.get("slots", {}), palette, staples, consumed, target)
        # Staples carry config kcal/P/C/F/fibre but no sugar/sodium; fill those AFTER finalizing (they're
        # not in the day projection, so no re-projection is needed).
        _gap_fill_staples(result["slots"])
    except Exception as e:
        log_failure(logger, logging.WARNING, "meal_compose_failed", e, plan_date=str(plan_date))
        return {**base, "status": "compose_failed"}

    if not any(result["slots"].values()):   # LLM picked only invalid items -> nothing composed
        log_event(logger, logging.INFO, "meal_compose_empty", plan_date=str(plan_date))
        return {**base, "status": "compose_failed"}
    _attach_name_en(result["slots"])         # cheap Flash-Lite Thai->English for the card's 2nd copy box
    result["slots"].update(keep_slots)       # show + persist the untouched slot(s) beside the re-picked one(s)
    log_event(logger, logging.INFO, "meal_planned", plan_date=str(plan_date), shop=inp["shop"],
              slots=list(result["slots"].keys()), kept=list(keep_slots.keys()), bent=len(result["report"]))
    return {**base, "status": "planned", "slots": result["slots"], "projected": result["projected"],
            "note": parsed.get("note"), "report": result["report"]}


# Instant interim message (best-effort) — the Flash compose takes ~10-20s.
def _interim(msg, text: str) -> None:
    chat_id = getattr(msg, "chat_id", None)
    if chat_id:
        try:
            send_reply(chat_id, text)
        except Exception as e:
            log_failure(logger, logging.WARNING, "meal_interim_failed", e)


# Persists a 'planned' result: the meal_plan slots, plus — when the day-of solver SWAPPED shops (the
# assigned shop sold out) — the effective shop onto daily_plan.meal_plan_provider, so the expense
# reconciler matches a spend at the shop B was actually sent to (not the stale scaffold pick).
# Best-effort: a write hiccup never loses the card. Shared by all three plan entry points.
def _persist_planned(today, result: dict, source: str) -> None:
    try:
        persistence.save_meal_plan(today, result["slots"], meta={"source": source, "as_of": str(today)})
        if result.get("shop_swapped"):
            # daily_plan holds ONE provider/day; update_meal_provider repoints the whole day. Known edge
            # (pre-existing): if a slot was already bought at the OLD shop and only the other slot swaps,
            # a LATE spend at the old shop could then fail to match. Rare (spends usually reconcile same-day).
            persistence.update_meal_provider(today, result["shop"])
            # Re-balance the rest of the week around the swap (B 2026-07-01). Best-effort — the count feeds
            # the "↻ re-balanced" note; a refit failure never undoes today's plan.
            try:
                result["week_refit"] = _refit_week_shops(today, result["shop"])
            except Exception as e:
                log_failure(logger, logging.WARNING, "week_refit_failed", e, plan_date=str(today))
    except Exception as e:
        log_failure(logger, logging.ERROR, "meal_save_failed", e, plan_date=str(today))


# Interactive entry for `/plan -> 🍽️ Meal` (and the 11am cron, which calls run_meals later). Plans the
# day, persists the slots, SELF-SENDS the card (so it can pin it kind='meal' + register correction-state
# on the sent message), and returns [] (the webhook sends nothing more). The card's ✓ Ate buttons fire
# the completion handler (#5). Input: the CALLBACK_QUERY InboundMessage. Output: [] (self-sent).
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


# Extracts which of the shop's known dishes B is saying are unavailable. The model may ONLY return names
# copied from the provided list (we re-match case-insensitively, so drift is dropped) — never invents.
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

# Category prefixes a promo board prepends to a dish name — stripped before matching so a board label
# ("Grain Bowl - Morrocan Rice & Cajun") maps to the plain stored dish ("Morrocan Rice & Cajun Chicken").
_MENU_CATEGORY_PREFIX = re.compile(
    r"^(new\s+)?(salad|pasta|grain\s+bowl|grain|bowl|rice(berry)?|noodles|set|combo)\s*[-–:]\s*",
    re.IGNORECASE)
_DISH_STOP = frozenset({"the", "with", "and", "a", "of", "in", "on", "set", "combo", "new"})


# The dish's significant tokens (lowercased, category-prefix stripped, punctuation split, stopwords/1-char
# dropped) — the unit fuzzy matching compares on.
def _dish_tokens(s) -> set:
    stripped = _MENU_CATEGORY_PREFIX.sub("", str(s or "").strip().lower())
    toks = re.findall(r"[a-z0-9]+", stripped)
    return {t for t in toks if t not in _DISH_STOP and len(t) > 1}


# Re-matches model-returned dish names against the shop's real menu names, dropping anything not on the
# menu — the model can only ever NARROW to real item_names, never hallucinate one. Drift- and promo-board
# tolerant (B 2026-07-01): exact (case-insensitive) first, else a token-SUBSET match after stripping the
# board's category prefix — every significant token of the shorter name must appear in the longer (so
# board "New Salad - Blackened Chicken & Ranch" maps to stored "Blackened Chicken & Ranch Salad", but two
# different "Grilled Chicken …" dishes do NOT collapse). Requires ≥2 significant tokens to avoid a lone
# "chicken" matching everything.
def _match_known(returned, names: list[str]) -> set:
    by_lower = {n.strip().lower(): n for n in names}
    name_tok = [(n, _dish_tokens(n)) for n in names]
    out: set = set()
    for r in returned or []:
        rl = str(r).strip().lower()
        if rl in by_lower:                                   # exact
            out.add(by_lower[rl])
            continue
        rt = _dish_tokens(r)
        if len(rt) < 2:
            continue
        for n, nt in name_tok:
            if len(nt) < 2:
                continue
            small, big = (rt, nt) if len(rt) <= len(nt) else (nt, rt)
            if small <= big:                                 # all of the shorter's tokens are in the longer
                out.add(n)
                break
    return out


# Learns dishes that aren't available at today's shop from B's correction — her words and/or a photo of
# the shop's board — and persists them (daily_plan.unavailable_items) so plan_meals drops them from the
# palette now AND on every re-run today (B 2026-07-01). Best-effort: any failure just means no exclusion
# learned, and the normal re-compose proceeds. A menu-board photo the model judges COMPLETE also excludes
# everything on the known menu it did NOT see offered (guarded by a few confirmed hits so a blurry shot
# can't nuke the whole menu). Returns the count recorded.
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
        # ALL photos of a menu-board album are read in ONE vision call (B sends the board as several
        # screenshots — they cover different sections of the SAME menu, so they must be judged together;
        # judging them one-at-a-time never sees the full board).
        file_ids = list(getattr(msg, "media_group_file_ids", None) or [])
        if not file_ids and getattr(msg, "file_id", None):
            file_ids = [msg.file_id]
        if is_photo and file_ids:
            try:
                token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
                images = [get_file_bytes(fid, token) for fid in file_ids[:4]]   # cap like the expense album
                prompt = _UNAVAIL_PHOTO_PROMPT.format(shop=shop, names=listed)
                raw = (generate_with_images(images, prompt, model=MODEL_FLASH) if len(images) > 1
                       else generate_with_image(images[0], prompt, model=MODEL_FLASH))
                parsed = parse_json_response(raw)
                unavailable |= _match_known(parsed.get("unavailable"), names)
                available = _match_known(parsed.get("available"), names)
                # A COMPLETE board is the menu of record for today: keep only what's on it (drop everything
                # else). Guarded by enough confirmed matches so a blurry/partial shot can't nuke the menu.
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


# Quoted-reply meal correction (domain='plan', kind='meal'): B's fix re-composes the un-eaten slot(s)
# via Flash, honoring her words (sold-out / swap / give-me-X) AND a photo of the shop's board if she
# attaches one, then re-sends + re-pins the card. Any dish she flags as unavailable (in text or the
# photo) is recorded on the day + stripped from the palette, so it's never re-offered today. Eaten slots
# are untouched (taken_slots excludes them; save_meal_plan won't overwrite a logged slot).
# Input: the quoting message + its conversation_state. Output: [] (self-sent).
def handle_meal_correction(msg, state: dict) -> list[tuple]:
    text = (getattr(msg, "text", None) or getattr(msg, "caption", None) or "").strip()
    is_photo = getattr(msg, "message_type", None) == MessageType.PHOTO and bool(
        getattr(msg, "file_id", None) or getattr(msg, "media_group_file_ids", None))
    if not text and not is_photo:
        return [("✏️ Tell me what to change — a note, or a photo of the shop's menu today.", None)]
    today, _tz = get_local_today()
    # Learn + persist any sold-out dishes (from B's words and/or the menu photo) BEFORE re-planning, so
    # plan_meals reads them back and drops them from the palette (deterministic — not left to the LLM).
    _learn_unavailable(msg, today, _tz, text, is_photo)
    # If B says the shop is closed / sold out / "order from another shop", force a shop swap (the menu-empty
    # auto-swap can't detect a shop that's shut but still has menu data — only B knows).
    avoid = _wants_shop_change(text)
    # Preserve un-flagged slots: if only specific dishes are unavailable, keep the still-available slot(s)
    # and re-pick only the flagged one(s). Off when B wants a whole-shop change (avoid) — then re-pick all.
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


# Sends the meal message, pinning it kind='meal' (replacing the prior day's) and registering plan/meal
# correction-state when it's a real card. All best-effort — a pin/state hiccup never loses the message.
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


# 11am cron (BRIEF §6/§8): sweep yesterday, then plan today and PROACTIVELY send the card (or the
# /suggest_food guide on own-food days), pinned kind='meal'. The proactive card is NOT quote-correctable
# (no triggering inbound update); B adjusts via /plan -> 🍽️ Meal. Best-effort. Returns the sent text.
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
