"""
Assembles the state packet the quality/fartlek interval planner hands to Gemini Pro. The caller
already knows today's run_type + surface (from cardio_plan via persistence.read_run_day); this adds
the config seed pace, the standing race goal, and B's recent runs (load + fitness context).

Per [[feedback_riegel_scope]]: NEVER plan from the Riegel 10k estimate — fitness comes from recent
ACTUAL runs (exercise.cardio_activities) only. Resilient: a failing query logs + degrades to [].

Functions:
  build_run_state(run_type, surface, planned_for, correction=None, note=None) -> dict
  _norm_type(v) / _norm_surface(v)   — token normalisers (shared with the correction handler)
"""

import logging

from system.db import get_connection
from domains.health_agent.goals import load_goals
from system.logging import log_event, log_failure

logger = logging.getLogger(__name__)

_RUN_TYPES = {"easy", "long", "quality", "fartlek"}
_SURFACES = {"treadmill", "outdoor"}
QUALITY_TYPES = {"quality", "fartlek"}            # these get LLM-designed intervals + a Garmin push
GOAL = "sub-60 10k (~6:00/km ~10 km/h race pace), staying injury-free"


# Normalises a run-type token; None for unknown (so a bad token falls through to the planned type).
def _norm_type(v) -> str | None:
    if not v:
        return None
    v = str(v).strip().lower()
    return v if v in _RUN_TYPES else None


# Normalises a surface token (accepts common synonyms); None for unknown.
def _norm_surface(v) -> str | None:
    if not v:
        return None
    v = str(v).strip().lower()
    if v in ("outdoor", "outdoors", "outside", "road", "trail", "street"):
        return "outdoor"
    if v in ("treadmill", "indoor", "inside", "mill", "tread"):
        return "treadmill"
    return v if v in _SURFACES else None


# Last few runs for load + fitness context (distance, duration, derived pace, HR, surface). Best-effort.
def _recent_runs() -> list[dict]:
    out: list[dict] = []
    try:
        conn = get_connection()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT started_at::date, distance_m, duration_seconds, average_speed_mps, "
                        "average_heartrate, is_treadmill FROM exercise.cardio_activities "
                        "WHERE activity_category = 'run' AND started_at >= now() - interval '21 days' "
                        "ORDER BY started_at DESC LIMIT 6")
                    rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log_failure(logger, logging.WARNING, "run_state_recent_runs_failed", e)
        return out
    for d, dist, dur, spd, hr, treadmill in rows:
        pace = round(float(dur) / (float(dist) / 1000.0)) if dist and dur and float(dist) > 0 else None
        out.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else str(d),
            "distance_km": round(float(dist) / 1000.0, 1) if dist else None,
            "duration_min": round(float(dur) / 60.0) if dur else None,
            "pace_s_per_km": pace,
            "speed_kmh": round(float(spd) * 3.6, 1) if spd else None,
            "avg_hr": round(float(hr)) if hr else None,
            "treadmill": bool(treadmill) if treadmill is not None else None,
        })
    return out


# Builds the interval-planner state packet. run_type/surface come from the caller (today's cardio_plan
# row + any correction override). seed = goals.running.types[run_type]; goal = the standing race target.
def build_run_state(run_type: str, surface: str, planned_for, correction: str | None = None,
                    note: str | None = None) -> dict:
    types = (load_goals().get("running", {}) or {}).get("types", {})
    seed = types.get(run_type) or types.get("easy") or {"distance_km": 6.0, "speed_kmh": 8.0}
    state = {
        "today": planned_for.isoformat() if hasattr(planned_for, "isoformat") else str(planned_for),
        "run_type": run_type,
        "surface": surface,
        "seed": seed,
        "goal": GOAL,
        "correction": correction,
        "note": note,
        "recent_runs": _recent_runs(),
    }
    log_event(logger, logging.INFO, "run_state_built", today=state["today"], run_type=run_type,
              surface=surface, recent=len(state["recent_runs"]))
    return state
