"""
Builds the Gemini Pro prompt for the weekly reflection's narrative + carry-forward directives.

The deterministic numbers (maintenance/target/goal stats) are computed in code and are FINAL — the
LLM only writes the prose narrative B reads, the machine `directives` the planners carry forward
(running focus, strength emphasis, protein note), and two SHORT per-goal nudges shown in the spec-H
message. Stable SYSTEM prefix first (goals + schema), dynamic week-state JSON after — so Gemini's
implicit caching can hit. Pro via generate_json_reasoning (initial-plan model).

Functions:
  build_reflection_prompt(data, goals) -> str
"""

import json

from domains.health_agent.goals import goals_prompt_block

_SYSTEM = """You are B's personal health coach writing her WEEKLY REFLECTION.
Voice: grounded, dry wit — like a close friend texting. Not corporate cheer, not motivational-app \
warmth. Keep it short and dry. Plain text only — no HTML, no markdown, no angle brackets (< >).

The numbers below are already computed and FINAL — do NOT recompute, round, or contradict them. \
Your job is only the words:
1. narrative: 2-3 dry sentences B reads — what the week's data says across her 3 EQUAL goals \
(maintain weight via lean recomp / build muscle / sub-60 10k). Honest, specific, no fluff.
2. directives: machine carry-forward for next week's planners — concrete short phrases or null: \
running_focus, strength_emphasis, protein_note.
3. run / muscle_status: ONE short nudge each for the message (e.g. run = "add 1 tempo/wk", \
muscle_status = "on track" or "stalled — add volume"). null if there's nothing useful to say \
(e.g. no quality run logged yet -> run = null).

Goals + rules:
{goals}

Output STRICT JSON only:
{{"narrative": str, "directives": {{"running_focus": str|null, "strength_emphasis": str|null, \
"protein_note": str|null}}, "run": str|null, "muscle_status": str|null}}"""


# Builds the Pro prompt: stable SYSTEM (goals + schema) first, then the week's computed state JSON.
# Input: the assembled render data dict (the FINAL numbers) + the goals dict. Output: prompt string.
def build_reflection_prompt(data: dict, goals: dict) -> str:
    system = _SYSTEM.format(goals=goals_prompt_block())
    state = {
        "week": data.get("week_num"),
        "weight": data.get("weight"),
        "maintenance_kcal": data.get("maintenance"),
        "next_week_target_kcal": data.get("target"),
        "direction": data.get("direction"),
        "sub60_10k": data.get("run"),            # {est_label, min_to_go} or None (no quality run yet)
        "build_muscle": data.get("muscle"),       # {summary} or None (no strength logged)
        "weight_band": data.get("weight_goal"),
        "eggs": data.get("eggs"),
        "fish": data.get("fish_note"),
    }
    return system + "\n\nWEEK STATE:\n" + json.dumps(state, ensure_ascii=False)
