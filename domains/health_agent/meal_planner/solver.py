"""
Day-of meal solver — deterministic core (BRIEF §6 "Day-of (11am)"). Layer 1 of the two-layer solver:
compute what macros are still OPEN for the day, then FILTER the assigned shop's menu to dishes that fit.
Gemini (layer 2, later module) composes lunch+dinner+staples from the filtered palette; code does the
final macro check. Everything here is PURE + unit-tested; the DB reads (logged-today, macro_target,
menu) live in the package's persistence module.

`compute_remaining` mirrors spec C's "target left was 1,151–1,401 kcal · 83–105P · ≥40F · fibre →20g":
remaining = the day's macro_target MINUS what's already accounted for (food logged so far + the day's
reserved workout fuel that hasn't been logged yet, §7). Bands clamp at 0 (an over-eaten day leaves 0,
never negative).

Functions:
  compute_remaining(macro_target, consumed) -> dict   # remaining budget for the day's meals
  filter_menu(items, remaining) -> list[dict]         # shop dishes that can fit the remaining budget
  taken_slots(meal_rows, logged_meal_types) -> set    # which of lunch/dinner is already eaten/recorded
  reserved_fuel(activity_type, logged_markers, fixed_cfg) -> dict   # workout fuel to reserve (unlogged)
  logged_fuel_markers(food_items, fixed_cfg) -> set   # fuel already in food_log (avoid double-reserve)
  finalize_meals(proposed_slots, palette, staples_cfg, consumed, macro_target) -> dict  # validate + check
  select_items(meal_row, kind, ref) -> (items, already)   # ✓Ate: which items to post + idempotency (ref=dish idx/staple name)
  owed_proteins(tally, rotation_cfg) -> list   # proteins still owed this week (rotation nudge / suggest)
  owed_proteins_split(week_tally, fortnight_tally, rotation_cfg) -> list   # "per 2wk" specs judged on the fortnight
  parse_serving(serving) -> (amount, unit)     # "150 g" -> (150.0, "g"); "1 egg" -> (1.0, "egg")
  staple_label(item) -> str                    # card/button label, e.g. "edamame 150 g" / "2× boiled_egg"
  dish_label(item) -> str                      # main-dish button label, e.g. "B10 Tender Chicken Teriyaki"
"""

import re

_MACROS = ("kcal", "protein_g", "fat_g", "carbs_g", "fibre_g")

# A meal SLOT is "taken" (don't plan it) when something of its kind is already eaten. food_log meal_type
# maps to a slot here; a meal_plan slot that's bought/ate is also taken.
_SLOT_LOG_TYPES = {"lunch": {"lunch", "brunch"}, "dinner": {"dinner", "supper"}}


def _left(target_val, consumed_val) -> int:
    return max(0, round((target_val or 0) - (consumed_val or 0)))


# Subtracts already-consumed macros from the day's target, returning the budget LEFT for the meal plan.
# Input: macro_target = the scaffold's day target {kcal{low,target,high}, protein_g{low,high},
# fat_g{min}, carbs_g{target}, fibre_g{target,stretch}, day_type}; consumed = {kcal, protein_g, fat_g,
# carbs_g, fibre_g} = food logged so far + the day's reserved-but-unlogged workout fuel (summed by the
# caller). Output: the same shape as macro_target, every band clamped at >= 0. Pure.
def compute_remaining(macro_target: dict, consumed: dict) -> dict:
    mt = macro_target or {}
    c = consumed or {}
    kcal = mt.get("kcal", {})
    protein = mt.get("protein_g", {})
    fat = mt.get("fat_g", {})
    carbs = mt.get("carbs_g", {})
    fibre = mt.get("fibre_g", {})
    return {
        "kcal": {
            "low": _left(kcal.get("low"), c.get("kcal")),
            "target": _left(kcal.get("target"), c.get("kcal")),
            "high": _left(kcal.get("high"), c.get("kcal")),
        },
        "protein_g": {
            "low": _left(protein.get("low"), c.get("protein_g")),
            "high": _left(protein.get("high"), c.get("protein_g")),
        },
        "fat_g": {"min": _left(fat.get("min"), c.get("fat_g"))},
        "carbs_g": {"target": _left(carbs.get("target"), c.get("carbs_g"))},
        "fibre_g": {
            "target": _left(fibre.get("target"), c.get("fibre_g")),
            "stretch": _left(fibre.get("stretch"), c.get("fibre_g")),
        },
        "day_type": mt.get("day_type"),
    }


