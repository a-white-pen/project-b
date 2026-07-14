"""
Gemini Pro prompt for QUALITY / FARTLEK run sessions — the model designs the interval structure
(warmup → repeats of work+recovery → cooldown) toward B's sub-60 10k goal. easy/long runs are
deterministic (no model call), so this prompt is only used on the quality/fartlek path.

All speeds in km/h (B runs treadmill by default). Deterministic guardrails — rep count, speed caps,
forced warmup/cooldown, recovery-strictly-easier-than-work — are applied in intervals.py AFTER the
model responds, not trusted here.

Function:
  build_run_prompt(state) -> str
"""

import json

OUTPUT_SCHEMA = """\
Return ONLY a JSON object (no markdown) with this exact shape (all speeds in km/h):
{
  "rationale": "1-2 sentences on the session and how it serves the race-pace goal",
  "warmup":   {"minutes": n, "speed_kmh": n},
  "repeats":  {"count": n,
               "work":     {"distance_m": n_or_null, "seconds": n_or_null, "speed_kmh": n},
               "recovery": {"seconds": n, "speed_kmh": n}},
  "cooldown": {"minutes": n, "speed_kmh": n}
}
Rules:
- Give "work" EITHER a distance_m OR a seconds (the other null). Quality reps are usually distance
  (e.g. 800 m); fartlek surges are usually time (e.g. 60 s).
- Work speed MUST be faster than recovery speed and faster than B's easy pace (~7.5 km/h). Aim the
  work reps at/near race pace (~10 km/h) for quality; a touch easier with more variety for fartlek.
- Keep it sane: 4-8 reps, work 200-1200 m or 30-180 s, recovery 60-180 s, warmup 8-12 min,
  cooldown 5-10 min. Never prescribe a speed above 13.5 km/h.
"""

SYSTEM = """\
You are B's run coach. Design TODAY's {run_type} interval session. B's standing goal: {goal}.
Use her recent runs (in the state) to gauge fitness and freshness — if she's run a lot or hard
lately, keep the volume modest; if fresh, you can push a little.

The session is a structured workout: a warmup, a repeat block of (work + recovery), and a cooldown.
Work reps target race pace; recovery is easy jogging. All speeds in km/h.
{note_line}{correction_line}
{output_schema}
"""


def build_run_prompt(state: dict) -> str:
    note_line = f"\nB's note for today: {state['note']}\n" if state.get("note") else ""
    corr = f"\nB's correction to honour (re-design accordingly): {state['correction']}\n" if state.get("correction") else ""
    return (
        SYSTEM.format(run_type=state.get("run_type", "quality"), goal=state.get("goal", ""),
                      note_line=note_line, correction_line=corr, output_schema=OUTPUT_SCHEMA)
        + "\n\nSTATE PACKET (today's run + recent runs + seed pace):\n"
        + json.dumps(state, indent=2, default=str)
        + "\n\nReturn the JSON now."
    )
