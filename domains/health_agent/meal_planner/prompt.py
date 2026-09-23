"""
Builds the prompt that selects lunch, dinner, and home staples from a filtered menu.

The model chooses item names and amounts. solver.finalize_meals validates those choices and applies
the stored nutrition values and staple limits.

state shape (the glue builds it):
  {remaining, slots_to_plan: [str], palette: [dish], staples: {name: cfg}, is_vegetarian_day,
   protein_owed: [str], protein_rotation: {p: spec}, protein_tally: {p: n},
   protein_tally_2wk: {p: n}, shop}

Functions:
  _staples_for_prompt — formats staple settings for the model
  build_meal_prompt — combines targets, menu choices, and meal constraints
"""

import json

from domains.health_agent.meal_planner import solver

_SYSTEM = """You compose B's meals for the slots listed in slots_to_plan (only those — the others are
already eaten). Build each slot from the PALETTE (the shop's dishes that fit today's remaining budget)
plus the HOME STAPLES. You may MIX freely — a main, a lighter dish, an appetiser/side, plus staples — to
land the day near the remaining target; you do NOT have to pick one big "main" per slot.

Hard rules (the code re-checks + enforces these — but compose to them):
- Pick item_name values ONLY from the palette or the staples below. NEVER invent a dish or a number.
- Staples: for each you include, set `amount` in its `unit` (grams for g/ml staples, a COUNT for eggs),
  UP TO its `max_amount`. Pick the amount to round out the day's protein — you need NOT use the
  full max (e.g. 90 g edamame, not the whole 150 g, if that fits better).
- If it is a vegetarian day, choose vegetarian dishes/staples.
- Aim to land the DAY (already-eaten + your picks) inside remaining.kcal [low..high] and
  remaining.protein_g [low..high]. Carbs, fat, and fibre are flexible.
  Lunch != dinner.
- Protein rotation & VARIETY: protein_rotation is the weekly spec ("per 2wk" specs are judged over two
  weeks); protein_tally is what B has ALREADY eaten this week (protein_tally_2wk = the two-week counts
  for the "per 2wk" proteins). Nudge toward any protein in protein_owed when a fitting dish offers it.
  A protein NOT in protein_owed has met its minimum — STOP favouring it: prefer a protein with a low or
  zero tally so the week stays varied (e.g. after a fish day, do NOT keep picking fish; rotate to beef/
  pork/chicken/etc. that still fit the macros).
- If `correction` is non-null, it's B's quoted-reply fix (e.g. "fish sold out", "swap dinner", "give me
  beef") — HONOR it: drop the sold-out/rejected items, re-pick the slot(s) she names, prefer what she asks.

Output STRICT JSON only:
{{"slots": {{"<slot>": [{{"item_name": str, "role": "main"|"side"|"staple", "amount": number,
"protein_source": [str]}}]}}, "note": str}}
Include `amount` only for staples (in the staple's `unit`; eggs = a count). On each MAIN, set `protein_source` to the protein(s) it
provides — a subset of beef|pork|chicken|duck|lamb|mutton|fish|other_seafood|egg|cheese|plant_other
(this feeds B's weekly protein rotation). `note` = one short line for the card. Plan ONLY the slots in
slots_to_plan.
"""


# Formats staple settings for the prompt, including units, maximum amounts, and nutrition per serving.
def _staples_for_prompt(staples: dict) -> dict:
    out = {}
    for name, cfg in (staples or {}).items():
        amt, unit = solver.parse_serving(cfg.get("serving"))
        out[name] = {"unit": unit, "max_amount": round(amt * int(cfg.get("max_servings", 1))),
                     "serving": cfg.get("serving"),
                     "per_serving": {m: cfg.get(m) for m in ("kcal", "protein_g", "carbs_g", "fat_g")}}
    return out


# Builds the meal prompt from the remaining target, open meal slots, menu, staples, and preferences.
def build_meal_prompt(state: dict) -> str:
    state = {**state, "staples": _staples_for_prompt(state.get("staples") or {})}
    return _SYSTEM + "\n\nDAY STATE:\n" + json.dumps(state, ensure_ascii=False, default=str)
