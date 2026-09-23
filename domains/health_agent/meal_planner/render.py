"""
Renders meal plans, own-food guidance, and Singapore staple suggestions for Telegram.

Shop and dish names use copyable HTML blocks. The daily table shows recorded calories, protein,
carbohydrate, fat, and fibre, while its target lines show only calories and protein. Dynamic text is
escaped before it is inserted into HTML.

Functions:
  _padR — pads a table label on the right
  _padL — pads a table value on the left
  _sum_macros — totals nutrition values across items
  _fuel_header — builds the workout-food heading
  _meal_label — formats a meal type for display
  _meal_rows — builds ordered meal rows for the daily table
  _day_row — formats one daily-table row
  _tfloor — reads the main calorie target value
  _tlow — reads a target's lower bound
  _trange — formats a protein target range
  _day_block — builds the daily nutrition summary
  _day_table — wraps the shared nutrition summary for cards
  render_meal_card — renders a planned or unavailable meal state
  render_suggest — renders guidance for a day without shop planning
  render_staple_topup — renders Singapore staple suggestions
"""

from domains.health_agent.meal_planner import solver
from system.text import esc as _esc, is_thai as _is_thai

_MK = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")   # macro keys

# Thai WongNai listing names keyed by the canonical restaurant name.
_SHOP_TH = {
    "Freshies Clean Ketogenic": "Freshies Clean Ketogenic วงศ์สว่าง",
    "KIN Healthy": "KIN Healthy อาหารคลีน ประชาชื่น",
    "Deelizz On Table": "อาหารคลีน ตามสั่ง Deelizz On Table ประชาชื่น 39",
    "Budder Clean Food": "Budder อาหารคลีนโคตรอร่อย สะพานควาย",
    "FitFish": "FitFish ปลาย่าง อาหารคลีน",
    "Chicken Breast Kitchen": "อกไก่ Kitchen อาหารคลีน",
    "Leanlicious": "Leanlicious อาหารคลีน เดอะมอลล์ งามวงศ์วาน",
}

# Pads or truncates a label to a fixed width. Returns the formatted string.
def _padR(s, n: int) -> str:
    return (str(s) + " " * n)[:n]


# Left-pads a value to a fixed width. Returns the formatted string.
def _padL(s, n: int) -> str:
    return (" " * n + str(s))[-n:]


# Adds nutrition values across a list of meal items. Returns one total per tracked value.
def _sum_macros(items: list[dict]) -> dict:
    return {k: sum(i.get(k) or 0 for i in items) for k in _MK}


_DAY_KEYS = ("kcal", "protein_g", "carbs_g", "fat_g", "fibre_g")   # the 5 macros shown in the day table


# Returns the workout-food heading from the stored workout label.
def _fuel_header(workout_label) -> str:
    return "Around your strength" if "strength" in (workout_label or "").lower() else "Around your run"


# Display order for meal rows. Unknown meal types sort last.
_MEAL_ORDER = ("breakfast", "brunch", "lunch", "snack", "pre_workout", "post_workout", "dinner", "supper")


# Formats a meal type such as post_workout as a readable label.
def _meal_label(mt: str) -> str:
    return str(mt or "other").replace("_", " ").title()        # "post_workout" -> "Post Workout"


# Converts meal totals into ordered rows for the daily table.
def _meal_rows(by_meal: dict) -> list:
    items = list((by_meal or {}).items())
    items.sort(key=lambda kv: (_MEAL_ORDER.index(kv[0]) if kv[0] in _MEAL_ORDER else len(_MEAL_ORDER), kv[0]))
    return [(_meal_label(mt), m) for mt, m in items]


# Formats one row of the daily nutrition table.
def _day_row(label: str, m: dict, W: int = 13) -> str:
    g = lambda k: round(m.get(k) or 0)  # noqa: E731
    return (_padR(label, W) + _padL(g("kcal"), 6) + _padL(g("protein_g"), 5)
            + _padL(g("carbs_g"), 5) + _padL(g("fat_g"), 5) + _padL(g("fibre_g"), 5))


