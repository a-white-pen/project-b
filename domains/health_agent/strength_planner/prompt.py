"""
Gemini Pro prompt for the day-of STRENGTH session (BRIEF §11). The model decides EVERYTHING about
the prescription — which exercises, how many sets, the rep range, the rest range, the target weight —
from best evidence (hypertrophy/strength science) AND B's own data in the state packet (recent
working loads, recovery, running load, weight). NOTHING about reps/sets/rest is hardcoded; the
catalog below is STRUCTURAL only (what each exercise is + where she does it + how it loads).

"LLM proposes, code guarantees": the planner (planner.py) then validates the model's picks against
the catalog, resolves + rounds weights to loadable increments, applies wide sanity clamps, and
enforces the deterministic rules (compound-first ordering, venue-scoped pairings). So this prompt GUIDES; it
does not need to be perfectly obeyed.

Stable SYSTEM prefix first (goals + conventions + catalog + output contract), dynamic state JSON
after — so Gemini implicit caching can hit across days.

Functions:
  build_prompt(state) -> str
"""

import json

from domains.health_agent.strength_planner import catalog
from domains.health_agent.goals import goals_prompt_block, load_goals


# One model-friendly line per catalog exercise AT THE ACTIVE VENUE — STRUCTURAL facts only (no
# rep/rest/set numbers). The model reads pattern/role/equipment to balance the session and decides
# the prescription. Dumbbell loads show the venue's real unit (kg in Singapore, lb in Bangkok).
def _catalog_block(venue: str) -> str:
    venues = catalog.load_catalog()["meta"].get("venues", {})
    db_unit = ((venues.get(venue, {}) or {}).get("dumbbell", {}) or {}).get("unit", "kg")
    lines = []
    for e in catalog.exercises_for_venue(venue):
        flags = []
        if e.get("reps_per_side"):
            flags.append("per-side")
        if not e.get("garmin"):
            flags.append("no-watch-code")
        anchor = e.get("pairs_after")
        if anchor and venue in (e.get("pairs_after_at") or [venue]):
            flags.append(f"only-with:{anchor}")
        seed = e.get("seed_weight")
        fixed = catalog.active_fixed_weight(e, venue)         # venue-scoped fixed load (e.g. BKK 3 kg pair)
        if e.get("equipment") == "bodyweight":
            load = "bodyweight"
        elif e.get("equipment") == "dumbbell":
            load = f"dumbbell({db_unit})"
        else:
            load = f"{e.get('equipment')}({e.get('load_unit')})"
        if fixed:                                             # forced, non-progressing weight — say so, skip seed
            load += f", fixed {fixed.get('value')}{fixed.get('unit')}"
            flags.append("fixed-weight")
        elif load != "bodyweight" and seed and seed.get("unit") != "bodyweight":
            sv, su = seed.get("value"), seed.get("unit")          # show the seed in this line's display unit
            disp = db_unit if e.get("equipment") == "dumbbell" else "kg"
            if sv is not None and su != disp:
                sv = round(catalog.lb_to_kg(sv), 1) if su == "lb" else round(catalog.kg_to_lb(sv), 1)
            load += f", seed {sv}{disp}"
        tail = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"- {e['name']} | {e.get('movement_pattern')} | "
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
Order exercises as they should be performed (the system also orders compounds first + enforces venue pairings).
Use target_weight_kg=null for bodyweight and fixed-weight exercises. For loaded lifts, base it on the
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

Choose exercises ONLY from this catalog (B's active gym), by EXACT name:
{_catalog_block(state.get("venue") or catalog.default_venue())}

{_OUTPUT_SCHEMA}"""


# Builds the full prompt: stable SYSTEM (goals + conventions + catalog + output contract), then the
# dynamic state packet (recent loads, recovery, running, weight) as JSON.
# Input: the state dict from state.build_state. Output: the prompt string.
def build_prompt(state: dict) -> str:
    return _system(state) + "\n\nSTATE (B's live data):\n" + json.dumps(state, ensure_ascii=False, default=str)
