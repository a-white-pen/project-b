"""
Pure helpers for day-of meal planning.

The solver subtracts logged food and reserved workout food from the daily calorie and protein targets,
filters menu choices, validates model selections, and calculates the projected daily totals. Database
reads live in persistence.py.

Functions:
  _left — subtracts a consumed value without going below zero
  compute_remaining — computes remaining calorie and protein ranges
  filter_menu — keeps available dishes within the calorie limit
  slots_to_keep — keeps planned slots unaffected by unavailable dishes
  taken_slots — finds lunch and dinner slots already used
  reserved_fuel — totals workout food not yet logged
  logged_fuel_markers — finds workout foods already logged
  _servings — validates a serving count
  parse_serving — parses a serving amount and unit
  _staple_amount — validates and rounds a staple amount
  staple_label — formats a staple for cards and buttons
  dish_label — formats a main dish for buttons
  finalize_meals — validates model choices and calculates daily totals
  select_items — selects items for a meal confirmation
  _min_count — reads the minimum from a rotation setting
  owed_proteins — finds proteins still due in the weekly rotation
  owed_proteins_split — applies weekly and two-week rotation windows
  suggest_staples — selects protein top-ups within the calorie limit
"""

import re

_MACROS = ("kcal", "protein_g", "fat_g", "carbs_g", "fibre_g")

# Food-log meal types that count as lunch or dinner.
_SLOT_LOG_TYPES = {"lunch": {"lunch", "brunch"}, "dinner": {"dinner", "supper"}}


# Subtracts a consumed value from a target. Returns zero instead of a negative value.
def _left(target_val, consumed_val) -> int:
    return max(0, round((target_val or 0) - (consumed_val or 0)))


# Subtracts consumed calories and protein from the daily target. Values never go below zero.
def compute_remaining(macro_target: dict, consumed: dict) -> dict:
    mt = macro_target or {}
    c = consumed or {}
    kcal = mt.get("kcal", {})
    protein = mt.get("protein_g", {})
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
    }


# Keeps menu items that have calories, fit the remaining calorie limit, and are available today.
# Inputs: menu items, remaining target, and optional unavailable names. Returns the usable menu items.
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


# Keeps uneaten meal slots whose dishes are still available. Returns the slots that need no replacement.
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


# Returns lunch and dinner slots that are already bought, eaten, or present in today's food log.
def taken_slots(meal_rows: list[dict], logged_meal_types) -> set:
    logged = set(logged_meal_types or [])
    plan_status = {r.get("meal_type"): r.get("status") for r in (meal_rows or [])}
    taken = set()
    for slot in ("lunch", "dinner"):
        if plan_status.get(slot) in ("bought", "ate") or (logged & _SLOT_LOG_TYPES[slot]):
            taken.add(slot)
    return taken


# Sums unlogged workout food for the day's activities so it is reserved in the meal budget.
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


# Finds workout-food markers already present in today's food log to avoid reserving them twice.
def logged_fuel_markers(food_items, fixed_cfg: dict) -> set:
    texts = [str(t).lower() for t in (food_items or [])]
    found = set()
    for group in ("run_fuel", "strength_fuel"):
        for it in (fixed_cfg or {}).get(group, []) or []:
            marker = it.get("marker")
            if marker and any(marker.split("_")[0] in t for t in texts):
                found.add(marker)
    return found


# Converts a serving count to a positive integer. Invalid values return one.
def _servings(value) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


# Parses a serving label such as "150 g" into an amount and unit. Defaults to one serving.
def parse_serving(serving) -> tuple:
    m = re.match(r"\s*([0-9.]+)\s*([A-Za-z]+)", str(serving or ""))
    return (float(m.group(1)), m.group(2).lower()) if m else (1.0, "serving")


# Validates a staple amount, applies its maximum, and rounds eggs or measured amounts appropriately.
def _staple_amount(it: dict, serv_amt: float, max_amt: float, unit: str) -> float:
    raw = it.get("amount")
    if raw is None and it.get("servings") is not None:
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


# Formats a staple amount for cards and buttons.
def staple_label(item: dict) -> str:
    name = item.get("item_name", "")
    amount, unit = item.get("amount"), (item.get("unit") or "")
    if amount is None:
        return name
    amt = int(amount) if float(amount) == int(float(amount)) else round(float(amount), 1)
    return f"{amt}× {name}" if unit == "egg" else f"{name} {amt} {unit}"


_DISH_CODE_RE = re.compile(r"^([A-Za-z]{1,3}\d+)\b")


# Formats a main dish label with its menu code and English name when available.
def dish_label(item: dict) -> str:
    name = (item.get("item_name") or "").strip()
    m = _DISH_CODE_RE.match(name)
    code = m.group(1) if m else ""
    english = (item.get("name_en") or name).strip()
    if code and not english.startswith(code):
        return f"{code} {english}"
    return english or name or "this dish"