# Returns the main display value from a target definition.
def _tfloor(td) -> float:
    if isinstance(td, dict):
        for k in ("target", "low", "min"):
            if td.get(k) is not None:
                return td[k]
    return 0


# Returns the lower bound used for the remaining amount.
def _tlow(td) -> float:
    if isinstance(td, dict):
        for k in ("low", "min", "target"):
            if td.get(k) is not None:
                return td[k]
    return 0


# Formats the protein range for the target line.
def _trange(td) -> str:
    if isinstance(td, dict):
        lo, hi = td.get("low"), td.get("high")
        if lo is not None and hi is not None and round(lo) != round(hi):
            return f"{round(lo)}–{round(hi)}"
        for k in ("target", "min", "low"):
            if td.get(k) is not None:
                return str(round(td[k]))
    return "0"


# Builds the daily nutrition table and the calorie and protein target lines.
def _day_block(rows: list, total: dict, target: dict) -> str:
    W = 13
    hdr = _padR("", W) + _padL("kcal", 6) + _padL("P", 5) + _padL("C", 5) + _padL("F", 5) + _padL("Fib", 5)
    if rows:
        box = ([hdr] + [_day_row(lbl, m, W) for lbl, m in rows]
               + ["─" * len(hdr), _day_row("Total so far", total, W)])
    else:
        box = [hdr, _day_row("Total so far", total, W)]
    g = lambda d, k: round(d.get(k) or 0)  # noqa: E731
    tk = target.get("kcal") or {}
    klo, khi = tk.get("low"), tk.get("high")
    kcal_tgt = f"{round(_tfloor(tk))} kcal" + (
        f" ({round(klo)}–{round(khi)})" if klo is not None and khi is not None and round(klo) != round(khi) else "")
    # Remaining calories reach zero at the lower bound; "over" starts above the upper bound.
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
        f"<b>🎯 Target · {kcal_tgt}</b> · {_trange(target.get('protein_g') or {})}P",
        "",
        f"<b>Still to eat · {kcal_txt}</b> · {still('protein_g')}P",
    ])


# Builds the daily nutrition block shared by all meal-card states.
def _day_table(result: dict) -> str:
    eaten = result.get("eaten") or {}
    reserved = result.get("reserved") or {}
    sofar = {k: (eaten.get(k) or 0) + (reserved.get(k) or 0) for k in _MK}
    day_rows = _meal_rows(result.get("eaten_by_meal"))
    if any((reserved.get(k) or 0) for k in _DAY_KEYS):
        day_rows.append((result.get("workout_label") or "Run food", reserved))
    return _day_block(day_rows, sofar, result.get("target_macros") or {})