# Filters a shop's menu to dishes that CAN fit the day's remaining budget — the palette the LLM (layer
# 2) composes lunch+dinner from (alongside the home staples). A dish fits if it has usable macro data
# and doesn't ALONE blow the remaining kcal ceiling (it's one of two meals; the precise pairing /
# protein+fat floors / lunch≠dinner / price are checked at compose + final-check time, later modules).
# An exhausted budget (remaining high <= 0) yields an empty palette. exclude_names = dishes B has told us
# are NOT available at this shop today (daily_plan.unavailable_items) — dropped up front (case-insensitive)
# so the compose LLM can never re-offer a sold-out item, and the exclusion survives every re-run today
# (B 2026-07-01: "it needs to remember what there isn't from that shop on that day"). Input: items = menu
# dicts (each with item_name + kcal/...); remaining = compute_remaining output. Output: the fitting items. Pure.
def filter_menu(items: list[dict], remaining: dict, exclude_names=None) -> list[dict]:
    high = (remaining.get("kcal") or {}).get("high") or 0
    excl = {str(n).strip().lower() for n in (exclude_names or ())}
    fitting = []
    for it in items:
        if str(it.get("item_name") or "").strip().lower() in excl:   # sold-out / unavailable today
            continue
        kcal = it.get("kcal")
        if not kcal or kcal <= 0:        # no macro data -> can't compose a balanced day from it
            continue
        if kcal > high:                  # alone busts the day's remaining kcal (high=0 -> nothing fits)
            continue
        fitting.append(it)
    return fitting


# On a meal correction that flags only SPECIFIC dishes unavailable, decides which un-eaten slots to KEEP
# unchanged: a slot is kept when ALL its current dishes are still available (none in this shop's unavailable
# set) — only the flagged/empty slot(s) get re-picked (B 2026-07-01: "keep what I didn't say is unavailable
# — it's implicitly available"). No unavailable dishes at all -> keep nothing (a full re-pick, prior
# behaviour). Case-insensitive match. Pure. Input: shop name; unavailable = {shop: [item_name]}; planned =
# {slot: [items]} the current picks; uneaten = the un-eaten slots. Output: {slot: [items]} to keep verbatim.
def slots_to_keep(shop, unavailable: dict | None, planned: dict, uneaten: list) -> dict:
    excl = {str(n).strip().lower() for n in (unavailable or {}).get(shop, [])}
    if not (shop and excl):
        return {}
    keep: dict = {}
    for slot in uneaten:
        items = planned.get(slot)
        if items and not any((i.get("item_name") or "").strip().lower() in excl for i in items):
            keep[slot] = items
    return keep


# Which of {lunch, dinner} is already handled, so the planner plans ONLY the rest (BRIEF §6; B 2026-06-25:
# "don't plan a slot that already has a record"). A slot is taken if its meal_plan row is bought/ate OR
# a food_log entry today maps to it (lunch/brunch -> lunch; dinner/supper -> dinner). Pure.
# Input: meal_rows = [{meal_type, status}] from nutrition.meal_plan today; logged_meal_types = the set of
# food_log.meal_type values logged today. Output: a subset of {"lunch", "dinner"}.
def taken_slots(meal_rows: list[dict], logged_meal_types) -> set:
    logged = set(logged_meal_types or [])
    plan_status = {r.get("meal_type"): r.get("status") for r in (meal_rows or [])}
    taken = set()
    for slot in ("lunch", "dinner"):
        if plan_status.get(slot) in ("bought", "ate") or (logged & _SLOT_LOG_TYPES[slot]):
            taken.add(slot)
    return taken


