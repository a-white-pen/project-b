"""
Per-day macro_target builder — the deterministic replacement for the retired kcal/kg macro_budget.py.

Turns a single day's kcal target (from the §7 weekly split, calibration.compute_day_targets) + the
day type + bodyweight into the macro_target jsonb cached on health_agent.daily_plan. PURE — cfg is
goals.yaml `nutrition`. Shared (the scaffold sets macro_target; the day-of meal planner refreshes it).

Shape (matches the daily_plan.macro_target DDL comment):
  {"kcal":{low,target,high}, "protein_g":{low,high}, "fat_g":{min},
   "carbs_g":{target}, "fibre_g":{target,stretch}, "day_type"}
- kcal low/high = target ± kcal_band_kcal.
- protein_g = bodyweight × protein_g_per_kg band (the STRENGTH band on strength days), floored at
  protein_floor_g (lean-recomp: protein is the lever).
- fat_g.min = fat_floor_g. carbs = remainder after the protein floor + fat floor (NO carb floor).

Functions:
  build_macro_target(day_kcal, day_type, weight_kg, cfg) -> dict
"""


# Builds the macro_target dict for one day. Inputs: the day's kcal target, day_type
# ('cardio'|'strength'|'rest'), current bodyweight kg (None -> protein falls back to the floor),
# and the nutrition cfg. Output: the macro_target jsonb dict.
def build_macro_target(day_kcal: int, day_type: str, weight_kg: float | None, cfg: dict) -> dict:
    band = cfg["kcal_band_kcal"]
    atwater = cfg["atwater"]                      # kcal per gram: {protein:4, carb:4, fat:9}
    floor = cfg["protein_floor_g"]
    gkg = cfg["protein_g_per_kg"]["strength" if day_type == "strength" else "default"]

    if weight_kg:
        p_low = max(round(weight_kg * gkg[0]), floor)
        p_high = max(round(weight_kg * gkg[1]), p_low)
    else:
        p_low = p_high = floor

    fat_min = cfg["fat_floor_g"]
    # carbs = remainder once the protein floor (p_low) + fat floor are reserved; never negative.
    carbs_target = max(
        0,
        round((day_kcal - p_low * atwater["protein"] - fat_min * atwater["fat"]) / atwater["carb"]),
    )

    return {
        "kcal": {"low": day_kcal - band, "target": day_kcal, "high": day_kcal + band},
        "protein_g": {"low": p_low, "high": p_high},
        "fat_g": {"min": fat_min},
        "carbs_g": {"target": carbs_target},
        "fibre_g": {"target": cfg["fibre_g"], "stretch": cfg.get("fibre_stretch_g", cfg["fibre_g"])},
        "day_type": day_type,
    }