# Renders a meal-planning result and returns its text and optional buttons.
def render_meal_card(result: dict) -> tuple:
    status = result.get("status")
    if status == "all_eaten":
        return ("<b>✓ Both meals logged today</b> 🎉\n\n" + _day_table(result)
                + "\n<i>calories + protein are the goals · carbs, fat and fibre are flexible</i>"), None
    if status == "compose_failed":
        return "⚠️ Couldn't put a meal together just now — tap 🍽️ Meal again in a moment.", None
    if status == "own_food":
        return render_suggest(result), None
    if status == "staple_topup":
        return render_staple_topup(result), None
    if status == "at_limit":
        slots = " + ".join(result.get("slots_to_plan") or []) or "the rest of today"
        staples = "(2 boiled eggs · 150g greek yoghurt · edamame)"
        if result.get("shop_unavailable"):        # shop closed + couldn't swap -> say so, don't imply "at budget"
            note = "; ".join(result.get("report") or []) or "No shop to order from today"
            head = f"<b>🍽️ {_esc(note)}</b> — home food for {slots} {staples}."
        else:
            head = f"<b>🍽️ Basically at your budget for today</b> — skip {slots}, or a light staple {staples}."
        return (head + "\n\n" + _day_table(result)
                + "\n<i>calories + protein are the goals · carbs, fat and fibre are flexible</i>"), None

    # A planned result includes the shop, dishes, nutrition table, and logging buttons.
    slots = result.get("slots", {})
    lines = ["<b>Order from</b>", f"<pre>{_esc(result.get('shop'))}</pre>"]
    shop_th = _SHOP_TH.get(result.get("shop"))
    if shop_th:
        lines.append(f"<pre>{_esc(shop_th)}</pre>")
    lines.append("")
    meal_btns, staple_btns = [], []

    first_slot = True
    for slot in ("lunch", "dinner"):
        items = slots.get(slot)
        if not items:
            continue
        if not first_slot:
            lines.append("")
        first_slot = False
        mains = [i for i in items if i.get("role") != "staple"]
        staples = [i for i in items if i.get("role") == "staple"]
        m = _sum_macros(items)
        price = round(sum(i.get("price_thb") or 0 for i in items))
        lines.append(f"<b>{slot.capitalize()}</b> · {round(m['kcal'])} kcal · "
                     f"{round(m['protein_g'])}P · {round(m['carbs_g'])}C · {round(m['fat_g'])}F · <b>฿{price}</b>")
        lines.append(f"<pre>{_esc(', '.join(i['item_name'] for i in mains)) or '—'}</pre>")
        # Add English names below Thai dish names when available.
        if any(_is_thai(i["item_name"]) and i.get("name_en") for i in mains):
            en = ", ".join((i.get("name_en") or i["item_name"]) for i in mains)
            lines.append(f"<pre>{_esc(en)}</pre>")
        if staples:
            lines.append("+ " + " · ".join(_esc(solver.staple_label(s)) for s in staples) + " <i>(home)</i>")
        for idx, mn in enumerate(mains):
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
    by_meal = dict(result.get("eaten_by_meal") or {})
    for slot in ("lunch", "dinner"):
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
    if result.get("week_refit"):
        n = result["week_refit"]
        lines.append(f"<i>↻ Re-balanced this week's shops · {n} day{'' if n == 1 else 's'} updated</i>")

    rows = [[b] for b in meal_btns] + [staple_btns[i:i + 2] for i in range(0, len(staple_btns), 2)]
    return "\n".join(lines), ({"inline_keyboard": rows} if rows else None)


# Renders guidance for a day without a shop meal plan. No rows are written and no buttons are shown.
def render_suggest(result: dict) -> str:
    lines = [
        "<b>🍴 Eating on your own</b>",
        "",
        _day_table(result),
        "<i>calories + protein are the goals · carbs, fat and fibre are flexible</i>",
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


# Renders a Singapore home-staple suggestion for the remaining protein.
def render_staple_topup(result: dict) -> str:
    lines = [
        "<b>🏠 Fridge top-up</b>",
        "",
        _day_table(result),
        "<i>calories + protein are the goals · carbs, fat and fibre are flexible</i>",
        "",
    ]
    topup = result.get("topup") or []
    if topup:
        picks = " · ".join(_esc(solver.staple_label(s)) for s in topup)
        add = result.get("topup_added") or {}
        lines.append(f"<b>➕ Top up with</b> · {picks}")
        lines.append(f"<i>adds ~{round(add.get('protein_g') or 0)}P · "
                     f"{round(add.get('kcal') or 0)} kcal</i>")
    else:
        rem = result.get("remaining") or {}
        prot_gap = round((rem.get("protein_g") or {}).get("low") or 0)
        if prot_gap:
            lines.append(f"<b>⚠️ No calorie room to top up</b> — still short {prot_gap}P, "
                         "but you're at today's calorie ceiling.")
        else:
            lines.append("<b>✓ No fridge top-up needed</b> — protein is covered.")
    fuel = result.get("fuel_items") or []
    if fuel:
        lines += ["", f"<b>{_fuel_header(result.get('workout_label'))}</b> <i>· not eaten yet</i>",
                  _esc(" · ".join(fuel))]
    return "\n".join(lines)
