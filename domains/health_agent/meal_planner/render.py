"""
Renders the day-of meal result (plan_meals output) for Telegram — the Message Workbook "Order Card"
redesign. HTML (bold/italic + <pre> "copy boxes"): the shop name and each slot's dish names sit in
<pre> blocks so Telegram renders them as tap-to-copy (handy for ordering Thai dish names), with
macros/price OUTSIDE the boxes. A Thai-script dish gets a SECOND copy box underneath holding its English
gloss (name_en, from the cheap Flash-Lite translation pass in service._attach_name_en) — Thai stays
cleanly copyable on top, English readable below. A WongNai shop also gets a second copy box with its Thai
listing title under the English name (_SHOP_TH). Per-slot macros are P·C·F. Then an
activity-aware "Around your run" / "Around your strength" fuel line and the shared hybrid day block
(_day_block): an aligned box of PER-MEAL rows (Breakfast / Lunch / Snack / … + the meals being ordered +
fuel) -> rule -> Total so far, then BOLD Target [ranges] + Still-to-eat lines beneath (still-to-eat counts
down to the LOW of each range). Columns kcal·P·C·F·Fib. ALL dynamic content is html.escape()'d.

The ✓ Ate buttons are PER-DISH (one per main dish + one per staple, B 2026-06-28); the handler removes each on tap
(BRIEF §6). Short-circuits (all_eaten / at_limit / compose_failed) stay one-liners; own-food days
render the eating-on-your-own guide (spec G, suggest2 redesign).

Functions:
  render_meal_card(result) -> (text, reply_markup|None)
  render_suggest(result) -> str    # spec G — eating-on-your-own guide
"""

from domains.health_agent.meal_planner import solver
from system.text import esc as _esc, is_thai as _is_thai

_MK = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")   # macro keys

# WongNai shops -> their full WongNai listing title (Thai), shown in a 2nd copy box UNDER the English name
# so B can find/order them in the app. Keys = the canonical restaurant_name (the join key everywhere);
# sourced once from the WongNai pages (B 2026-06-28). Non-WongNai shops (FitFuel by Grain, Jones) get none.
_SHOP_TH = {
    "Freshies Clean Ketogenic": "Freshies Clean Ketogenic วงศ์สว่าง",
    "KIN Healthy": "KIN Healthy อาหารคลีน ประชาชื่น",
    "Deelizz On Table": "อาหารคลีน ตามสั่ง Deelizz On Table ประชาชื่น 39",
    "Budder Clean Food": "Budder อาหารคลีนโคตรอร่อย สะพานควาย",
    "FitFish": "FitFish ปลาย่าง อาหารคลีน",
    "Chicken Breast Kitchen": "อกไก่ Kitchen อาหารคลีน",
    "Leanlicious": "Leanlicious อาหารคลีน เดอะมอลล์ งามวงศ์วาน",
}

# Order-card header day-type phrasing (B 2026-06-28). day_type ∈ {cardio, strength, rest}; default "today".
_DAY_TYPE_LABEL = {"cardio": "cardio today", "strength": "weight-training today", "rest": "rest day today"}


# Monospace column padding for the <pre> day-table (right-pad label, left-pad numbers; truncate to width).
def _padR(s, n: int) -> str:
    return (str(s) + " " * n)[:n]


def _padL(s, n: int) -> str:
    return (" " * n + str(s))[-n:]


def _sum_macros(items: list[dict]) -> dict:
    return {k: sum(i.get(k) or 0 for i in items) for k in _MK}


_DAY_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")   # the 5 macros shown in the day table


# "Around your run" / "Around your strength" header, from the day's workout_label.
def _fuel_header(workout_label) -> str:
    return "Around your strength" if "strength" in (workout_label or "").lower() else "Around your run"


# Day-order for the "📊 Your day" per-meal rows (mirrors the dashboard today.js); unknown types sort last.
_MEAL_ORDER = ("breakfast", "brunch", "lunch", "snack", "pre_workout", "post_workout", "dinner", "supper")


def _meal_label(mt: str) -> str:
    return str(mt or "other").replace("_", " ").title()        # "post_workout" -> "Post Workout"


# {meal_type: macros} -> ordered [(label, macros)] rows for the day table (Breakfast / Lunch / Snack / …).
def _meal_rows(by_meal: dict) -> list:
    items = list((by_meal or {}).items())
    items.sort(key=lambda kv: (_MEAL_ORDER.index(kv[0]) if kv[0] in _MEAL_ORDER else len(_MEAL_ORDER), kv[0]))
    return [(_meal_label(mt), m) for mt, m in items]


