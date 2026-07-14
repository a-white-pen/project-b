"""
The quality/fartlek interval brain (BRIEF §11): Gemini Pro DESIGNS the session (prompt.py), then
deterministic guardrails clamp rep count + speeds and FORCE a warmup + cooldown ("LLM proposes, code
guarantees"). easy/long runs are deterministic (run.py) and never reach here.

Output: the canonical interval `plan` dict, consumed by run.render_interval_card + garmin_upload +
persistence:
  {planned_for, run_type, surface, model, rationale, needs_garmin: True,
   steps: [ {kind:'warmup',...}, {kind:'repeat', count, work, recovery}, {kind:'cooldown',...} ]}
All speeds km/h. Pure except the single generate_json_reasoning call (stub it in tests).

Function:
  plan_intervals(state, model=MODEL_PRO) -> dict
"""

import logging

from domains.health_agent.run_planner import prompt as run_prompt
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response
from system.logging import log_event

logger = logging.getLogger(__name__)

# Safety + sanity clamps (NOT trusted to the model).
SPEED_MIN_KMH = 5.0
SPEED_MAX_KMH = 13.5
REPS_MIN = 3
REPS_MAX = 8


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _clamp_speed(v) -> float:
    return round(_clamp(float(v if v is not None else 7.5), SPEED_MIN_KMH, SPEED_MAX_KMH), 1)


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Designs the interval session: the LLM proposes, then guardrails clamp + force warmup/cooldown and a
# genuine recovery gap. Input: the state packet (state.build_run_state). Output: the canonical plan dict.
def plan_intervals(state: dict, model: str = MODEL_PRO) -> dict:
    raw = generate_json_reasoning(run_prompt.build_run_prompt(state), model=model)
    data = parse_json_response(raw)
    seed_speed = _clamp_speed((state.get("seed") or {}).get("speed_kmh", 8.0))

    warm = data.get("warmup") or {}
    cool = data.get("cooldown") or {}
    rep = data.get("repeats") or {}
    work = rep.get("work") or {}
    rec = rep.get("recovery") or {}

    warm_s = _clamp(int(_num(warm.get("minutes"), 10) * 60), 300, 900)
    cool_s = _clamp(int(_num(cool.get("minutes"), 8) * 60), 300, 900)
    warm_speed = _clamp_speed(_num(warm.get("speed_kmh"), 7.0))
    cool_speed = _clamp_speed(_num(cool.get("speed_kmh"), 7.0))
    count = _clamp(int(_num(rep.get("count"), 5)), REPS_MIN, REPS_MAX)

    work_speed = _clamp_speed(_num(work.get("speed_kmh"), max(seed_speed, 10.0)))
    # Leave room for a genuine recovery gap below the work speed: never let work sit at the floor.
    work_speed = _clamp_speed(max(work_speed, SPEED_MIN_KMH + 2.0))
    rec_speed = _clamp_speed(_num(rec.get("speed_kmh"), 6.5))
    if rec_speed >= work_speed:                                # recovery MUST be strictly easier
        rec_speed = round(max(SPEED_MIN_KMH, work_speed - 1.0), 1)

    work_step = {"kind": "interval", "label": "fast", "speed_kmh": work_speed}
    if _num(work.get("distance_m")):
        work_step["end_type"] = "distance"
        # Round to the nearest 10 m: a treadmill/watch can't target finer, and it keeps the card, the
        # .fit (which encodes m/10) and the Garmin push all stating the SAME distance (no ±5 m drift).
        meters = _clamp(int(_num(work.get("distance_m"), 800)), 200, 1500)
        work_step["end_m"] = int(round(meters / 10.0)) * 10
    else:
        work_step["end_type"] = "time"
        work_step["end_s"] = _clamp(int(_num(work.get("seconds"), 90)), 30, 240)
    rec_step = {"kind": "recovery", "label": "easy", "speed_kmh": rec_speed,
                "end_type": "time", "end_s": _clamp(int(_num(rec.get("seconds"), 120)), 30, 240)}

    steps = [
        {"kind": "warmup", "label": "warm up", "end_type": "time", "end_s": warm_s, "speed_kmh": warm_speed},
        {"kind": "repeat", "count": count, "work": work_step, "recovery": rec_step},
        {"kind": "cooldown", "label": "cool down", "end_type": "time", "end_s": cool_s, "speed_kmh": cool_speed},
    ]
    plan = {
        "planned_for": state["today"], "run_type": state["run_type"], "surface": state["surface"],
        "model": model, "rationale": (data.get("rationale") or "").strip(), "needs_garmin": True,
        "steps": steps,
    }
    log_event(logger, logging.INFO, "run_intervals_built", run_type=state["run_type"],
              surface=state["surface"], reps=count, work_speed=work_speed)
    return plan
