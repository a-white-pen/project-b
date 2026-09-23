"""Computes training progress and weight-band status for the weekly reflection.

The 10 km estimate is display-only. It uses the latest completed quality or fartlek run and must not
be used to plan exercise or nutrition.

Functions:
  riegel_project — estimates a target-distance time from one run
  format_duration — formats seconds as race time
  ten_k_goal_progress — computes the rough sub-60 10 km status
  band_position — describes a weight value within the reference band
  _total_volume — calculates recorded strength volume
  _top_weight_by_exercise — finds the heaviest load by exercise
  strength_volume_deltas — compares strength load and volume between periods
"""

RIEGEL_EXPONENT = 1.06
SUB60_SECONDS = 3600


# Estimates a target-distance time from a representative run.
# Returns seconds, or None for invalid inputs.
def riegel_project(distance_m, duration_s, target_m: float = 10000.0):
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return duration_s * (target_m / distance_m) ** RIEGEL_EXPONENT


# Formats seconds as race time with uncapped minutes.
def format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


# Computes a rough display-only 10 km estimate from a quality or fartlek run.
# Returns the estimate and minutes above 60, or None when the run is unusable.
def ten_k_goal_progress(distance_m, duration_s):
    est = riegel_project(distance_m, duration_s, 10000.0)
    if est is None:
        return None
    return {
        "est_10k_s": est,
        "est_10k_label": format_duration(est),
        "min_to_go": max(0.0, round((est - SUB60_SECONDS) / 60.0, 1)),
    }


# Describes the current seven-day average within or outside the reference band.
def band_position(weight_kg, low: float, high: float) -> dict:
    if weight_kg is None:
        return {"zone": "unknown", "label": "no recent weight"}
    if weight_kg > high:
        return {"zone": "above", "label": f"{weight_kg:.1f}, above band"}
    if weight_kg < low:
        return {"zone": "below", "label": f"{weight_kg:.1f}, below band"}
    third = (high - low) / 3.0
    if weight_kg >= high - third:
        zone, where = "top", "in band (top)"
    elif weight_kg <= low + third:
        zone, where = "bottom", "in band (bottom)"
    else:
        zone, where = "mid", "in band (mid)"
    return {"zone": zone, "label": f"{weight_kg:.1f}, {where}"}


# Calculates total recorded load times reps across strength sets.
def _total_volume(sets) -> float:
    return sum((s.get("weight_kg") or 0) * (s.get("reps") or 0) for s in sets)


# Finds the heaviest recorded load for each exercise.
def _top_weight_by_exercise(sets) -> dict:
    top: dict = {}
    for s in sets:
        w = s.get("weight_kg")
        ex = s.get("exercise_name")
        if w is None or not ex:
            continue
        if ex not in top or w > top[ex]:
            top[ex] = w
    return top


# Compares strength volume and top loads between two periods.
# Returns total volumes, percentage change, and exercises with higher top loads.
def strength_volume_deltas(this_sets, prev_sets) -> dict:
    this_vol = _total_volume(this_sets)
    prev_vol = _total_volume(prev_sets)
    vol_pct = round((this_vol - prev_vol) / prev_vol * 100, 1) if prev_vol else None
    this_top = _top_weight_by_exercise(this_sets)
    prev_top = _top_weight_by_exercise(prev_sets)
    gainers = [
        {"exercise": ex, "from_kg": prev_top[ex], "to_kg": w, "delta_kg": round(w - prev_top[ex], 2)}
        for ex, w in this_top.items()
        if ex in prev_top and w > prev_top[ex]
    ]
    gainers.sort(key=lambda g: g["delta_kg"], reverse=True)
    return {
        "volume_delta_pct": vol_pct,
        "this_volume_kg": this_vol,
        "prev_volume_kg": prev_vol,
        "top_gainers": gainers,
    }
