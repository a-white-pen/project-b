"""
Renders the weekly reflection (spec H) as the Telegram message.

PURE: takes a computed reflection dict (the service assembles it from calibration + goal_progress +
DB reads + the LLM narrative) and returns the plain-text message. No HTML tags (emoji + symbols
only), so no escaping is needed. Missing/None fields degrade gracefully — notably the run line shows
"no quality run logged yet" when B has no quality/fartlek run on record (per the Riegel scope rule).

Functions:
  assemble_reflection_data(...) -> dict   # build the render dict from calibration + goal reads + cfg
  render_weekly_reflection(data) -> str
"""

from domains.health_agent.meal_planner.solver import owed_proteins
from domains.health_agent.weekly_reflection import goal_progress as gp
from system.text import esc as _esc


# Renders the spec-H weekly reflection from a computed data dict (shape documented in the tests).
# Degrades gracefully: no quality run -> "no quality run logged yet"; absent muscle/eggs/fish/budget
# lines are skipped. Output: the plain-text Telegram message string.
def render_weekly_reflection(data: dict) -> str:
    lines = [f"<b>📊 Week {data['week_num']} · weekly check-in</b>", ""]

    # ⚖️ Weight section
    w = data.get("weight") or {}
    wg = data.get("weight_goal") or {}
    if w.get("now") is not None:
        lines.append("<b>⚖️ Weight</b>")
        prev = w.get("prev")
        head = (f"<b>{prev:.1f} → {w['now']:.1f} kg</b>" if prev is not None
                else f"<b>{w['now']:.1f} kg</b>")
        if w.get("trend_kg") is not None:
            head += f"  ·  3-wk trend <b>{w['trend_kg']:+.1f}/wk</b>"
            if w.get("trend_word"):
                head += f" · {w['trend_word']}"
        lines.append(head)
        band = wg.get("band_label")
        lines.append(f"{(_esc(band) + ' · ') if band else ''}maintenance ~{data['maintenance']}")
        tgt = f"next week target <b>~{data['target']}</b>"
        if data.get("direction"):
            tgt += f" <i>({_esc(str(data['direction']).split(' — ')[0])})</i>"   # short form, e.g. (gentle cut)
        lines.append(tgt)
        lines.append("")

    # 🎯 Goals
    lines.append("<b>🎯 Goals</b>")
    run = data.get("run")
    if run:
        seg = f"🏃 <b>sub-60 10k</b> — est. ~{_esc(run['est_label'])}"
        if run.get("min_to_go"):
            seg += f" · ~{run['min_to_go']} min to go"
        lines.append(seg)
        if run.get("directive"):
            lines.append(f"       <i>→ {_esc(run['directive'])}</i>")
    else:
        lines.append("🏃 <b>sub-60 10k</b> — <i>no quality run logged yet</i>")

    muscle = data.get("muscle")
    if muscle and muscle.get("summary"):
        seg = f"💪 <b>build muscle</b> — {_esc(muscle['summary'])}"
        if muscle.get("status"):
            seg += f" <i>· {_esc(muscle['status'])}</i>"
        lines.append(seg)
    else:
        lines.append("💪 <b>build muscle</b> — <i>no strength logged yet</i>")

    if wg.get("band_label"):
        seg = f"⚖️ <b>54–56 band</b> — {_esc(wg['band_label'])}"
        if wg.get("trend_kg") is not None:
            seg += f" · {wg['trend_kg']:+.1f}/wk"
        lines.append(seg)
    lines.append("")

    # 🍽️ Habits — the eggs chip, the meal spend-vs-budget line, then the protein rotation (incl. fish),
    # each on its own line below.
    habits = []
    eggs = data.get("eggs")
    if eggs and eggs.get("target") is not None:
        habits.append(f"<b>🥚 {eggs.get('count', 0)}/{eggs['target']} eggs</b>")
    spend = data.get("spend")
    rotation = data.get("rotation")
    if habits or spend or rotation:
        lines.append("<b>🍽️ Habits</b>")
        if habits:
            lines.append("  ·  ".join(habits))
        if spend:
            lines.append(_spend_line(spend))
        if rotation:
            parts = [f"{p} {cnt} {'✓' if ok else '✗'}" + (" (2wk)" if is2 else "")
                     for p, cnt, ok, is2 in rotation]
            lines.append("<b>🥩 " + " · ".join(parts) + "</b>")

    # The LLM coach's note (also stored in weekly_reflections.narrative). Closing italic paragraph;
    # escaped for HTML.
    narrative = data.get("narrative")
    if narrative:
        lines.append("")
        lines.append(f"<i>{_esc(narrative)}</i>")

    return "\n".join(lines)


