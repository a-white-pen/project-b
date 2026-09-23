"""Turns a proposed week into an enforced weekly plan.

Functions:
  _is_rest — checks whether a day has active training
  _normalise_day — normalizes one proposed day
  _summary — builds the weekly activity-count summary
  assemble_week — normalizes days, enforces rules, and adds nutrition targets
  _to_date — converts stored values to dates
  _apply_pins — applies fixed day instructions
  plan_week — asks the model for a week and assembles the result
"""

from datetime import date as _date

from domains.health_agent.goals import build_nutrition_target
from domains.health_agent.week_planner import prompt as wk_prompt
from domains.health_agent.week_planner.enforce import enforce_week
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response

_KINDS = {"rest", "cardio", "strength", "other"}
_RUN_TYPES = {"easy", "long", "quality", "fartlek"}


# Returns True when a day contains no active training.
def _is_rest(day) -> bool:
    return not [a for a in day["activity_type"] if a != "rest"]


# Normalizes one proposed day to the supported activity and run types.
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


# Builds the short activity-count summary shown with a weekly plan.
def _summary(n_cardio: int, n_strength: int) -> str:
    return f"{n_strength} strength + {n_cardio} run{'s' if n_cardio != 1 else ''} this week"


# Enforces the proposed days and adds the fixed nutrition target to each day.
# Returns the final days, enforcement report, summary, and activity counts.
def assemble_week(proposed_days: list[dict], cfg: dict, rules: dict,
                  done_this_week: dict | None = None) -> dict:
    days = [_normalise_day(d) for d in proposed_days]
    enforced, report = enforce_week(days, rules, done_this_week)

    n_cardio = sum(1 for d in enforced if "cardio" in d["activity_type"])
    n_strength = sum(1 for d in enforced if "strength" in d["activity_type"])
    n_rest = sum(1 for d in enforced if _is_rest(d))

    for d in enforced:
        d["macro_target"] = build_nutrition_target(cfg)

    return {
        "days": enforced,
        "report": report,
        "summary": _summary(n_cardio, n_strength),
        "day_counts": {"cardio": n_cardio, "strength": n_strength, "rest": n_rest},
    }


# Converts a stored or serialized date to a date object.
def _to_date(v):
    return v if isinstance(v, _date) else _date.fromisoformat(str(v)[:10])


# Applies fixed day instructions to proposed days before rule enforcement.
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


# Generates a proposed week, applies pins, and returns the enforced plan.
def plan_week(state: dict, cfg: dict, rules: dict) -> dict:
    raw = generate_json_reasoning(wk_prompt.build_prompt(state), model=MODEL_PRO)
    parsed = parse_json_response(raw)
    proposed = _apply_pins(parsed.get("days", []), state.get("pins"))
    result = assemble_week(proposed, cfg, rules, done_this_week=state.get("done_this_week"))
    result["rationale"] = parsed.get("rationale")
    result["status_line"] = parsed.get("status_line")
    return result
