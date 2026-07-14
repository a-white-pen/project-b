"""
Builds the Gemini Pro prompt for the WEEK SCAFFOLD / re-plan. The model proposes only the week SHAPE
— the code (enforce + macros) guarantees the rules + computes the macro targets (§2 enforcement
boundary), so the prompt tells it NOT to compute calories or force counts. Stable SYSTEM prefix
(goals + rules + schema) first, dynamic state JSON after, for implicit caching.

state shape (state.py produces it):
  {today, horizon: [date], weekly_target, weight_kg, directives, recent_training, pins, existing}

Functions:
  build_prompt(state) -> str
"""

import json

from domains.health_agent.goals import goals_prompt_block

_SYSTEM = """You plan B's training WEEK. Propose only the SHAPE — the code enforces the rules and sets
the macros afterwards, so do NOT compute calories or rigidly force counts; aim for the targets below.

Aim for (code will enforce/relax + report what bent):
- 2 cardio (runs) + 2 strength per CALENDAR week (Mon-Sun), >= 3 sessions total. `done_this_week` in the
  state = sessions already done THIS week before today; plan only the REMAINDER for the current week, and
  a fresh 2 cardio + 2 strength for any days that fall in the NEXT week (this window can span two weeks).
- Avoid weekends (social). Keep >= 1 full rest day. Never two HARD days back-to-back (strength, or a
  quality/fartlek run). No heavy lower-body the day before a hard run (keep legs fresh for running).
- ONE vegetarian day PER CALENDAR WEEK (Mon-Fri) — if the horizon spans two weeks, give each week its own
  veg day (code drops a duplicate if this week already had one).
- Honor B's PINS exactly — a pinned day is fixed; plan around it.

B's 3 EQUAL goals + the carry-forward directives steer emphasis (run focus, strength emphasis):
{goals}

For EACH day in the horizon, output: activity_type (a subset of rest|cardio|strength|other — usually
one; a 2-a-day is allowed), run_type (easy|long|quality|fartlek on a run day, else null),
strength_focus (e.g. "full body", else null), is_vegetarian_day (bool).

Output STRICT JSON only:
{{"days": [{{"date": "YYYY-MM-DD", "activity_type": [str], "run_type": str|null,
"strength_focus": str|null, "is_vegetarian_day": bool}}],
"rationale": str, "status_line": str}}
status_line = one dry line for the re-plan header, e.g. "Reshuffled 3 days — same 2+2, better spaced."
"""


# Builds the scaffold prompt: stable SYSTEM (goals + rules + schema) first, then the week state JSON.
# Input: the state dict (state.py). Output: the prompt string (dates serialised via default=str).
def build_prompt(state: dict) -> str:
    system = _SYSTEM.format(goals=goals_prompt_block())
    return system + "\n\nWEEK STATE:\n" + json.dumps(state, ensure_ascii=False, default=str)
