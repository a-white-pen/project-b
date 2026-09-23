"""Builds and renders the weekly reflection message.

Functions:
  render_weekly_reflection — returns the Telegram message
  _spend_line — formats weekly meal spending
  _muscle_summary — summarizes strength load and volume changes
  _fish_note — builds the fish-frequency note
  _rotation_status — builds protein-rotation status
  _meal_spend — calculates meal budget and spending
  assemble_reflection_data — combines goal reads, configuration, and narrative text
"""

from domains.health_agent.goals import mode_config
from domains.health_agent.meal_planner.solver import owed_proteins
from domains.health_agent.weekly_reflection import goal_progress as gp
from system.text import esc as _esc


# Renders a weekly reflection and omits sections with no useful data.
def render_weekly_reflection(data: dict) -> str:
    lines = [f"<b>📊 Week {data['week_num']} · weekly check-in</b>", ""]

    # Adds training, nutrition, and weight-reference status.
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

    nutrition = data.get("nutrition") or {}
    if nutrition:
        kcal = nutrition.get("kcal") or {}
        protein = nutrition.get("protein_g") or {}
        fibre = data.get("fibre_reference") or {}
        lines.append(
            f"🍽️ <b>nutrition</b> — {int(kcal.get('low', 0)):,}–{int(kcal.get('high', 0)):,} kcal"
            f" · {int(protein.get('low', 0))}–{int(protein.get('high', 0))}g protein"
            f" · fibre reference {int(fibre.get('target', 0))}g"
        )

    weight_reference = data.get("weight_reference") or {}
    if weight_reference.get("band_label"):
        low, high = weight_reference.get("band_kg") or (54, 56)
        lines.append(f"⚖️ <b>{low:g}–{high:g} kg reference</b> — "
                     f"{_esc(weight_reference['band_label'])}")
    lines.append("")

    # Adds configured food habits and meal spending when available.
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

    # Escapes the stored narrative before showing it in the HTML message.
    narrative = data.get("narrative")
    if narrative:
        lines.append("")
        lines.append(f"<i>{_esc(narrative)}</i>")

    return "\n".join(lines)


# Formats planned and eaten meal spending against the budget for eaten meals.
def _spend_line(spend: dict) -> str:
    if not spend.get("eaten"):
        return f"💰 <b>0/{spend.get('planned', 0)} meals</b> · <i>none eaten yet</i>"
    d = spend.get("delta_sgd", 0)
    tag = f"S${abs(d)} over" if d > 0 else (f"S${abs(d)} under" if d < 0 else "on budget")
    return (f"💰 <b>{spend['eaten']}/{spend['planned']} meals</b> · budget "
            f"<b>S${spend['budget_sgd']}</b> · spent <b>S${spend['spent_sgd']}</b> <i>({tag})</i>")


# Builds a short strength progress summary from load and volume changes.
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


# Builds the fish-frequency note used in the reflection prompt.
def _fish_note(fish_count):
    return "no fish yet" if not fish_count else f"fish {fish_count}×"


# Builds the protein-rotation status using each protein's configured time window.
def _rotation_status(cfg: dict, tally_1wk: dict, tally_2wk: dict) -> list:
    cfg = cfg or {}
    is_2wk = {p: ("2wk" in str(s)) for p, s in cfg.items()}
    owed = set(owed_proteins(tally_1wk or {}, {p: s for p, s in cfg.items() if not is_2wk[p]}))
    owed |= set(owed_proteins(tally_2wk or {}, {p: s for p, s in cfg.items() if is_2wk[p]}))
    return [(p, (tally_2wk if is_2wk[p] else (tally_1wk or {})).get(p, 0), p not in owed, is_2wk[p])
            for p in cfg]


# Calculates the weekly meal budget and spending for eaten planned meals.
# Returns None when no meals were planned.
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


# Combines computed weekly facts, goals, optional nudges, and narrative for rendering.
def assemble_reflection_data(week_num, now_avg7, goal_inputs, goals,
                             directives=None, narrative=None) -> dict:
    directives = directives or {}
    nutrition = goals["nutrition"]["standard_day_target"]
    fibre_reference = goals["nutrition"].get("soft_goals", {}).get("fibre_g", {})
    band = goals["weight_tracking"]["reference_band_kg"]
    mc = goals.get("meal_constraints", {})
    eggs_target = mc.get("eggs_min", 10)
    spend = _meal_spend(goal_inputs.get("meals"), mc)
    # Shows protein rotation only where the active location uses shop meal planning.
    if mode_config().get("b_extended_plans_meals", True):
        rotation = _rotation_status(goals.get("meal_constraints", {}).get("protein_rotation", {}),
                                    goal_inputs.get("protein_1wk") or {}, goal_inputs.get("protein_2wk") or {})
        fish_note = _fish_note(goal_inputs.get("fish_count", 0))
    else:
        rotation, fish_note = [], None

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
        "run": run_block,
        "muscle": muscle_block,
        "nutrition": nutrition,
        "fibre_reference": fibre_reference,
        "weight_reference": {"band_label": wg["label"], "band_kg": band},
        "eggs": {"count": goal_inputs.get("eggs", 0), "target": eggs_target},
        "fish_note": fish_note,
        "rotation": rotation,
        "spend": spend,
        "narrative": narrative,
    }
