"""
The day-of strength planner's enforcement layer (BRIEF §11 "LLM proposes, code guarantees"). Takes
the state packet, calls Gemini (Pro for the initial plan, Flash for corrections) to PROPOSE the
prescription, then GUARANTEES a valid session in pure, unit-tested code:

  parse JSON -> per exercise: map to a known catalog name (drop unknowns), clamp sets/reps/rest to
  wide sanity bounds, resolve + round the weight to a loadable increment, compute the .fit values
  (reps = TOP of the range, rest = MIDPOINT) -> enforce pairings (Hip Thrust only/immediately after
  Seated Cable Row) -> order gym-first then apartment-last -> canonical plan dict.

The reps/rest RANGES come from the model (evidence + B's data), NOT a catalog table — the only
numeric guarantees here are WIDE safety clamps so a malformed reply can't emit a nonsense .fit.

Pure except plan_session's single generate_json_reasoning call (stub it in tests). The canonical
plan dict it returns is consumed verbatim by render.py / fit.py / garmin_upload.py.

Functions:
  plan_session(state, model=MODEL_PRO) -> dict     # the canonical plan (raises ValueError if empty)
  estimate_minutes(exercises) -> int
"""

import logging

from domains.health_agent.strength_planner import catalog
from domains.health_agent.strength_planner import prompt as strength_prompt
from system.llm import MODEL_PRO, generate_json_reasoning, parse_json_response
from system.logging import log_event

logger = logging.getLogger(__name__)

# WIDE sanity clamps — guards against a malformed model reply, NOT a prescription. The model decides
# the actual reps/sets/rest within these from evidence + B's data.
MIN_SETS, MAX_SETS, DEFAULT_SETS = 1, 6, 3
MIN_REPS, MAX_REPS, DEFAULT_REPS = 1, 30, 10
MIN_REST_S, MAX_REST_S, DEFAULT_REST_S = 15, 300, 90
MAX_EXERCISES = 12
_FOCI = {"full_body", "upper", "lower", "push", "pull"}


def _as_int(v, default: int) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return default


def _as_float(v):
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


# Clamps a [low, high] range into [mn, mx] and guarantees low <= high.
def _clamp_range(low: int, high: int, mn: int, mx: int) -> tuple[int, int]:
    low, high = _clamp(low, mn, mx), _clamp(high, mn, mx)
    return (high, low) if low > high else (low, high)


# Resolves an exercise's working weight to a {kg_low, kg_high, basis} dict (or None for bodyweight).
# Chain: fixed apartment weight -> model's target_weight_kg -> B's recent_top_kg from history ->
# catalog seed -> None. The chosen target is rounded to a loadable increment via catalog.round_weight_kg.
def _resolve_weight(item: dict, entry: dict, name: str, state: dict) -> dict | None:
    basis = entry.get("weight_basis")
    if entry.get("equipment") == "bodyweight":
        return None
    if entry.get("fixed_weight"):                       # apartment 3 kg dumbbells — forced
        kg = catalog.round_weight_kg(0, entry)
        return {"kg_low": kg, "kg_high": kg, "basis": basis} if kg is not None else None

    target = _as_float(item.get("target_weight_kg"))
    if target is None:                                  # fall back to what B actually lifted recently
        hist = (state.get("exercise_history") or {}).get(name) or {}
        if hist.get("recent_top_kg") is not None:
            target = float(hist["recent_top_kg"])
    if target is None:                                  # cold start — catalog seed hint
        sw = entry.get("seed_weight") or {}
        sv = sw.get("value")                            # guard malformed catalog entries (unit, no value)
        if sv is not None and sw.get("unit") == "lb":
            target = catalog.lb_to_kg(sv)
        elif sv is not None and sw.get("unit") == "kg":
            target = float(sv)
    if target is None:
        return None
    kg = catalog.round_weight_kg(target, entry)
    if kg is None:
        return None
    return {"kg_low": kg, "kg_high": kg, "basis": basis}


# Builds one canonical exercise dict from a model item, or None if the name is not in the catalog.
# Clamps sets/reps/rest to sanity bounds, resolves the weight, and computes the .fit scalars
# (reps = TOP of range so B records fewer if a set falls short; rest = MIDPOINT).
def _build_exercise(item: dict, state: dict) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = catalog.canonical_from_alias(item.get("name"))
    if not name or not catalog.is_known(name):
        return None
    entry = catalog.get_exercise(name)

    sets = _clamp(_as_int(item.get("sets"), DEFAULT_SETS), MIN_SETS, MAX_SETS)
    rl = _as_int(item.get("reps_low"), DEFAULT_REPS)
    rh = _as_int(item.get("reps_high"), rl)
    rl, rh = _clamp_range(rl, rh, MIN_REPS, MAX_REPS)
    sl = _as_int(item.get("rest_low_s"), DEFAULT_REST_S)
    sh = _as_int(item.get("rest_high_s"), sl)
    sl, sh = _clamp_range(sl, sh, MIN_REST_S, MAX_REST_S)

    weight = _resolve_weight(item, entry, name, state)
    return {
        "name": name,
        "watch_label": entry.get("watch_label", name),
        "location": entry.get("location", "gym"),
        "garmin": entry.get("garmin"),
        "sets": sets,
        "reps": {"low": rl, "high": rh},
        "reps_per_side": bool(entry.get("reps_per_side")),
        "rest_s": {"low": sl, "high": sh},
        "weight": weight,
        "note": "per side" if entry.get("reps_per_side") else None,
        "fit": {"reps": rh, "rest_s": round((sl + sh) / 2),
                "weight_kg": weight["kg_high"] if weight else None},
    }