# The workout fuel to RESERVE against today's meal budget (BRIEF §7): fuel is eaten ~1pm, AFTER the 11am
# plan, so at plan time it isn't logged yet — reserve it so the meals don't overfill. cardio days reserve
# run_fuel, strength days reserve strength_fuel. A fuel item whose marker is ALREADY in today's food_log
# is dropped (it's counted in `logged` — never double-reserve). Pure. Output: summed macros to add to
# `consumed` before compute_remaining.
def reserved_fuel(activity_type, logged_markers, fixed_cfg: dict) -> dict:
    markers = set(logged_markers or [])
    acts = set(activity_type or [])
    items: list[dict] = []
    if "cardio" in acts:
        items += (fixed_cfg or {}).get("run_fuel", []) or []
    if "strength" in acts:
        items += (fixed_cfg or {}).get("strength_fuel", []) or []
    total = {m: 0 for m in _MACROS}
    for it in items:
        if it.get("marker") in markers:
            continue                     # already logged -> don't reserve again
        for m in _MACROS:
            total[m] += it.get(m) or 0
    return total


# Best-effort detection of which fixed_intake fuel MARKERS are already in today's food_log, so
# reserved_fuel won't double-count fuel B logged (BRIEF §7 "logging it supersedes its reserve"). Matches
# the marker's base word as a case-insensitive substring of a logged food_item (approximate — a typed
# "banana"/"milk"/"cereal"/"whey protein" log resolves its marker). Input: food_items = today's
# food_log item texts; fixed_cfg = goals fixed_intake. Output: the set of matched markers. Pure.
def logged_fuel_markers(food_items, fixed_cfg: dict) -> set:
    texts = [str(t).lower() for t in (food_items or [])]
    found = set()
    for group in ("run_fuel", "strength_fuel"):
        for it in (fixed_cfg or {}).get(group, []) or []:
            marker = it.get("marker")
            if marker and any(marker.split("_")[0] in t for t in texts):
                found.add(marker)
    return found


