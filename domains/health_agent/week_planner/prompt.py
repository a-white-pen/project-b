"""
Builds the Gemini Pro prompt for the WEEK SCAFFOLD / re-plan. The model proposes only the week SHAPE
— the code (enforce + macros) guarantees the rules + computes the macro targets (§2 enforcement
boundary), so the prompt tells it NOT to compute calories or force counts. Stable SYSTEM prefix
(goals + rules + schema) first, dynamic state JSON after, for implicit caching.

The weekend-avoidance clause and the vegetarian-day rule/field are toggled by the mode flags
(mode.avoid_weekends, mode.b_extended_plans_meals) so the model isn't asked to plan a concept B has
turned off — e.g. in Singapore any day is fine and B self-orders meals, so there's no veg day.

state shape (state.py produces it):
  {today, horizon: [date], weekly_target, weight_kg, directives, recent_training, pins, existing}

Functions:
  build_prompt(state) -> str
"""

import json

from domains.health_agent.goals import goals_prompt_block, mode_config

_SYSTEM = """You plan B's training WEEK. Propose only the SHAPE — the code enforces the rules and sets
the macros afterwards, so do NOT compute calories or rigidly force counts; aim for the targets below.

Aim for (code will enforce/relax + report what bent):
- 2 cardio (runs) + 2 strength per CALENDAR week (Mon-Sun), >= 3 sessions total. `done_this_week` in the
  state = sessions already done THIS week before today; plan only the REMAINDER for the current week, and
  a fresh 2 cardio + 2 strength for any days that fall in the NEXT week (this window can span two weeks).
- {weekend_clause} Keep >= 1 full rest day. Never two HARD days back-to-back (strength, or a
  quality/fartlek run). No heavy lower-body the day before a hard run (keep legs fresh for running).{veg_clause}
- Honor B's PINS exactly — a pinned day is fixed; plan around it.

B's 3 EQUAL goals + the carry-forward directives steer emphasis (run focus, strength emphasis):
{goals}

For EACH day in the horizon, output: activity_type (a subset of rest|cardio|strength|other — usually
one; a 2-a-day is allowed), run_type (easy|long|quality|fartlek on a run day, else null),
strength_focus (e.g. "full body", else null){veg_field}.

Output STRICT JSON only:
{{"days": [{{"date": "YYYY-MM-DD", "activity_type": [str], "run_type": str|null,
"strength_focus": str|null{veg_json}}}],
"rationale": str, "status_line": str}}
status_line = one dry line for the re-plan header, e.g. "Reshuffled 3 days — same 2+2, better spaced."
"""


# Builds the scaffold prompt: stable SYSTEM (goals + rules + schema) first, then the week state JSON.
# The weekend + vegetarian clauses are gated by mode.avoid_weekends / mode.b_extended_plans_meals so the
# model never plans a turned-off concept (saves tokens + avoids stale guidance). Input: the state dict
# (state.py). Output: the prompt string (dates serialised via default=str).
def build_prompt(state: dict) -> str:
    mode = mode_config()
    weekend_clause = ("Avoid weekends (social)." if mode.get("avoid_weekends", True)
                      else "Any day of the week is fine (weekends included); just spread the sessions out.")
    if mode.get("b_extended_plans_meals", True):
        veg_clause = ("\n- ONE vegetarian day PER CALENDAR WEEK (Mon-Fri) — if the horizon spans two weeks, "
                      "give each week its own veg day (code drops a duplicate if this week already had one).")
        veg_field = ", is_vegetarian_day (bool)"
        veg_json = ', "is_vegetarian_day": bool'
    else:
        veg_clause = veg_field = veg_json = ""
    system = _SYSTEM.format(goals=goals_prompt_block(), weekend_clause=weekend_clause,
                            veg_clause=veg_clause, veg_field=veg_field, veg_json=veg_json)
    return system + "\n\nWEEK STATE:\n" + json.dumps(state, ensure_ascii=False, default=str)
