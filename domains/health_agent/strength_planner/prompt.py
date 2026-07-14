"""
Gemini Pro prompt for the day-of STRENGTH session (BRIEF §11). The model decides EVERYTHING about
the prescription — which exercises, how many sets, the rep range, the rest range, the target weight —
from best evidence (hypertrophy/strength science) AND B's own data in the state packet (recent
working loads, recovery, running load, weight). NOTHING about reps/sets/rest is hardcoded; the
catalog below is STRUCTURAL only (what each exercise is + where she does it + how it loads).

"LLM proposes, code guarantees": the planner (planner.py) then validates the model's picks against
the catalog, resolves + rounds weights to loadable increments, applies wide sanity clamps, and
enforces the deterministic rules (apartment-last, Hip-Thrust-after-Row). So this prompt GUIDES; it
does not need to be perfectly obeyed.

Stable SYSTEM prefix first (goals + conventions + catalog + output contract), dynamic state JSON
after — so Gemini implicit caching can hit across days.

Functions:
  build_prompt(state) -> str
"""

import json

from domains.health_agent.strength_planner import catalog
from domains.health_agent.goals import goals_prompt_block, load_goals


# One model-friendly line per catalog exercise — STRUCTURAL facts only (no rep/rest/set numbers).
# The model reads location/pattern/role/equipment to balance the session and decides the prescription.
def _catalog_block() -> str:
    lines = []
    for e in catalog.load_catalog()["exercises"]:
        flags = []
        if e.get("reps_per_side"):
            flags.append("per-side")
        if not e.get("garmin"):
            flags.append("no-watch-code")
        if e.get("pairs_after"):
            flags.append(f"only-with:{e['pairs_after']}")
        fixed = e.get("fixed_weight")
        seed = e.get("seed_weight")
        if fixed:
            load = f"fixed {fixed['value']}{fixed.get('unit', 'kg')}"
        elif e.get("equipment") == "bodyweight":
            load = "bodyweight"
        else:
            load = f"{e.get('equipment')}({e.get('load_unit')})"
            if seed and seed.get("unit") != "bodyweight":
                load += f", seed {seed['value']}{seed.get('unit')}"
        tail = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {e['name']} | {e['location']} | {e.get('movement_pattern')} | "
                     f"{e.get('role')} | {load}{tail}")
    return "\n".join(lines)


# Compact YAML of the strength conventions (duration, weekly volume, rules) for the prompt.
def _conventions_block() -> str:
    import yaml
    s = load_goals().get("strength", {})
    keep = {k: s.get(k) for k in ("duration_min", "duration_range_min", "sets_per_muscle_per_week",
                                  "sets_per_exercise", "order", "rules") if s.get(k) is not None}
    return yaml.safe_dump(keep, sort_keys=False, allow_unicode=True).strip()


_OUTPUT_SCHEMA = """Return STRICT JSON only, no prose, no code fences:
{"focus": "full_body"|"upper"|"lower"|"push"|"pull",
 "rationale": "1-2 sentences citing the data that drove today's choices",
 "exercises": [
   {"name": "<EXACT name from the catalog>",
    "sets": <int>,
    "reps_low": <int>, "reps_high": <int>,
    "rest_low_s": <int>, "rest_high_s": <int>,
    "target_weight_kg": <number or null>}
 ]}
Order exercises as they should be performed (the system enforces apartment-last + Hip-Thrust-after-Row).
Use target_weight_kg=null for bodyweight or fixed-apartment exercises. For loaded lifts, base it on the
exercise_history.recent_top_kg in the state (progress conservatively — small jumps, ~1-2 reps in reserve)."""


def _system(state: dict) -> str:
    return f"""You are B's strength coach. Plan ONE strength session for {state['today']} ({state['weekday']}).

B's three goals are weighted EQUALLY (lean recomp, build balanced muscle, run sub-60 10k injury-free):
{goals_prompt_block()}

Conventions (science-based; the system also enforces the hard ones):
{_conventions_block()}

YOU decide, per exercise, from best evidence AND B's data below — reps, sets, rest and target weight:
- SETS: ~2-4 working sets/exercise; aim ~10-20 hard sets per muscle across the WEEK (use recent_sessions
  + exercise_history to avoid over/under-doing a muscle). Heavier compounds earn more sets than small
  isolation/core.
- REPS: pick an evidence-based range per exercise (heavy compounds lower e.g. 6-10; isolation/core higher
  e.g. 12-20). Give reps_low and reps_high.
- REST: longer for compounds (90-150s), short for isolation/core (30-60s). Give rest_low_s and rest_high_s.
- WEIGHT: progress conservatively from exercise_history.recent_top_kg; null for bodyweight/fixed.
- RECOVERY: if she ran in the last 0-2 days (running.days_since_last_run), keep lower-body moderate and
  protect the legs; 3+ days is safe for full intensity. If sleep is clearly short, trim volume MODESTLY.
- Bias volume to glutes/hamstrings/back/shoulders/core. Prefer the dumbbell triceps extension over the
  cable pushdown, and don't program both triceps isolations in one session.
- Keep the whole session within the ~80 min budget.
{("- B's note for today: " + state["note"]) if state.get("note") else ""}
{("- B's correction to honour (re-plan accordingly): " + state["correction"]) if state.get("correction") else ""}

Choose exercises ONLY from this catalog, by EXACT name:
{_catalog_block()}

{_OUTPUT_SCHEMA}"""


# Builds the full prompt: stable SYSTEM (goals + conventions + catalog + output contract), then the
# dynamic state packet (recent loads, recovery, running, weight) as JSON.
# Input: the state dict from state.build_state. Output: the prompt string.
def build_prompt(state: dict) -> str:
    return _system(state) + "\n\nSTATE (B's live data):\n" + json.dumps(state, ensure_ascii=False, default=str)