# One aligned monospace row for the day box: label + kcal·P·C·F·Fib.
def _day_row(label: str, m: dict, W: int = 13) -> str:
    g = lambda k: round(m.get(k) or 0)  # noqa: E731
    return (_padR(label, W) + _padL(g("kcal"), 6) + _padL(g("protein_g"), 5)
            + _padL(g("carbs_g"), 5) + _padL(g("fat_g"), 5) + _padL(g("fibre_g"), 5))


# The floor (minimum to hit) of a macro target, for the Target-line headline: target -> low -> min.
def _tfloor(td) -> float:
    if isinstance(td, dict):
        for k in ("target", "low", "min"):
            if td.get(k) is not None:
                return td[k]
    return 0


# The LOWER bound of a macro target, for the still-to-eat math: low -> min -> target. B 2026-06-28:
# still-to-eat counts down to the BOTTOM of the band, not the middle (vs _tfloor, which prefers target).
def _tlow(td) -> float:
    if isinstance(td, dict):
        for k in ("low", "min", "target"):
            if td.get(k) is not None:
                return td[k]
    return 0


# Target DISPLAY for a non-kcal macro: "low–high" when a real range exists (protein), else the single
# value (fat = its min, fibre = its target).
def _trange(td) -> str:
    if isinstance(td, dict):
        lo, hi = td.get("low"), td.get("high")
        if lo is not None and hi is not None and round(lo) != round(hi):
            return f"{round(lo)}–{round(hi)}"
        for k in ("target", "min", "low"):
            if td.get(k) is not None:
                return str(round(td[k]))
    return "0"


# The shared hybrid day block (Order Card + own-food guide): an aligned <pre> box (component rows + a
# rule + "Total so far"; an empty day shows just the Total line) THEN a blank line
# and bold "🎯 Target" + "▸ Still to eat" lines OUTSIDE the box (Telegram can't bold inside <pre>). No
# "📊 Your day" title (B 2026-06-28 — the box is self-evident). Columns kcal·P·F·Fib (carbs flexible).
# `target` is the RAW range structure ({kcal:{low,target,high}, protein_g:{low,high}, fat_g:{min},
# fibre_g:{target,stretch}}): the Target line shows the kcal middle + (low–high) and the range where one
# exists (protein low–high; fat min; fibre target). Still-to-eat counts down to the LOW (bottom of the
# band) for EVERY macro (B 2026-06-28); for kcal, once total reaches the low it's "0 kcal" and "over" only
# triggers past the TOP of the band (high) — inside the band reads "0 kcal", not over. Protein/fat/fibre =
# low - total, clamped >= 0, never "over". Columns kcal·P·C·F·Fib (carbs shown; still a flexible guide).
# Input: rows = [(label, macros)], total, target.
def _day_block(rows: list, total: dict, target: dict) -> str:
    W = 13
    hdr = _padR("", W) + _padL("kcal", 6) + _padL("P", 5) + _padL("C", 5) + _padL("F", 5) + _padL("Fib", 5)
    if rows:                                            # per-meal rows -> rule -> Total. B 2026-06-28:
        box = ([hdr] + [_day_row(lbl, m, W) for lbl, m in rows]      # always break down, even one meal
               + ["─" * len(hdr), _day_row("Total so far", total, W)])
    else:                                               # nothing logged yet -> just the Total line
        box = [hdr, _day_row("Total so far", total, W)]
    g = lambda d, k: round(d.get(k) or 0)  # noqa: E731
    tk = target.get("kcal") or {}
    klo, khi = tk.get("low"), tk.get("high")
    kcal_tgt = f"{round(_tfloor(tk))} kcal" + (
        f" ({round(klo)}–{round(khi)})" if klo is not None and khi is not None and round(klo) != round(khi) else "")
    # kcal still-to-eat counts down to the LOW (bottom of the band); once total reaches the low it's 0,
    # and "over" only triggers past the TOP of the band (high) — inside the band is "0 kcal", not over
    # (B 2026-06-28: still-to-eat uses the lower bound of the range, not the middle).
    low_k, tot_k = round(_tlow(tk)), g(total, "kcal")
    hi_k = round(tk["high"]) if tk.get("high") is not None else low_k
    if tot_k <= low_k:
        kcal_txt = f"{low_k - tot_k} kcal"
    elif tot_k <= hi_k:
        kcal_txt = "0 kcal"
    else:
        kcal_txt = f"0 kcal <i>({tot_k - hi_k} over)</i>"
    still = lambda k: max(0, round(_tlow(target.get(k) or {})) - g(total, k))  # noqa: E731
    return "\n".join([
        f"<pre>{chr(10).join(box)}</pre>",
        "",
        f"<b>🎯 Target · {kcal_tgt}</b> · "
        f"{_trange(target.get('protein_g') or {})}P | {_trange(target.get('carbs_g') or {})}C | "
        f"{_trange(target.get('fat_g') or {})}F | {_trange(target.get('fibre_g') or {})}fib",
        "",
        f"<b>Still to eat · {kcal_txt}</b> · {still('protein_g')}P | {still('carbs_g')}C | "
        f"{still('fat_g')}F | {still('fibre_g')}fib",
    ])