# Enforces catalog `pairs_after` constraints (today: Hip Thrust only/immediately after Seated Cable
# Row — they share the bench at the cable-row station). A dependent whose anchor is absent is DROPPED;
# one whose anchor is present is re-sequenced to immediately follow it. Generalises to any pairs_after.
def _enforce_pairings(built: list[dict]) -> list[dict]:
    present = {e["name"] for e in built}
    deferred: dict[str, list[dict]] = {}
    base: list[dict] = []
    dropped: list[str] = []
    for ex in built:
        anchor = catalog.get_exercise(ex["name"]).get("pairs_after")
        if not anchor:
            base.append(ex)
        elif anchor in present:
            deferred.setdefault(anchor, []).append(ex)
        else:
            dropped.append(ex["name"])
    out: list[dict] = []
    for ex in base:
        out.append(ex)
        out.extend(deferred.pop(ex["name"], []))       # insert dependents right after their anchor
    for leftovers in deferred.values():                # anchor present but itself was a dependent
        out.extend(leftovers)
    if dropped:
        log_event(logger, logging.INFO, "strength_pairing_dropped", exercises=dropped)
    return out


# Final safety net AFTER the MAX_EXERCISES truncation: drops any dependent whose pairs_after anchor
# got cut by the cap (anchors carry no pairs_after, so they are never dropped here) — guarantees the
# pairing invariant holds in the emitted plan even when the model proposes more than the cap.
def _drop_orphaned_pairs(exercises: list[dict]) -> list[dict]:
    names = {e["name"] for e in exercises}
    kept, dropped = [], []
    for ex in exercises:
        anchor = catalog.get_exercise(ex["name"]).get("pairs_after")
        if anchor and anchor not in names:
            dropped.append(ex["name"])
        else:
            kept.append(ex)
    if dropped:
        log_event(logger, logging.INFO, "strength_pairing_dropped_post_truncation", exercises=dropped)
    return kept


# Rough session duration estimate (minutes): per set, ~3.5s/rep work (bounded) + the rest, +45s setup
# per exercise. Logged-only signal for the card; never clamps the plan.
def estimate_minutes(exercises: list[dict]) -> int:
    total_s = 0.0
    for ex in exercises:
        reps = ex["reps"]["high"]
        rest = ex["fit"]["rest_s"]
        work = min(max(reps * 3.5, 25), 80)
        total_s += ex["sets"] * (work + rest) + 45
    return round(total_s / 60)


# Generates today's canonical strength plan: prompt -> LLM proposes -> code guarantees. model=MODEL_PRO
# for the initial plan, MODEL_FLASH for corrections. Returns the canonical plan dict (consumed by
# render/fit/garmin). Raises ValueError if no valid exercises survive (caller treats as a failure).
def plan_session(state: dict, model: str = MODEL_PRO) -> dict:
    raw = generate_json_reasoning(strength_prompt.build_prompt(state), model=model)
    data = parse_json_response(raw)

    built: list[dict] = []
    seen: set[str] = set()
    for item in (data.get("exercises") or []):
        ex = _build_exercise(item, state)
        if ex is None or ex["name"] in seen:
            continue
        seen.add(ex["name"])
        built.append(ex)

    built = _enforce_pairings(built)
    gym = [e for e in built if e["location"] != "apartment"]
    apartment = [e for e in built if e["location"] == "apartment"]
    ordered = (gym + apartment)[:MAX_EXERCISES]         # gym first, apartment last
    ordered = _drop_orphaned_pairs(ordered)             # the cap must not strand a dependent from its anchor
    if not ordered:
        raise ValueError("planner produced no valid exercises")

    focus = data.get("focus") if data.get("focus") in _FOCI else "full_body"
    plan = {
        "planned_for": state["today"],
        "focus": focus,
        "rationale": (data.get("rationale") or "").strip(),
        "model": model,
        "estimated_minutes": estimate_minutes(ordered),
        "exercises": ordered,
    }
    log_event(logger, logging.INFO, "strength_planned", plan_date=state.get("today"),
              focus=focus, exercises=len(ordered), est_min=plan["estimated_minutes"])
    return plan