# The meal spend-vs-budget line (Habits): "💰 8/10 meals · budget S$52 · spent S$58 (S$6 over)".
# Budget is on the meals B ACTUALLY ATE (eaten × per-meal budget); spend is the PLAN-LINKED menu cost
# of those meals (THB→SGD). delta_sgd > 0 = over, < 0 = under, 0 = on budget. No meals eaten yet ->
# "none eaten yet" (a 0-vs-0 "on budget" would read wrong).
def _spend_line(spend: dict) -> str:
    if not spend.get("eaten"):
        return f"💰 <b>0/{spend.get('planned', 0)} meals</b> · <i>none eaten yet</i>"
    d = spend.get("delta_sgd", 0)
    tag = f"S${abs(d)} over" if d > 0 else (f"S${abs(d)} under" if d < 0 else "on budget")
    return (f"💰 <b>{spend['eaten']}/{spend['planned']} meals</b> · budget "
            f"<b>S${spend['budget_sgd']}</b> · spent <b>S${spend['spent_sgd']}</b> <i>({tag})</i>")


# Word for the weight trend: |x| < 0.15/wk reads "flat" (week-to-week is water-dominated); else dir.
def _trend_word(trend_kg):
    if trend_kg is None:
        return ""
    if abs(trend_kg) < 0.15:
        return "flat"
    return "falling" if trend_kg < 0 else "rising"


# The calorie-direction note from next-week target vs maintenance + the band mid.
def _direction(target, maintenance, band_mid):
    if target < maintenance:
        return f"gentle cut — above mid {band_mid:g}"
    if target > maintenance:
        return f"gentle gain — below mid {band_mid:g}"
    return "holding — in band"


# Short build-muscle summary from the volume/load deltas, e.g. "RDL +2.5 kg, volume +8%". None when
# there's nothing to report (-> the render shows "no strength logged yet").
def _muscle_summary(deltas):
    parts = []
    gainers = deltas.get("top_gainers") or []
    if gainers:
        top = gainers[0]
        parts.append(f"{top['exercise']} +{top['delta_kg']:g} kg")
    pct = deltas.get("volume_delta_pct")
    if pct is not None:
        parts.append(f"volume {pct:+g}%")
    return ", ".join(parts) if parts else None


# The fish-rotation note (spec H "no fish yet") — still fed to the LLM prompt.
def _fish_note(fish_count):
    return "no fish yet" if not fish_count else f"fish {fish_count}×"


# Per-protein rotation status for the Habits line, matching the meal planner's "owed" logic. Each
# protein uses the right window: a "… per 2wk" spec (duck) reads the 2-week tally, the rest the Mon-Sun
# week. Reuses solver.owed_proteins so "achieved" matches the planner exactly.
# Output: [(protein, count, achieved_bool, is_2wk_bool)] in config order.
def _rotation_status(cfg: dict, tally_1wk: dict, tally_2wk: dict) -> list:
    cfg = cfg or {}
    is_2wk = {p: ("2wk" in str(s)) for p, s in cfg.items()}
    owed = set(owed_proteins(tally_1wk or {}, {p: s for p, s in cfg.items() if not is_2wk[p]}))
    owed |= set(owed_proteins(tally_2wk or {}, {p: s for p, s in cfg.items() if is_2wk[p]}))
    return [(p, (tally_2wk if is_2wk[p] else (tally_1wk or {})).get(p, 0), p not in owed, is_2wk[p])
            for p in cfg]