# Validates proposed items against the menu and staple config, applies staple limits, and calculates
# daily totals. Returns validated slots, projected nutrition values, and any target warnings.
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
                # Normalize the model's protein tag for the weekly rotation tally.
                ps = it.get("protein_source")
                ps = [ps] if isinstance(ps, str) else (ps if isinstance(ps, list) else None)
                validated.append({"item_name": name, "restaurant": d.get("restaurant"),
                                  "role": it.get("role") or "main", "price_thb": d.get("price_thb"),
                                  "protein_source": ps,
                                  # Preserve extra nutrition values and estimation details for food_log.
                                  "sugar_g": d.get("sugar_g"), "sodium_mg": d.get("sodium_mg"),
                                  "macro_meta": d.get("macro_meta"), **macros})
            elif name in staples_cfg:                        # a home staple — pick amount up to max, scale macros
                cfg = staples_cfg[name]
                serv_amt, unit = parse_serving(cfg.get("serving"))
                max_amt = serv_amt * int(cfg.get("max_servings", 1))
                amount = _staple_amount(it, serv_amt, max_amt, unit)
                try:
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
        kcal = macro_target.get("kcal") or {}
        protein = macro_target.get("protein_g") or {}
        low, high = kcal.get("low"), kcal.get("high")
        p_low, p_high = protein.get("low"), protein.get("high")
        if low and projected["kcal"] < low:
            report.append(f"day projects {projected['kcal']} kcal — under the {low} floor")
        if high and projected["kcal"] > high:
            report.append(f"day projects {projected['kcal']} kcal — over the {high} ceiling")
        if p_low and projected["protein_g"] < p_low:
            report.append(f"protein {projected['protein_g']}g — under the {p_low}g floor")
        if p_high and projected["protein_g"] > p_high:
            report.append(f"protein {projected['protein_g']}g — over the {p_high}g range")
    return {"slots": out_slots, "projected": projected, "report": report}


# Selects the items for a meal button and reports whether they were already logged.
# `d` selects one dish, `s` selects one staple, and `m` selects all main dishes.
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


# Extracts the minimum count from a protein-rotation setting.
def _min_count(spec) -> int:
    nums = re.findall(r"\d+", str(spec))
    return int(nums[0]) if nums else 1


# Returns proteins whose weekly tally is below the configured minimum.
def owed_proteins(tally: dict, rotation_cfg: dict) -> list:
    tally = tally or {}
    return [p for p, spec in (rotation_cfg or {}).items() if tally.get(p, 0) < _min_count(spec)]


# Uses the two-week tally for settings containing "2wk" and the weekly tally for all others.
def owed_proteins_split(week_tally: dict, fortnight_tally: dict, rotation_cfg: dict) -> list:
    week_tally, fortnight_tally = week_tally or {}, fortnight_tally or {}
    return [p for p, spec in (rotation_cfg or {}).items()
            if ((fortnight_tally if "2wk" in str(spec) else week_tally).get(p, 0)) < _min_count(spec)]


# Suggests home staples to fill the remaining protein without exceeding the calorie limit.
# Returns the selected staples and their combined nutrition values.
def suggest_staples(remaining: dict, staples_cfg: dict) -> dict:
    kcal_budget = (remaining.get("kcal") or {}).get("high") or 0
    protein_gap = (remaining.get("protein_g") or {}).get("low") or 0
    cands: list[dict] = []
    for name, cfg in (staples_cfg or {}).items():
        serv_amt, unit = parse_serving(cfg.get("serving"))
        max_amt = serv_amt * int(cfg.get("max_servings", 1))
        scale = (max_amt / serv_amt) if serv_amt else 1
        cands.append({"item_name": name, "amount": max_amt, "unit": unit,
                      "serving": cfg.get("serving"), "role": "staple",
                      **{m: (cfg.get(m) or 0) * scale for m in _MACROS}})
    chosen: list[dict] = []
    added = {m: 0.0 for m in _MACROS}
    while cands:
        prot_short = added["protein_g"] < protein_gap
        if not prot_short:
            break

        # Scores a staple by protein per calorie when it still fits the calorie limit.
        def _score(c):
            if (c["kcal"] or 0) > kcal_budget - added["kcal"]:      # would bust the day's kcal ceiling
                return -1.0
            s = 0.0
            if prot_short:
                s += (c["protein_g"] or 0) / max(c["kcal"] or 1, 1)
            return s

        best = max(cands, key=_score)
        if _score(best) <= 0:                                       # nothing helps within the kcal ceiling
            break
        chosen.append(best)
        for m in _MACROS:
            added[m] += best[m] or 0
        cands.remove(best)
    return {"staples": chosen, "added": {m: round(added[m], 1) for m in _MACROS}}