def _servings(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


# Parses a staple serving string ("150 g" / "1 egg" / "200 ml") -> (amount, unit). Default (1, "serving").
def parse_serving(serving) -> tuple:
    m = re.match(r"\s*([0-9.]+)\s*([A-Za-z]+)", str(serving or ""))
    return (float(m.group(1)), m.group(2).lower()) if m else (1.0, "serving")


# The chosen staple amount IN ITS UNIT, clamped to (0, max] and rounded (eggs whole >=1; g/ml to the
# nearest 10, >=10). Reads the LLM's `amount`; tolerates the legacy `servings` contract; defaults to one
# serving when absent. B 2026-06-28: the bot picks grams up to the max, not just whole servings.
def _staple_amount(it: dict, serv_amt: float, max_amt: float, unit: str) -> float:
    raw = it.get("amount")
    if raw is None and it.get("servings") is not None:        # legacy: servings × serving size
        raw = _servings(it.get("servings")) * serv_amt
    try:
        amt = float(raw) if raw is not None else serv_amt     # default: one serving
    except (TypeError, ValueError):
        amt = serv_amt
    if amt != amt:                                            # NaN (json.loads allows it) -> one serving
        amt = serv_amt
    amt = min(max(amt, 0.0), max_amt)
    if unit == "egg":
        return float(max(1, min(int(round(amt)), int(round(max_amt)))))
    return float(min(max(round(amt / 10.0) * 10, 10), max_amt))   # g/ml -> nearest 10, in [10, max]


# Human label for a staple's amount on the card / button: eggs as a count ("2× boiled_egg"), measured
# staples as amount+unit ("edamame 150 g"). Reads item amount/unit (set by finalize_meals); name-only fallback.
def staple_label(item: dict) -> str:
    name = item.get("item_name", "")
    amount, unit = item.get("amount"), (item.get("unit") or "")
    if amount is None:
        return name
    amt = int(amount) if float(amount) == int(float(amount)) else round(float(amount), 1)
    return f"{amt}× {name}" if unit == "egg" else f"{name} {amt} {unit}"


_DISH_CODE_RE = re.compile(r"^([A-Za-z]{1,3}\d+)\b")


# Button/label name for a MAIN dish: the menu code (leading "B10"/"P14"/… token of item_name, if present)
# + the clean English gloss (name_en) — e.g. "B10 Tender Chicken Teriyaki". Falls back to item_name when
# there's no English gloss; avoids doubling the code if the English already starts with it. B 2026-06-28.
def dish_label(item: dict) -> str:
    name = (item.get("item_name") or "").strip()
    m = _DISH_CODE_RE.match(name)
    code = m.group(1) if m else ""
    english = (item.get("name_en") or name).strip()
    if code and not english.startswith(code):
        return f"{code} {english}"
    return english or name or "this dish"


# The deterministic guarantee over the LLM's composition (BRIEF §6 "code does the final macro check +
# enforces the per-item staple caps"). Validates each proposed item against the REAL palette / staples
# (an item the LLM invented is DROPPED, never trusted), clamps a staple's amount to its max + scales the
# macros to it, attaches the authoritative macros (code owns the numbers, not the LLM), projects the day
# total, and flags floors/ceiling bent.
# Input: proposed_slots = {slot: [{item_name, role?, amount?}]} (the slots the LLM was asked to plan);
# palette = filter_menu output (shop dishes w/ macros); staples_cfg = goals home_staples {name:{serving,
# max_servings, kcal,...}}; consumed = already-eaten macros; macro_target = the day target (for the
# ceiling/floor check). Output: {slots: {slot:[validated items]}, projected: {macros}, report: [str]}. Pure.
def finalize_meals(proposed_slots: dict, palette: list[dict], staples_cfg: dict,
                   consumed: dict, macro_target: dict | None = None) -> dict:
    by_name = {d["item_name"]: d for d in (palette or [])}
    staples_cfg = staples_cfg or {}
    out_slots: dict = {}
    report: list[str] = []
    chosen = {m: 0.0 for m in _MACROS}

    for slot, items in (proposed_slots or {}).items():
        validated = []
        for it in (items or []):
            name = it.get("item_name")
            if name in by_name:                              # a real shop dish — code owns its macros
                d = by_name[name]
                macros = {m: (d.get(m) or 0) for m in _MACROS}
                # protein_source comes from the LLM's tag (the menu doesn't carry it) — it feeds the
                # weekly protein rotation once posted to food_log. Normalised to a list.
                ps = it.get("protein_source")
                ps = [ps] if isinstance(ps, str) else (ps if isinstance(ps, list) else None)
                validated.append({"item_name": name, "restaurant": d.get("restaurant"),
                                  "role": it.get("role") or "main", "price_thb": d.get("price_thb"),
                                  "protein_source": ps,
                                  # sugar/sodium aren't in _MACROS (not part of the day projection) but ride
                                  # along to food_log; macro_meta carries any compose-time gap-fill provenance.
                                  "sugar_g": d.get("sugar_g"), "sodium_mg": d.get("sodium_mg"),
                                  "macro_meta": d.get("macro_meta"), **macros})
            elif name in staples_cfg:                        # a home staple — pick amount up to max, scale macros
                cfg = staples_cfg[name]
                serv_amt, unit = parse_serving(cfg.get("serving"))
                max_amt = serv_amt * int(cfg.get("max_servings", 1))
                amount = _staple_amount(it, serv_amt, max_amt, unit)
                try:                                         # report only when the LLM asked for over the max
                    if it.get("amount") is not None and float(it["amount"]) > max_amt:
                        report.append(f"capped {name} to {round(amount)}{unit} (max {round(max_amt)}{unit})")
                except (TypeError, ValueError):
                    pass
                scale = (amount / serv_amt) if serv_amt else 0
                macros = {m: (cfg.get(m) or 0) * scale for m in _MACROS}
                validated.append({"item_name": name, "restaurant": None, "role": "staple",
                                  "amount": amount, "unit": unit, "serving": cfg.get("serving"),
                                  "price_thb": 0,
                                  "sugar_g": None, "sodium_mg": None,   # filled post-finalize (config lacks them)
                                  **macros})
            else:
                report.append(f"dropped unrecognised item: {name}")
                continue
            for m in _MACROS:
                chosen[m] += macros[m]
        out_slots[slot] = validated

    projected = {m: round((consumed.get(m) or 0) + chosen[m]) for m in _MACROS}
    if macro_target:
        high = (macro_target.get("kcal") or {}).get("high")
        p_low = (macro_target.get("protein_g") or {}).get("low")
        f_min = (macro_target.get("fat_g") or {}).get("min")
        if high and projected["kcal"] > high:
            report.append(f"day projects {projected['kcal']} kcal — over the {high} ceiling")
        if p_low and projected["protein_g"] < p_low:
            report.append(f"protein {projected['protein_g']}g — under the {p_low}g floor")
        if f_min and projected["fat_g"] < f_min:
            report.append(f"fat {projected['fat_g']}g — under the {f_min}g floor")
    return {"slots": out_slots, "projected": projected, "report": report}


# Picks the items to post for a ✓ Ate tap + whether they're already logged (idempotency). Pure; called
# INSIDE the locked transaction by persistence.claim_and_post. meal_row = {items, status, meta}. `ref` is
# the dish INDEX for kind 'd', the staple NAME for kind 's', None for kind 'm'. Kinds:
#   'd' (one main dish, B 2026-06-28): mains[ref], already if ref in meta.posted_mains.
#   's' (one staple): that staple, already if its name in meta.posted_staples.
#   'm' (whole slot's mains — legacy, no longer rendered): all non-staple items, already if slot 'ate'.
def select_items(meal_row: dict, kind: str, ref) -> tuple:
    items = meal_row.get("items") or []
    if kind == "m":
        return [i for i in items if i.get("role") != "staple"], meal_row.get("status") == "ate"
    if kind == "d":
        mains = [i for i in items if i.get("role") != "staple"]
        if not isinstance(ref, int) or not (0 <= ref < len(mains)):
            return [], True                                  # stale/out-of-range index -> benign no-op
        posted = set((meal_row.get("meta") or {}).get("posted_mains") or [])
        return [mains[ref]], (ref in posted)
    posted = set((meal_row.get("meta") or {}).get("posted_staples") or [])
    chosen = [i for i in items if i.get("role") == "staple" and i.get("item_name") == ref]
    return chosen, (ref in posted)


# The minimum count from a rotation spec: '>=1' -> 1, '1-2' -> 1, '>=1 per 2wk' -> 1 (first number).
def _min_count(spec) -> int:
    nums = re.findall(r"\d+", str(spec))
    return int(nums[0]) if nums else 1


# Proteins still owed this week, given the actual tally and the rotation config (beef>=1, pork>=1,
# fish 1-2, duck>=1/2wk). A protein is owed when its tally is below the spec's minimum. Feeds both the
# day-of compose nudge and the /suggest_food guide. Pure. Input: tally {protein: count}, rotation_cfg
# {protein: spec}. Output: the owed protein tokens (config order).
def owed_proteins(tally: dict, rotation_cfg: dict) -> list:
    tally = tally or {}
    return [p for p, spec in (rotation_cfg or {}).items() if tally.get(p, 0) < _min_count(spec)]


# owed_proteins over BOTH windows: a spec containing "2wk" (duck ">=1 per 2wk") is judged on the
# FORTNIGHT tally (last Monday .. this Sunday), every other spec on the WEEK tally (Mon-Sun) — so duck
# eaten LAST week doesn't re-surface as owed this week (B 2026-07-02). Same split the weekly reflection
# applies (weekly_reflection/render._protein_row), so planner + reflection agree. Pure; config order.
def owed_proteins_split(week_tally: dict, fortnight_tally: dict, rotation_cfg: dict) -> list:
    week_tally, fortnight_tally = week_tally or {}, fortnight_tally or {}
    return [p for p, spec in (rotation_cfg or {}).items()
            if ((fortnight_tally if "2wk" in str(spec) else week_tally).get(p, 0)) < _min_count(spec)]