# The shared "your day so far" block (per-meal box + Target + Still-to-eat), built from a plan_meals
# result. Reused by the own-food guide AND the all_eaten / at_limit cards so B can always see where her
# day stands vs target — even when there's nothing left to order (B 2026-06-28).
def _day_table(result: dict) -> str:
    eaten = result.get("eaten") or {}
    reserved = result.get("reserved") or {}
    sofar = {k: (eaten.get(k) or 0) + (reserved.get(k) or 0) for k in _MK}
    day_rows = _meal_rows(result.get("eaten_by_meal"))          # Breakfast / Lunch / Snack / … logged so far
    if any((reserved.get(k) or 0) for k in _DAY_KEYS):          # add the fuel row only when there IS fuel
        day_rows.append((result.get("workout_label") or "Run food", reserved))
    return _day_block(day_rows, sofar, result.get("target_macros") or {})


# Renders a plan_meals result. Returns (message_text, reply_markup). reply_markup is the ✓ Ate keyboard
# for a 'planned' card, else None. HTML (copy boxes + table); dynamic content is escaped.
def render_meal_card(result: dict) -> tuple:
    status = result.get("status")
    # all_eaten (both meals logged) / at_limit (no budget left): nothing more to order, but STILL show the
    # day table so B can see where she landed vs target (B 2026-06-28).
    if status == "all_eaten":
        return ("<b>✓ Both meals logged today</b> 🎉\n\n" + _day_table(result)
                + "\n<i>P · F are daily minimums · carbs flexible</i>"), None
    if status == "compose_failed":
        return "⚠️ Couldn't put a meal together just now — tap 🍽️ Meal again in a moment.", None
    if status == "own_food":
        return render_suggest(result), None
    if status == "at_limit":
        slots = " + ".join(result.get("slots_to_plan") or []) or "the rest of today"
        staples = "(2 boiled eggs · 150g greek yoghurt · edamame)"
        if result.get("shop_unavailable"):        # shop closed + couldn't swap -> say so, don't imply "at budget"
            note = "; ".join(result.get("report") or []) or "No shop to order from today"
            head = f"<b>🍽️ {_esc(note)}</b> — home food for {slots} {staples}."
        else:
            head = f"<b>🍽️ Basically at your budget for today</b> — skip {slots}, or a light staple {staples}."
        return (head + "\n\n" + _day_table(result)
                + "\n<i>P · F are daily minimums · carbs flexible</i>"), None

    # status == 'planned' -> the Order Card
    slots = result.get("slots", {})
    day_label = _DAY_TYPE_LABEL.get(result.get("day_type"), "today")
    lines = [f"<b>Order from</b> · <i>{_esc(day_label)}</i>", f"<pre>{_esc(result.get('shop'))}</pre>"]
    shop_th = _SHOP_TH.get(result.get("shop"))
    if shop_th:                                                  # WongNai: Thai title below the English box
        lines.append(f"<pre>{_esc(shop_th)}</pre>")
    lines.append("")
    meal_btns, staple_btns = [], []

    first_slot = True
    for slot in ("lunch", "dinner"):
        items = slots.get(slot)
        if not items:
            continue
        if not first_slot:
            lines.append("")                                    # blank line between Lunch and Dinner
        first_slot = False
        mains = [i for i in items if i.get("role") != "staple"]
        staples = [i for i in items if i.get("role") == "staple"]
        m = _sum_macros(items)                                   # slot macros (incl staples you eat)
        price = round(sum(i.get("price_thb") or 0 for i in items))  # staples carry no ฿
        lines.append(f"<b>{slot.capitalize()}</b> · {round(m['kcal'])} kcal · "
                     f"{round(m['protein_g'])}P · {round(m['carbs_g'])}C · {round(m['fat_g'])}F · <b>฿{price}</b>")
        lines.append(f"<pre>{_esc(', '.join(i['item_name'] for i in mains)) or '—'}</pre>")  # dishes to order
        # If any main is Thai script, add a SECOND copy box below with the English glosses (name_en),
        # so Thai stays cleanly copyable on top and B can read the English underneath.
        if any(_is_thai(i["item_name"]) and i.get("name_en") for i in mains):
            en = ", ".join((i.get("name_en") or i["item_name"]) for i in mains)
            lines.append(f"<pre>{_esc(en)}</pre>")               # English, below the Thai
        if staples:                                             # e.g. "+ edamame 150 g · 2× boiled_egg (home)"
            lines.append("+ " + " · ".join(_esc(solver.staple_label(s)) for s in staples) + " <i>(home)</i>")
        for idx, mn in enumerate(mains):                        # one ✓ button per main dish (B 2026-06-28)
            meal_btns.append({"text": f"✓ Ate {solver.dish_label(mn)}",
                              "callback_data": f"meal_ate:d:{slot}:{idx}"})
        for s in staples:
            staple_btns.append({"text": f"✓ {solver.staple_label(s)}",
                                "callback_data": f"meal_ate:s:{slot}:{s['item_name']}"})

    total_cost = round(sum((i.get("price_thb") or 0) for it in slots.values() for i in it))
    lines += ["", f"<b>฿{total_cost} total</b>"]

    fuel = result.get("fuel_items") or []
    if fuel:
        lines += ["", f"<b>{_fuel_header(result.get('workout_label'))}</b> <i>· not eaten yet</i>",
                  _esc(" · ".join(fuel))]

    reserved = result.get("reserved") or {}
    by_meal = dict(result.get("eaten_by_meal") or {})            # logged so far, by meal type
    for slot in ("lunch", "dinner"):                             # + the meals being ordered now
        if slots.get(slot):
            by_meal[slot] = _sum_macros(slots[slot])
    day_rows = _meal_rows(by_meal)
    if any((reserved.get(k) or 0) for k in _DAY_KEYS):
        day_rows.append((result.get("workout_label") or "Run food", reserved))
    lines += ["", _day_block(day_rows, result.get("projected") or {}, result.get("target_macros") or {})]
    if result.get("report"):
        lines.append(f"<b>⚠️</b> {_esc('; '.join(result['report']))}")
    if result.get("note"):
        lines.append(f"<b>📝</b> <i>{_esc(result['note'])}</i>")
    if result.get("week_refit"):          # future days re-balanced after a shop swap (silent DB; note here)
        n = result["week_refit"]
        lines.append(f"<i>↻ Re-balanced this week's shops · {n} day{'' if n == 1 else 's'} updated</i>")

    rows = [[b] for b in meal_btns] + [staple_btns[i:i + 2] for i in range(0, len(staple_btns), 2)]
    return "\n".join(lines), ({"inline_keyboard": rows} if rows else None)


