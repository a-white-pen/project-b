"""
Goal-progress computations for the weekly reflection (spec H) — the deterministic distance-to-target
for the 3 goals. PURE functions; the DB reads (recent runs, strength deltas) + the LLM narrative live
in the reflection service.

⛔ USAGE CONSTRAINT (B, 2026-06-24): the Riegel 10k estimate is for the WEEKLY REFLECTION DISPLAY
ONLY — a rough fitness read. NEVER use it to plan runs, other exercise, or nutrition. Runs are
planned from the fixed seed paces (goals.yaml running.types) + recent load, NOT from race
projections. Caveats it carries: (1) it assumes a near-MAX-effort input run, so the reflection
service feeds it ONLY a QUALITY/FARTLEK run — the most recent on record, carried forward if there's
none this week; if B has NEVER logged one, the estimate is left BLANK ("no quality run yet"). B does
easy runs ad hoc, so a random easy jog would read as a misleadingly slow 10k; (2) it ignores
heart rate. Treat the number as a soft indicator and label it "rough".

- run_sub_60_10k: Riegel projection of a 10k time from a recent run + minutes-to-go vs 60:00.
- maintain_weight: position of the current 7d-avg within the maintain band.
- build_muscle: load/volume deltas come from strength_sets and are assembled in the service.

Functions:
  riegel_project(distance_m, duration_s, target_m) -> float | None
  format_duration(seconds) -> str                     # 3810 -> "63:30" (MM:SS, minutes uncapped)
  ten_k_goal_progress(distance_m, duration_s) -> dict | None
  band_position(weight_kg, low, high) -> dict
  strength_volume_deltas(this_sets, prev_sets) -> dict   # build-muscle load/volume deltas
"""

RIEGEL_EXPONENT = 1.06   # standard endurance fatigue exponent (T2 = T1 * (D2/D1)^1.06)
SUB60_SECONDS = 3600     # the sub-60 10k target


# Riegel race-time projection: project a time at target_m from one representative run.
# Input: a recent run's distance (m) + duration (s), target distance (m, default 10k).
# Output: projected seconds, or None for non-positive inputs. Most accurate when the run is within
# ~0.5-2x the target distance.
def riegel_project(distance_m, duration_s, target_m: float = 10000.0):
    if not distance_m or not duration_s or distance_m <= 0 or duration_s <= 0:
        return None
    return duration_s * (target_m / distance_m) ** RIEGEL_EXPONENT


# Formats seconds as MM:SS with minutes uncapped (race convention): 3810 -> "63:30", 3600 -> "60:00".
def format_duration(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    m, sec = divmod(s, 60)
    return f"{m}:{sec:02d}"


# sub-60 10k progress from a recent run: estimated current 10k time + minutes still to shave vs 60:00.
# REFLECTION DISPLAY ONLY — never for planning. Feed the most recent QUALITY/FARTLEK run (carry it
# forward; blank if none ever) — never an easy jog.
# Input: recent run distance (m) + duration (s). Output: {"est_10k_s","est_10k_label","min_to_go"}
# (min_to_go is 0.0 when already sub-60), or None when there's no usable run.
def ten_k_goal_progress(distance_m, duration_s):
    est = riegel_project(distance_m, duration_s, 10000.0)
    if est is None:
        return None
    return {
        "est_10k_s": est,
        "est_10k_label": format_duration(est),
        "min_to_go": max(0.0, round((est - SUB60_SECONDS) / 60.0, 1)),
    }


# Position of the current 7d-avg weight relative to the maintain band [low, high].
# Output: {"zone": "above"|"top"|"mid"|"bottom"|"below"|"unknown", "label": "55.9, in band (top)"}.
# "top"/"mid"/"bottom" are the in-band thirds (spec H reads e.g. "55.9, in band (top)").
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


# Total training volume (sum of weight_kg * reps) over a set of normalized strength sets.
# Input: list of {"exercise_name","weight_kg","reps"}. Output: kg·reps total (bodyweight rows = 0).
def _total_volume(sets) -> float:
    return sum((s.get("weight_kg") or 0) * (s.get("reps") or 0) for s in sets)


# Heaviest weight_kg seen per exercise. Input: normalized sets. Output: {exercise_name: top_kg}.
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


# build_muscle progress: load/volume deltas this period vs the prior period (for spec H, e.g.
# "RDL +2.5 kg, squat volume +8%"). Pure — the reflection service fetches the two set lists.
# Input: this_sets / prev_sets, each a list of {"exercise_name","weight_kg","reps"} (effective kg).
# Output: {"volume_delta_pct": float|None, "this_volume_kg", "prev_volume_kg",
#          "top_gainers": [{"exercise","from_kg","to_kg","delta_kg"}] sorted by delta desc}.
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
