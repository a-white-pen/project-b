"""
Week-scaffold brain. Gemini Pro PROPOSES a week shape; the deterministic pipeline here makes it real:
normalise -> enforce (week_planner.enforce — the hard floor) -> per-day macro_target
(calibration.compute_day_targets gives the day-split kcal, macros.build_macro_target the full target).
Code owns feasibility; the LLM only proposes (§2 enforcement boundary).

The post-LLM pipeline `assemble_week` (+ `_parse`/`_apply_pins`) is PURE + unit-tested; the LLM call
`plan_week` is the thin untestable glue (state.py provides its input).

Functions:
  assemble_week(proposed_days, weekly_target, weight_kg, cfg, rules) -> dict   # pure: enforce + macros
  plan_week(state, cfg, rules) -> dict                                         # LLM glue: propose -> assemble
"""

from datetime import date as _date

from domains.health_agent import calibration, macros
from domains.health_agent.week_planner import prompt as wk_prompt
from domains.health_agent.week_planner.enforce import enforce_week
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response

_KINDS = {"rest", "cardio", "strength", "other"}
_RUN_TYPES = {"easy", "long", "quality", "fartlek"}


def _is_rest(day) -> bool:
    return not [a for a in day["activity_type"] if a != "rest"]


# Validates one proposed day to the activity_type / run_type vocab; defaults the unknowns. Pure.
# run_type only survives on a cardio day. Output: a clean day dict (date/activity_type/run_type/
# strength_focus/is_vegetarian_day/note/locked).
def _normalise_day(day: dict) -> dict:
    at = [a for a in day.get("activity_type", []) if a in _KINDS] or ["rest"]
    rt = day.get("run_type") if day.get("run_type") in _RUN_TYPES else None
    if "cardio" not in at:
        rt = None
    return {
        "date": day["date"],
        "activity_type": at,
        "run_type": rt,
        "strength_focus": day.get("strength_focus"),
        "is_vegetarian_day": bool(day.get("is_vegetarian_day")),
        "note": day.get("note"),
        "locked": bool(day.get("locked")),
    }


# Picks (day_kcal, macro_day_type) for a day from the week's day_targets split.
# cardio (incl. a 2-a-day) -> cardio kcal (the bigger fuel); any strength -> the strength protein
# band; otherwise rest. Input: the day's activity_type + the {cardio,strength,rest} kcal dict.
def _macro_inputs(activity_type: list, day_targets: dict) -> tuple[int, str]:
    has_c = "cardio" in activity_type
    has_s = "strength" in activity_type
    kcal = day_targets["cardio"] if has_c else (day_targets["strength"] if has_s else day_targets["rest"])
    mt_type = "strength" if has_s else ("cardio" if has_c else "rest")
    return kcal, mt_type


def _summary(n_cardio: int, n_strength: int) -> str:
    return f"{n_strength} strength + {n_cardio} run{'s' if n_cardio != 1 else ''} this week"


# Turns the LLM's PROPOSED days into the enforced, macro-targeted canonical week. PURE (no LLM/DB).
# Input: proposed days (each {date, activity_type, run_type, strength_focus, is_vegetarian_day,
# locked?}), the weekly-avg target (from weekly_reflections), current bodyweight, the nutrition cfg,
# and the weekly_training rules. Output: {days, report, summary, day_counts}.
def assemble_week(proposed_days: list[dict], weekly_target: int, weight_kg: float | None,
                  cfg: dict, rules: dict, done_this_week: dict | None = None) -> dict:
    days = [_normalise_day(d) for d in proposed_days]
    enforced, report = enforce_week(days, rules, done_this_week)

    n_cardio = sum(1 for d in enforced if "cardio" in d["activity_type"])
    n_strength = sum(1 for d in enforced if "strength" in d["activity_type"])
    n_rest = sum(1 for d in enforced if _is_rest(d))

    day_targets = calibration.compute_day_targets(weekly_target, n_cardio, n_strength, n_rest, cfg)
    for d in enforced:
        kcal, mt_type = _macro_inputs(d["activity_type"], day_targets)
        d["macro_target"] = macros.build_macro_target(kcal, mt_type, weight_kg, cfg)

    return {
        "days": enforced,
        "report": report,
        "summary": _summary(n_cardio, n_strength),
        "day_counts": {"cardio": n_cardio, "strength": n_strength, "rest": n_rest},
    }


def _to_date(v):
    return v if isinstance(v, _date) else _date.fromisoformat(str(v)[:10])


# Overlays B's pins onto the proposed days: a pinned date's activity is FIXED (overrides the LLM) and
# the day is locked so enforce_week never moves it. Pure. Input: proposed days + pins
# (each {date, activity_type?, run_type?, strength_focus?, note?}). Output: days with dates coerced.
def _apply_pins(proposed: list[dict], pins: list[dict] | None) -> list[dict]:
    by_date = {_to_date(p["date"]): p for p in (pins or [])}
    out = []
    for d in proposed:
        dt = _to_date(d["date"])
        p = by_date.get(dt)
        if p:
            out.append({
                "date": dt,
                "activity_type": p.get("activity_type", d.get("activity_type", ["rest"])),
                "run_type": p.get("run_type", d.get("run_type")),
                "strength_focus": p.get("strength_focus", d.get("strength_focus")),
                "is_vegetarian_day": d.get("is_vegetarian_day", False),
                "note": p.get("note", d.get("note")),
                "locked": True,
            })
        else:
            out.append({**d, "date": dt, "locked": False})
    return out


# LLM glue: Gemini Pro proposes the week shape -> parse -> overlay pins -> assemble (enforce + macros).
# Input: the state dict (state.py), nutrition cfg, weekly_training rules. Output: assemble_week()'s
# dict + the LLM's `rationale` and `status_line`. NOT unit-tested (LLM call).
def plan_week(state: dict, cfg: dict, rules: dict) -> dict:
    raw = generate_json_reasoning(wk_prompt.build_prompt(state), model=MODEL_PRO)
    parsed = parse_json_response(raw)
    proposed = _apply_pins(parsed.get("days", []), state.get("pins"))
    result = assemble_week(proposed, state["weekly_target"], state.get("weight_kg"), cfg, rules,
                           done_this_week=state.get("done_this_week"))
    result["rationale"] = parsed.get("rationale")
    result["status_line"] = parsed.get("status_line")
    return result