# Spec G — eating-on-your-own guide (own-food/weekend days), suggest2 redesign. HTML: a one-line header
# (bold title · italic day-type), the shared hybrid day block (_day_block — aligned box of PER-MEAL rows
# [+ workout fuel] -> Total so far, then bold Target [with ranges] + Still-to-eat lines), then the
# prioritise + home-staples lines and the activity-aware "Around your run/strength" fuel line. No rows
# written, no buttons.
def render_suggest(result: dict) -> str:
    lines = [
        f"<b>🍴 Eating on your own</b> · <i>{_esc(result.get('day_type') or 'rest')} day</i>",
        "",
        _day_table(result),
        "<i>P · F are daily minimums · carbs flexible</i>",
        "",
    ]
    owed = result.get("owed") or []
    if owed:
        pretty = ", ".join(o.replace("_", " ").upper() for o in owed)
        lines.append(f"<b>🥩 Prioritise</b> {_esc(pretty)} <i>(still owed this week)</i>")
    if result.get("staples"):
        lines.append("<b>🏠 Home staples</b> · 2–3 boiled eggs · 150g greek yoghurt · edamame")
    fuel = result.get("fuel_items") or []
    if fuel:
        lines += ["", f"<b>{_fuel_header(result.get('workout_label'))}</b> <i>· not eaten yet</i>",
                  _esc(" · ".join(fuel))]
    return "\n".join(lines)