# Computes the meal spend-vs-budget block from this week's plan-linked meal tally + meal config.
# meals = {planned, eaten, spent_thb} (persistence.read_meal_spend) or None. Budget is on the meals
# B ATE (eaten × budget_sgd_per_meal); spend is the plan-linked menu cost (spent_thb / the flat
# planning fx, so budget + spend share one unit and compare directly). None when nothing was planned.
def _meal_spend(meals, meal_cfg: dict) -> dict | None:
    if not meals or not meals.get("planned"):
        return None
    per_meal = float(meal_cfg.get("budget_sgd_per_meal", 6.5))
    fx = float(meal_cfg.get("fx_thb_per_sgd_planning", 25)) or 25.0
    eaten = int(meals.get("eaten", 0))
    budget_sgd = round(eaten * per_meal)
    spent_sgd = round(float(meals.get("spent_thb", 0) or 0) / fx)
    return {"planned": int(meals["planned"]), "eaten": eaten,
            "budget_sgd": budget_sgd, "spent_sgd": spent_sgd,
            "delta_sgd": spent_sgd - budget_sgd}


# Builds the render dict (the deterministic spec-H structure) from the calibration result + the goal
# reads + goals config. PURE. The service merges the LLM's short directives via `directives`
# (e.g. {"run": "add 1 tempo/wk", "muscle_status": "on track"}).
# Inputs: ISO week number, CalibrationResult, current 7d-avg weight, read_goal_inputs() dict,
# the full goals dict, optional prior-week weight / SGD budget-left / LLM directives.
# Output: the dict render_weekly_reflection() consumes.
def assemble_reflection_data(week_num, calibration, now_avg7, goal_inputs, goals,
                             weight_prev=None, directives=None, narrative=None) -> dict:
    directives = directives or {}
    n = goals["nutrition"]
    band = n["weight_band_kg"]
    band_mid = n["band_mid_kg"]
    mc = goals.get("meal_constraints", {})
    eggs_target = mc.get("eggs_min", 10)
    spend = _meal_spend(goal_inputs.get("meals"), mc)
    rotation = _rotation_status(goals.get("meal_constraints", {}).get("protein_rotation", {}),
                                goal_inputs.get("protein_1wk") or {}, goal_inputs.get("protein_2wk") or {})

    run = goal_inputs.get("run")
    run_block = None
    if run:
        run_block = {"est_label": run["est_10k_label"], "min_to_go": run["min_to_go"],
                     "directive": directives.get("run")}

    muscle_summary = _muscle_summary(goal_inputs.get("muscle_deltas") or {})
    muscle_block = ({"summary": muscle_summary, "status": directives.get("muscle_status")}
                    if muscle_summary else None)

    wg = gp.band_position(now_avg7, band[0], band[1])

    return {
        "week_num": week_num,
        "weight": {"prev": weight_prev, "now": now_avg7,
                   "trend_kg": calibration.weight_trend_kg,
                   "trend_word": _trend_word(calibration.weight_trend_kg)},
        "maintenance": calibration.maintenance_kcal,
        "target": calibration.weekly_target_kcal,
        "direction": _direction(calibration.weekly_target_kcal, calibration.maintenance_kcal, band_mid),
        "run": run_block,
        "muscle": muscle_block,
        "weight_goal": {"band_label": wg["label"], "trend_kg": calibration.weight_trend_kg},
        "eggs": {"count": goal_inputs.get("eggs", 0), "target": eggs_target},
        "fish_note": _fish_note(goal_inputs.get("fish_count", 0)),
        "rotation": rotation,
        "spend": spend,
        "narrative": narrative,
    }
